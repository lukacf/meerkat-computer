"""X11 screenshot and input primitives for the explicitly enabled guest desktop."""

import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass

from .desktop_topology import validate_snapshot
from .protocol import WorkerError

MAX_SCREENSHOT_BYTES = 8_000_000
MAX_COMMAND_TIMEOUT = 104.0
MAX_TOPOLOGY_WINDOWS = 512
MAX_VISUAL_FENCE_SECONDS = 5.0
MAX_DRAG_PATH_SECONDS = 15.0


@dataclass(frozen=True)
class DisplayState:
    width: int
    height: int
    focus: str


@dataclass(frozen=True)
class WindowGeometry:
    window: int
    bounds: tuple[int, int, int, int]
    border: int
    window_class: str
    map_state: str
    override_redirect: bool


@dataclass(frozen=True)
class DesktopTopology:
    screen: DisplayState
    root: int
    tree: tuple[tuple[str, int, int], ...]
    windows: tuple[WindowGeometry, ...]


@dataclass(frozen=True)
class FocusedTopology:
    screen: DisplayState
    tree: tuple[tuple[str, int, int], ...]
    windows: tuple[WindowGeometry, ...]
    client_bounds: tuple[int, int, int, int]
    visible_bounds: tuple[int, int, int, int]


@dataclass(frozen=True)
class FocusedVisualFence:
    topology: FocusedTopology
    rgb_sha256: str

    @property
    def visible_bounds(self) -> tuple[int, int, int, int]:
        return self.topology.visible_bounds


def x11_key(value: str) -> str:
    modifiers = {
        "Control": "ctrl",
        "ControlOrMeta": "ctrl",
        "Alt": "alt",
        "Meta": "super",
        "Shift": "shift",
    }
    keys = {
        "Enter": "Return",
        "Tab": "Tab",
        "Escape": "Escape",
        "Backspace": "BackSpace",
        "Delete": "Delete",
        "Space": "space",
        "ArrowUp": "Up",
        "ArrowDown": "Down",
        "ArrowLeft": "Left",
        "ArrowRight": "Right",
        "Home": "Home",
        "End": "End",
        "PageUp": "Prior",
        "PageDown": "Next",
        "Insert": "Insert",
    }
    parts = value.split("+")
    tail = parts[-1]
    # Restrict printable single keys to letters/numbers; arbitrary X key expressions are not a tool.
    if any(part not in modifiers for part in parts[:-1]) or not (
        tail in keys
        or (len(tail) == 1 and tail.isascii() and tail.isalnum())
        or (tail.startswith("F") and tail[1:].isdigit() and 1 <= int(tail[1:]) <= 24)
    ):
        raise WorkerError("invalid_key", "Unsupported key combination", 422)
    return "+".join([modifiers[part] for part in parts[:-1]] + [keys.get(tail, tail)])


class X11:
    def __init__(self, display: str | None = None):
        self.display = display or os.environ.get("DISPLAY")
        self.available = False
        self.executable = None
        self.xprop = None
        self.xwininfo = None

    async def start(self):
        if sys.platform != "linux" or not self.display:
            raise WorkerError(
                "desktop_unavailable", "Desktop requires an enabled Linux X11 session", 503
            )
        self.executable = shutil.which("xdotool")
        if not self.executable:
            raise WorkerError(
                "desktop_unavailable", "Desktop requires the guest xdotool package", 503
            )
        self.xprop = shutil.which("xprop")
        self.xwininfo = shutil.which("xwininfo")
        await self.state()
        self.available = True

    async def close(self):
        self.available = False

    async def command(
        self,
        *arguments: str,
        input_text: str | None = None,
        timeout: float = 8.0,
        _executable: str | None = None,
    ):
        if not math.isfinite(timeout) or not 0 < timeout <= MAX_COMMAND_TIMEOUT:
            raise ValueError("Desktop command timeout must be positive and at most 104 seconds")
        environment = dict(os.environ, DISPLAY=self.display, LC_ALL="C")
        process = await asyncio.create_subprocess_exec(
            _executable or self.executable,
            *arguments,
            env=environment,
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=65_536,
        )

        async def exchange():
            if input_text is not None:
                process.stdin.write(input_text.encode())
                await process.stdin.drain()
                process.stdin.close()
            chunks = []
            size = 0
            while chunk := await process.stdout.read(min(8192, 65_537 - size)):
                size += len(chunk)
                if size > 65_536:
                    raise WorkerError(
                        "desktop_io_failed", "Desktop command output exceeded limit", 503
                    )
                chunks.append(chunk)
            output = b"".join(chunks)
            await process.wait()
            if process.returncode:
                raise WorkerError("desktop_io_failed", "Desktop command did not complete", 503)
            return output.decode("utf-8", errors="replace")

        try:
            return await asyncio.wait_for(exchange(), timeout)
        except BaseException:
            if process.returncode is None:
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise

    async def state(self) -> DisplayState:
        dimensions = (await self.command("getdisplaygeometry")).strip().split()
        if len(dimensions) != 2 or any(not value.isdecimal() for value in dimensions):
            raise WorkerError("desktop_io_failed", "Invalid desktop dimensions", 503)
        try:
            focus = (await self.command("getwindowfocus")).strip()
        except WorkerError:
            # Without a focused client, xdotool's WM_CLASS ancestor lookup can fail.
            # Recover only a verified native root focus; other lookup errors stay closed.
            topology = await self.desktop_topology()
            if (
                topology is None
                or int(topology.screen.focus) not in {0, 1, topology.root}
                or (topology.screen.width, topology.screen.height)
                != (int(dimensions[0]), int(dimensions[1]))
            ):
                raise
            return topology.screen
        if not focus.isdecimal():
            raise WorkerError("desktop_io_failed", "Desktop focus could not be read", 503)
        return DisplayState(int(dimensions[0]), int(dimensions[1]), focus)

    @staticmethod
    def parse_tree(output: str) -> tuple[int, tuple[tuple[str, int, int], ...]]:
        roots = re.findall(r"^\s*Root window id: (0x[0-9a-fA-F]+)\b", output, re.MULTILINE)
        if len(roots) != 1:
            raise ValueError("Unavailable X11 root")
        root = int(roots[0], 16)
        rows = []
        ids = {root}
        declared = 0
        for line in output.splitlines():
            count = re.fullmatch(r"(\s*)([0-9]+) child(?:ren)?:", line)
            if count is None:
                # xwininfo terminates an empty tree with a period, not a colon.
                count = re.fullmatch(r"(\s*)(0) children\.", line)
            if count:
                declared += int(count[2])
                rows.append(("children", len(count[1]), int(count[2])))
                continue
            child = re.match(r"^(\s+)(0x[0-9a-fA-F]+)\s", line)
            if child:
                key = int(child[2], 16)
                if key in ids or key == 0:
                    raise ValueError("Invalid X11 tree identity")
                ids.add(key)
                rows.append(("window", len(child[1]), key))
        if not rows or declared != len(ids) - 1 or len(ids) > MAX_TOPOLOGY_WINDOWS:
            raise ValueError("Incomplete or oversized X11 tree")
        return root, tuple(rows)

    @staticmethod
    def parse_geometry(window: int, output: str) -> WindowGeometry:
        headers = re.findall(r"^xwininfo: Window id: (0x[0-9a-fA-F]+)\b", output, re.MULTILINE)
        if len(headers) != 1 or int(headers[0], 16) != window:
            raise ValueError("X11 window identity changed")

        def field(label: str, pattern: str) -> str:
            values = re.findall(
                r"^\s*" + re.escape(label) + r":\s*(" + pattern + r")\s*$", output, re.MULTILINE
            )
            if len(values) != 1:
                raise ValueError("Incomplete X11 geometry")
            return values[0]

        bounds = tuple(
            int(field(key, r"-?[0-9]+"))
            for key in ("Absolute upper-left X", "Absolute upper-left Y", "Width", "Height")
        )
        if bounds[2] <= 0 or bounds[3] <= 0:
            raise ValueError("Invalid X11 geometry")
        return WindowGeometry(
            window,
            bounds,
            int(field("Border width", r"[0-9]+")),
            field("Class", "InputOutput|InputOnly"),
            field("Map State", "IsUnMapped|IsUnviewable|IsViewable"),
            field("Override Redirect State", "yes|no") == "yes",
        )

    async def desktop_topology(self) -> DesktopTopology | None:
        """Full desktop input context, including root focus and native overlays."""
        try:
            output = await self.command(
                "-m",
                "meerkat_computer.desktop_topology",
                _executable=sys.executable,
                timeout=MAX_VISUAL_FENCE_SECONDS,
            )
            return self.parse_desktop_topology(json.loads(output))
        except (WorkerError, ValueError, TimeoutError):
            return None

    @staticmethod
    def parse_desktop_topology(value) -> DesktopTopology:
        value = validate_snapshot(value)
        root, tree, windows, depths = value["root"], [], [], {}
        for window, parent, children, bounds, border, kind, mapped, override in value["nodes"]:
            depth = 0 if window == root else depths[parent] + 1
            depths[window] = depth
            if window != root:
                tree.append(("window", depth, window))
            tree.append(("children", depth, len(children)))
            windows.append(
                WindowGeometry(
                    window,
                    tuple(bounds),
                    border,
                    {1: "InputOutput", 2: "InputOnly"}[kind],
                    {0: "IsUnMapped", 1: "IsUnviewable", 2: "IsViewable"}[mapped],
                    bool(override),
                )
            )
        return DesktopTopology(
            DisplayState(*value["size"], str(value["focus"])), root, tuple(tree), tuple(windows)
        )

    async def capture_desktop_with_fence(self) -> tuple[bytes, DesktopTopology | None]:
        png = None
        try:
            async with asyncio.timeout(MAX_VISUAL_FENCE_SECONDS):
                before = await self.desktop_topology()
                png = await self.screenshot()
                after = await self.desktop_topology()
                return png, after if before is not None and before == after else None
        except TimeoutError:
            return png if png is not None else await self.screenshot(), None

    async def focused_topology(self) -> FocusedTopology | None:
        """Read only numeric X11 metadata, including unmanaged/InputOnly children."""
        if not self.xprop or not self.xwininfo:
            return None
        try:
            screen = await self.state()
            active = await self.command("-root", "_NET_ACTIVE_WINDOW", _executable=self.xprop)
            match = re.fullmatch(
                r"_NET_ACTIVE_WINDOW\(WINDOW\): window id # (0x[0-9a-fA-F]+)\s*", active
            )
            if match is None or int(match[1], 16) != int(screen.focus):
                return None
            tree_text = await self.command("-root", "-tree", _executable=self.xwininfo)
            root, tree = self.parse_tree(tree_text)
            ids = [root] + [value for kind, _, value in tree if kind == "window"]
            if int(screen.focus) not in ids or int(screen.focus) == root:
                return None
            semaphore = asyncio.Semaphore(8)

            async def geometry(window):
                async with semaphore:
                    output = await self.command(
                        "-id", hex(window), "-stats", _executable=self.xwininfo
                    )
                return self.parse_geometry(window, output)

            results = await asyncio.gather(
                *(geometry(window) for window in ids), return_exceptions=True
            )
            if any(isinstance(result, BaseException) for result in results):
                return None
            windows = tuple(results)
            if windows[0].bounds != (0, 0, screen.width, screen.height):
                return None
            focused = next(window for window in windows if window.window == int(screen.focus))
            if focused.window_class != "InputOutput" or focused.map_state != "IsViewable":
                return None
            x, y, width, height = focused.bounds
            left, top = max(0, x), max(0, y)
            right, bottom = min(screen.width, x + width), min(screen.height, y + height)
            if right <= left or bottom <= top:
                return None
            if await self.state() != screen:
                return None
            # A tree insertion during the awaited stats batch must not disappear
            # from the final topology check merely because focus stayed put.
            if await self.command("-root", "_NET_ACTIVE_WINDOW", _executable=self.xprop) != active:
                return None
            final_tree = await self.command("-root", "-tree", _executable=self.xwininfo)
            if self.parse_tree(final_tree) != (root, tree):
                return None
            return FocusedTopology(
                screen, tree, windows, focused.bounds, (left, top, right - left, bottom - top)
            )
        except (WorkerError, ValueError):
            # Private command output is never retained in a receipt or diagnostic.
            return None

    async def capture_with_fence(self) -> tuple[bytes, FocusedVisualFence | None]:
        png = None
        try:
            # Bound the optional inspection as a whole, including all child queries.
            # Cancelled subprocesses are killed and reaped by command().
            async with asyncio.timeout(MAX_VISUAL_FENCE_SECONDS):
                before = await self.focused_topology()
                png, digest = await self.capture(
                    before.visible_bounds if before is not None else None
                )
                after = await self.focused_topology()
                if before is None or before != after or digest is None:
                    return png, None
                return png, FocusedVisualFence(after, digest)
        except TimeoutError:
            # Observations can still use the ordinary screenshot-only path.
            return png if png is not None else await self.screenshot(), None

    async def screenshot(self) -> bytes:
        png, _ = await self.capture()
        return png

    async def capture(self, bounds=None) -> tuple[bytes, str | None]:
        def capture():
            import mss
            import mss.tools

            with mss.mss(display=self.display) as screen:
                monitor = screen.monitors[0]
                # Coordinate origin is fixed by the guest X server, not model supplied.
                if monitor["left"] != 0 or monitor["top"] != 0:
                    raise WorkerError(
                        "desktop_unavailable", "Desktop requires a zero-origin display", 503
                    )
                if monitor["width"] > 3840 or monitor["height"] > 2160:
                    raise WorkerError(
                        "desktop_unavailable", "Desktop exceeds supported dimensions", 503
                    )
                shot = screen.grab(monitor)
                rgb = shot.rgb
                digest = None
                if bounds is not None:
                    x, y, width, height = bounds
                    if x >= 0 and y >= 0 and x + width <= shot.width and y + height <= shot.height:
                        fingerprint = hashlib.sha256()
                        for row in range(y, y + height):
                            offset = (row * shot.width + x) * 3
                            fingerprint.update(rgb[offset : offset + width * 3])
                        digest = fingerprint.hexdigest()
                result = mss.tools.to_png(rgb, shot.size)
                if len(result) > MAX_SCREENSHOT_BYTES:
                    raise WorkerError(
                        "screenshot_too_large", "Desktop screenshot exceeds limit", 422
                    )
                return result, digest

        return await asyncio.to_thread(capture)

    async def move(self, x: int, y: int):
        # --sync can block forever if the pointer was already at its destination.
        await self.command("mousemove", str(x), str(y))

    async def click(self, x: int, y: int, button: str = "left", count: int = 1):
        await self.move(x, y)
        await self.command(
            "click",
            "--repeat",
            str(count),
            "--delay",
            "80",
            {"left": "1", "middle": "2", "right": "3"}[button],
        )

    async def type(self, text: str):
        # Text travels over stdin, never argv, logs, a clipboard or a shell.
        # Zero-delay repeated keys can share X11 timestamps and be lost by native apps.
        timeout = min(MAX_COMMAND_TIMEOUT, max(8.0, 5.0 + len(text) * 0.006))
        await self.command(
            "type",
            "--clearmodifiers",
            "--delay",
            "2",
            "--file",
            "-",
            input_text=text,
            timeout=timeout,
        )

    async def key(self, key: str):
        await self.command("key", "--clearmodifiers", x11_key(key))

    async def scroll(self, delta_x: float, delta_y: float):
        # X11 discrete wheel events approximate one notch per 120 requested units.
        for delta, negative, positive in ((delta_y, "4", "5"), (delta_x, "6", "7")):
            if delta:
                count = min(84, math.ceil(abs(delta) / 120))
                await self.command(
                    "click",
                    "--repeat",
                    str(count),
                    "--delay",
                    "10",
                    negative if delta < 0 else positive,
                )

    async def drag(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        button: str = "left",
        *,
        path: list[tuple[int, int]] | None = None,
    ):
        button_id = {"left": "1", "middle": "2", "right": "3"}[button]
        # Release even when cancellation interrupts the down acknowledgement itself.
        pressed = False
        try:
            async with asyncio.timeout(MAX_DRAG_PATH_SECONDS if path is not None else None):
                await self.move(*start)
                pressed = True
                await self.command("mousedown", button_id)
                previous = start
                for destination in [*(path or ()), end]:
                    for step in range(1, 9):
                        await self.move(
                            *(round(a + (b - a) * step / 8) for a, b in zip(previous, destination))
                        )
                        await asyncio.sleep(0.015)
                    previous = destination
        finally:
            if pressed:
                # Repeated cancellation must not release the worker lock while
                # this bounded button-up command is still in flight.
                release = asyncio.create_task(self.command("mouseup", button_id))
                cancelled = False
                while True:
                    try:
                        await asyncio.shield(release)
                        break
                    except asyncio.CancelledError:
                        if release.cancelled():
                            raise
                        cancelled = True
                if cancelled:
                    raise asyncio.CancelledError
