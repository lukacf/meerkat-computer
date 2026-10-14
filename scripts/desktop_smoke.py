"""Real native Linux qualification. This script only operates its private desktop.

The MCP binding bridge below is a test transport fixture. Broker authority is
covered by tests/stack.rs; this lane tests the production adapter and worker over
real HTTP/stdio against GTK, X11, AT-SPI, Chromium and installed desktop apps.
"""

import argparse
import asyncio
import base64
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from meerkat_computer.protocol import TOOL_ARGUMENTS

CAPABILITIES = [
    "browser",
    "desktop",
    "screenshots",
    "browser_semantics",
    "desktop_semantics",
    "files",
]
ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def observations(body):
    return [
        json.loads(block["text"])
        for block in body.get("content", [])
        if block["type"] == "text" and "observation_id" in json.loads(block["text"])
    ]


def observation(body):
    assert "error" not in body, body.get("error")
    return observations(body)[-1]


def node_named(view, name):
    found = [node for node in view.get("elements", []) if node.get("name") == name]
    assert found, (
        f"Native node missing: {name}; observed {[n.get('name') for n in view.get('elements', [])]}"
    )
    return found[0]


def has_name(view, name):
    return any(node.get("name") == name for node in view.get("elements", []))


def canvas_colors():
    """Sample the center of the private display, excluding window decorations."""
    import mss

    with mss.mss() as screen:
        frame = screen.grab({"left": 320, "top": 180, "width": 640, "height": 360})
        pixels = frame.rgb
        return len({pixels[index : index + 3] for index in range(0, len(pixels), 3 * 137)})


class Smoke:
    def __init__(self, client, lease, evidence, app_names, workspace):
        self.client, self.lease, self.evidence = client, lease, evidence
        self.app_names = app_names
        self.workspace = workspace
        self.events = []

    async def call(self, name, arguments, *, fail=False):
        response = await self.client.post(
            "/v1/call",
            json={
                "lease_id": self.lease["lease_id"],
                "epoch": self.lease["epoch"],
                "action_id": uuid.uuid4().hex,
                "name": name,
                "arguments": arguments,
            },
        )
        body = response.json()
        projection_samples = [
            {
                "surface": view.get("surface"),
                "json_bytes": len(json.dumps(view, ensure_ascii=True).encode()),
                **view["projection"],
            }
            for view in observations(body)
            if "projection" in view
        ]
        self.events.append(
            {
                "tool": name,
                "status": response.status_code,
                "metrics": body.get("metrics"),
                "error": body.get("error"),
                "projections": projection_samples,
            }
        )
        (self.evidence / "events.json").write_text(json.dumps(self.events, indent=2))
        if not fail:
            assert response.is_success, body.get("error", response.status_code)
        return body

    async def observe(self):
        try:
            body = await self.call("computer_observe", {"surface": "desktop"})
        except AssertionError:
            # Only this isolated fixture may expose private driver diagnostics.
            # Production HTTP deliberately excludes exception details.
            from meerkat_computer.desktop import Desktop
            from meerkat_computer.protocol import Lease

            driver = Desktop(self.evidence / "diagnostic-state")
            try:
                await driver.start()
                await driver.configure(Lease.model_validate(self.lease))
                await driver.observe()
            except Exception:  # noqa: BLE001 - isolated test captures otherwise-redacted driver diagnostics.
                (self.evidence / "driver-diagnostic.txt").write_text(traceback.format_exc())
            finally:
                await driver.close()
            raise
        view = observation(body)
        (self.evidence / "latest-observation.json").write_text(json.dumps(view, indent=2))
        return view

    async def wait_for(self, predicate, *, timeout=20):
        deadline = time.monotonic() + timeout
        while True:
            view = await self.observe()
            if predicate(view):
                return view
            assert time.monotonic() < deadline, "Native UI did not reach expected state"
            await asyncio.sleep(0.25)

    async def act(self, view, action, *, fail=False):
        return await self.call(
            "computer_act",
            {
                "observation_id": view["observation_id"],
                "action": action,
            },
            fail=fail,
        )

    async def startup_key(self, key):
        """Animated startup windows may invalidate capture before input admission."""
        deadline = time.monotonic() + 15
        while True:
            view = await self.observe()
            body = await self.act(view, {"type": "key", "key": key}, fail=True)
            if "error" not in body:
                return body
            error = body["error"]
            assert error.get("effect") == "none" and error.get("code") in {
                "stale_observation",
                "stale_ref",
            }, error
            assert time.monotonic() < deadline, "Startup never yielded a fresh input context"
            await asyncio.sleep(0.25)

    def save_screenshot(self, body, name):
        images = [block for block in body.get("content", []) if block["type"] == "image"]
        assert images, "Hybrid desktop observation must contain actual pixels"
        raw = base64.b64decode(images[-1]["data"], validate=True)
        assert raw.startswith(b"\x89PNG\r\n\x1a\n"), "Expected a PNG screenshot"
        (self.evidence / (name + ".png")).write_bytes(raw)

    async def native_fixture(self):
        view = await self.wait_for(lambda item: has_name(item, "Native message"))
        assert view["surface"] == "desktop"
        assert view["coordinate_space"] == "screen"
        assert view["accessibility"]["status"] in {"available", "partial"}
        assert len(view["applications"]) >= 1
        entry = node_named(view, "Native message")
        assert entry.get("app_id") and entry.get("bounds")
        assert view["projection"]["detail"] == "compact"
        assert view["projection"]["text_chars"] <= 6000
        expanded = observation(
            await self.call(
                "computer_observe",
                {
                    "surface": "desktop",
                    "detail": "full",
                    "scope_ref": entry["ref"],
                    "observation_id": view["observation_id"],
                },
            )
        )
        assert expanded["projection"]["detail"] == "full"
        assert len(expanded["elements"]) == 1
        assert expanded["observation_id"] != view["observation_id"]
        entry = node_named(expanded, "Native message")
        view = expanded
        focused = observation(await self.act(view, {"type": "focus", "ref": entry["ref"]}))
        entry = node_named(focused, "Native message")
        selected = observation(
            await self.act(
                focused,
                {
                    "type": "key",
                    "ref": entry["ref"],
                    "key": "Control+A",
                },
            )
        )
        entry = node_named(selected, "Native message")
        await self.act(
            selected,
            {
                "type": "type",
                "ref": entry["ref"],
                "text": "Hello from keyboard",
            },
        )
        focused = await self.wait_for(
            lambda item: any(
                node.get("name") == "Native message" and node.get("text") == "Hello from keyboard"
                for node in item.get("elements", [])
            )
        )
        entry = node_named(focused, "Native message")
        filled = observation(
            await self.act(
                focused,
                {
                    "type": "fill",
                    "ref": entry["ref"],
                    "text": "Hello from AT-SPI",
                },
            )
        )
        apply = node_named(filled, "Apply message")
        assert apply["actions"], "GTK button must expose native actions"
        await self.act(
            filled,
            {
                "type": "invoke",
                "ref": apply["ref"],
                "action_name": apply["actions"][0]["name"],
            },
        )
        view = await self.wait_for(lambda item: has_name(item, "Message: Hello from AT-SPI"))
        stale = await self.act(filled, {"type": "click", "ref": apply["ref"]}, fail=True)
        assert stale["error"]["code"] == "stale_observation"

        button = node_named(view, "Increment native count")
        bounds = button["bounds"]
        coordinate = await self.act(
            view,
            {
                "type": "click",
                "x": bounds["x"] + bounds["width"] / 2,
                "y": bounds["y"] + bounds["height"] / 2,
            },
        )
        self.save_screenshot(coordinate, "native-after-coordinate-click")
        assert coordinate["metrics"]["delta"]["clicks"] == 1
        view = await self.wait_for(lambda item: has_name(item, "Count: 1"))
        button = node_named(view, "Increment native count")
        await self.act(view, {"type": "click", "ref": button["ref"]})
        view = await self.wait_for(lambda item: has_name(item, "Count: 2"))
        menu = node_named(view, "Fixture")
        await self.act(view, {"type": "click", "ref": menu["ref"]})
        view = await self.wait_for(lambda item: has_name(item, "Open native dialog"))
        item = node_named(view, "Open native dialog")
        await self.act(view, {"type": "click", "ref": item["ref"]})
        view = await self.wait_for(lambda item: has_name(item, "Confirm native dialog"))
        dialog = node_named(view, "Confirm native dialog")
        assert dialog.get("window_id"), "Dialog must be associated with a native window"
        opened = await self.call("computer_observe", {"surface": "desktop"})
        self.save_screenshot(opened, "native-dialog")
        view = observation(opened)
        dialog = node_named(view, "Confirm native dialog")
        await self.act(view, {"type": "click", "ref": dialog["ref"]})
        await self.wait_for(lambda item: has_name(item, "Dialogs confirmed: 1"))
        return {
            "editable_text": True,
            "native_action": True,
            "coordinate_click": True,
            "active_window_ref_click": True,
            "active_window_ref_key_and_type": True,
            "menu_and_dialog": True,
            "stale_observation_refused": True,
            "compact_observation": True,
            "scoped_native_expansion_and_action": True,
        }

    async def blender_python(self, view):
        """Use Blender's built-in Python console through ordinary computer input."""
        blend_path = self.evidence / "blender-python.blend"
        receipt_path = self.evidence / "blender-python-receipt.json"
        script = (
            "import bpy, json; obj = bpy.data.objects['Cube']; "
            "obj.name = 'Meerkat Python Cube'; obj.scale = (1.8, 1.0, 0.6); "
            "obj.rotation_euler = (0.0, 0.0, 0.6); bpy.context.view_layer.update(); "
            f"bpy.ops.wm.save_as_mainfile(filepath={str(blend_path)!r}); "
            "json.dump({'name': obj.name, 'scale': list(obj.scale), "
            "'rotation_euler': list(obj.rotation_euler), 'blend_file': bpy.data.filepath}, "
            f"open({str(receipt_path)!r}, 'w'))"
        )
        (self.evidence / "blender-command.py").write_text(script + "\n")
        await self.call("workspace_write", {"path": "blender_scene.py", "text": script + "\n"})
        command = (
            "assert 'ee))' == chr(101)*2 + chr(41)*2; "
            f"exec(compile(open({str(self.workspace / 'blender_scene.py')!r}).read(), 'blender_scene.py', 'exec'))"
        )
        (self.evidence / "blender-console-command.txt").write_text(command + "\n")
        view = await self.observe()
        # The private fixture starts Blender with its default Layout workspace.
        # The observed screen dimensions place this click inside its 3D viewport.
        await self.act(
            view, {"type": "click", "x": view["width"] * 0.4, "y": view["height"] * 0.45}
        )
        view = await self.observe()
        await self.act(view, {"type": "key", "key": "Shift+F4"})
        await asyncio.sleep(0.5)
        console = await self.call("computer_observe", {"surface": "desktop"})
        self.save_screenshot(console, "blender-console-before-command")
        view = observation(console)
        await self.act(view, {"type": "type", "text": command})
        view = await self.observe()
        entered = await self.act(view, {"type": "key", "key": "Enter"}, fail=True)
        if "error" in entered:
            # Never repeat an uncertain command; the application's saved receipt
            # is independent evidence that its Python interpreter completed it.
            assert entered["error"]["effect"] == "unknown"
        console = await self.call("computer_observe", {"surface": "desktop"})
        self.save_screenshot(console, "blender-console-after-command")
        deadline = time.monotonic() + 20
        while not receipt_path.exists():
            assert time.monotonic() < deadline, "Blender Python command produced no receipt"
            await asyncio.sleep(0.25)
        receipt = json.loads(receipt_path.read_text())
        assert receipt["name"] == "Meerkat Python Cube"
        assert all(
            abs(actual - expected) < 1e-5
            for actual, expected in zip(receipt["scale"], (1.8, 1.0, 0.6), strict=True)
        )
        assert all(
            abs(actual - expected) < 1e-5
            for actual, expected in zip(receipt["rotation_euler"], (0.0, 0.0, 0.6), strict=True)
        )
        assert receipt["blend_file"] == str(blend_path)
        assert blend_path.stat().st_size > 1000
        with blend_path.open("rb") as saved:
            assert saved.read(7) == b"BLENDER"
        console = await self.call("computer_observe", {"surface": "desktop"})
        self.save_screenshot(console, "blender-python-console")
        await self.act(observation(console), {"type": "key", "key": "Shift+F5"})
        view = await self.observe()
        # Save the viewport editor selection too, so the artifact opens visually.
        await self.act(view, {"type": "key", "key": "Control+s"})
        await asyncio.sleep(0.5)
        changed = await self.call("computer_observe", {"surface": "desktop"})
        self.save_screenshot(changed, "blender-python")
        return {
            "entry": "built-in Python console via computer_act",
            "script_transport": "workspace_write",
            "repeated_character_probe": True,
            "receipt": receipt,
            "blend_bytes": blend_path.stat().st_size,
            "viewport_screenshot": True,
            "uncertain_enter_receipt": "error" in entered,
        }

    async def installed_apps(self):
        listed = await self.call("computer_apps", {"action": "list"})
        apps = json.loads(listed["content"][0]["text"])["applications"]
        selected = {}
        for wanted in ("blender", "inkscape", "gimp", "libreoffice"):
            matches = [app for app in apps if wanted in app["app_id"].lower()]
            assert matches, f"Installed desktop entry missing for {wanted}"
            selected[wanted] = matches[0]["app_id"]
        (self.evidence / "installed-apps.json").write_text(json.dumps(apps, indent=2))
        results = {}
        for app in self.app_names:
            launched = await self.call(
                "computer_apps",
                {
                    "action": "launch",
                    "app_id": selected[app],
                },
                fail=True,
            )
            uncertain_launch_receipt = "error" in launched
            if uncertain_launch_receipt:
                # A successful launch can race its returned observation. Preserve
                # that uncertainty and observe the existing process; never replay.
                assert launched["error"]["effect"] == "unknown"
                assert launched["metrics"]["delta"].get("application_launches") == 1
            else:
                submitted = json.loads(launched["content"][0]["text"])
                assert submitted["launch_submitted"] is True
            # X11 proves the visible native application exists even for custom
            # canvases that expose little or no accessibility tree.
            deadline = time.monotonic() + 60
            while True:
                windows = await asyncio.to_thread(
                    subprocess.run,
                    [
                        "xdotool",
                        "search",
                        "--onlyvisible",
                        "--class",
                        app,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if windows.returncode == 0 and windows.stdout.strip():
                    break
                assert time.monotonic() < deadline, f"No visible {app} window appeared"
                await asyncio.sleep(0.5)
            await asyncio.sleep(2)
            # These are explicit UI commands to the freshly observed application.
            # Dismiss first-run tips, then maximize the foreground native window.
            # This also gives GIMP a useful screenshot at the small test resolution.
            await self.startup_key("Escape")
            await self.startup_key("Alt+F10")
            if app == "blender":
                while await asyncio.to_thread(canvas_colors) < 24:
                    assert time.monotonic() < deadline, "Blender window has not rendered its canvas"
                    await asyncio.sleep(0.5)
            else:
                await asyncio.sleep(2)
            observed = await self.call("computer_observe", {"surface": "desktop"})
            view = observation(observed)
            window_moved_on_screen = False
            if app == "gimp":
                frame = next(
                    node
                    for node in view["elements"]
                    if node.get("role") == "frame"
                    and node.get("name") == "GNU Image Manipulation Program"
                )
                bounds = frame["bounds"]
                if bounds["y"] > 50:
                    # Openbox decorations are part of this Linux test fixture.
                    # Move the observed title bar into view using the public input
                    # tool instead of an out-of-band window-management command.
                    start_x = bounds["x"] + bounds["width"] / 3
                    new_x = max(0, (view["width"] - bounds["width"]) / 2)
                    await self.act(
                        view,
                        {
                            "type": "drag",
                            "x": start_x,
                            "y": bounds["y"] + 10,
                            "end_x": start_x + new_x - bounds["x"],
                            "end_y": 20,
                        },
                    )
                    await asyncio.sleep(0.5)
                    observed = await self.call("computer_observe", {"surface": "desktop"})
                    view = observation(observed)
                    moved = next(
                        node
                        for node in view["elements"]
                        if node.get("role") == "frame"
                        and node.get("name") == "GNU Image Manipulation Program"
                    )
                    assert moved["bounds"]["y"] < 50, (
                        "GIMP title-bar drag must move its UI on-screen"
                    )
                    window_moved_on_screen = True
            self.save_screenshot(observed, app)
            (self.evidence / (app + "-observation.json")).write_text(json.dumps(view, indent=2))
            aliases = {app, "soffice"} if app == "libreoffice" else {app}
            native_apps = [
                entry
                for entry in view.get("applications", [])
                if any(alias in entry.get("name", "").lower() for alias in aliases)
            ]
            app_ids = {entry["app_id"] for entry in native_apps}
            elements = [node for node in view.get("elements", []) if node.get("app_id") in app_ids]
            if app in {"inkscape", "libreoffice"}:
                assert elements, f"{app} must expose native accessibility elements"
            python_result = await self.blender_python(view) if app == "blender" else None
            close_commands = 0
            while close_commands < 3:
                active = await asyncio.to_thread(
                    subprocess.run,
                    [
                        "xdotool",
                        "getactivewindow",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                remaining = await asyncio.to_thread(
                    subprocess.run,
                    [
                        "xdotool",
                        "search",
                        "--onlyvisible",
                        "--class",
                        app,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if remaining.returncode != 0 or not remaining.stdout.strip():
                    break
                assert active.stdout.strip() in remaining.stdout.splitlines(), (
                    f"Refusing to close an unrelated foreground window while qualifying {app}"
                )
                view = await self.observe()
                key = await self.act(view, {"type": "key", "key": "Alt+F4"})
                assert key["metrics"]["delta"]["ui_commands"] >= 1
                close_commands += 1
                await asyncio.sleep(1)
            remaining = await asyncio.to_thread(
                subprocess.run,
                [
                    "xdotool",
                    "search",
                    "--onlyvisible",
                    "--class",
                    app,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert remaining.returncode != 0 or not remaining.stdout.strip(), (
                f"Keyboard did not close the {app} window"
            )
            closed = await self.call("computer_observe", {"surface": "desktop"})
            self.save_screenshot(closed, app + "-after-keyboard-close")
            results[app] = {
                "visible_windows": len(windows.stdout.splitlines()),
                "accessible_elements": len(elements),
                "accessibility_status": view["accessibility"]["status"],
                "accessibility_truncated": view["accessibility"]["truncated"],
                "screenshot": True,
                "keyboard_fallback_closed_window": True,
                "close_commands": close_commands,
                "window_moved_on_screen": window_moved_on_screen,
                "uncertain_launch_receipt": uncertain_launch_receipt,
            }
            if python_result is not None:
                results[app]["python"] = python_result
            (self.evidence / "app-results.json").write_text(json.dumps(results, indent=2))
        return {"entries": selected, "launched": results}


async def mcp_surface_flow(smoke, bridge_url, binding):
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "meerkat_computer.mcp_adapter",
            "--broker-url",
            bridge_url,
            "--binding-file",
            str(binding),
        ],
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        assert {"computer_apps", "computer_observe"} <= {tool.name for tool in listed.tools}
        native = await session.call_tool("computer_observe", {"surface": "desktop"})
        assert not native.isError
        native_view = next(
            json.loads(block.text)
            for block in native.content
            if block.type == "text" and "observation_id" in json.loads(block.text)
        )
        browser = await session.call_tool("browser_navigate", {"url": bridge_url + "/fixture"})
        assert not browser.isError
        browser_view = json.loads(
            next(block.text for block in browser.content if block.type == "text")
        )
        assert browser_view["surface"] == "browser"
        stale = await session.call_tool(
            "computer_act",
            {
                "observation_id": native_view["observation_id"],
                "action": {"type": "key", "key": "Escape"},
            },
        )
        assert stale.isError and "stale_observation" in stale.content[0].text
        button = next(item for item in browser_view["elements"] if item["label"] == "Browser count")
        clicked = await session.call_tool(
            "computer_act",
            {
                "observation_id": browser_view["observation_id"],
                "action": {"type": "click", "ref": button["ref"]},
            },
        )
        assert not clicked.isError
        assert clicked.structuredContent["metrics"]["delta"]["clicks"] == 1
        after = await session.call_tool("computer_observe", {"surface": "desktop"})
        assert not after.isError and any(block.type == "image" for block in after.content)
        invalid_browser = await session.call_tool(
            "computer_act",
            {
                "observation_id": browser_view["observation_id"],
                "action": {"type": "click", "ref": button["ref"]},
            },
        )
        assert invalid_browser.isError
    return {
        "production_stdio_adapter": True,
        "binding_host": "test_transport_fixture",
        "browser_native_switch": True,
        "cross_surface_refs_fenced": True,
    }


async def wait_ready(client, process):
    deadline = time.monotonic() + 60
    while True:
        assert process.returncode is None, "Worker exited before readiness; inspect worker.log"
        try:
            response = await client.get("/health")
            if response.is_success and response.json().get("ready"):
                return
        except httpx.HTTPError:
            pass
        assert time.monotonic() < deadline, "Worker readiness timeout"
        await asyncio.sleep(0.2)


def bridge_app(smoke, control_token):
    app = FastAPI()

    @app.get("/fixture")
    async def fixture():
        return HTMLResponse(
            "<button onclick=\"this.textContent='Browser count 1'\">Browser count</button>"
        )

    @app.get("/v1/tools")
    async def tools(request: Request):
        assert request.headers.get("authorization") == "Bearer " + control_token
        return {
            "tools": [
                {
                    "name": name,
                    "description": "Native smoke transport fixture",
                    "inputSchema": model.model_json_schema(),
                }
                for name, (_, model) in TOOL_ARGUMENTS.items()
            ]
        }

    @app.post("/v1/tools/call")
    async def call(request: Request):
        assert request.headers.get("authorization") == "Bearer " + control_token
        body = await request.json()
        body.update(lease_id=smoke.lease["lease_id"], epoch=smoke.lease["epoch"])
        response = await smoke.client.post("/v1/call", json=body)
        return JSONResponse(response.json(), status_code=response.status_code)

    return app


async def run(evidence, app_names):
    assert sys.platform == "linux" and os.getuid() != 0, "Run as a non-root Linux user"
    assert os.environ.get("DISPLAY") and os.environ.get("AT_SPI_BUS_ADDRESS")
    evidence.mkdir(parents=True, exist_ok=True)
    version_result = await asyncio.to_thread(
        subprocess.run,
        [
            "dpkg-query",
            "-W",
            "-f=${Package}\t${Version}\n",
            "blender",
            "inkscape",
            "gimp",
            "libreoffice-writer",
            "libreoffice-gtk3",
            "at-spi2-core",
            "xvfb",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    (evidence / "package-versions.txt").write_text(version_result.stdout)
    token, control_token = secrets.token_hex(32), secrets.token_hex(32)
    worker_port, bridge_port = free_port(), free_port()
    processes, logs = [], []
    bridge_server = bridge_task = None
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="desktop-smoke-") as directory:
        state = Path(directory)
        try:
            for name, argv, env in [
                (
                    "worker",
                    [
                        sys.executable,
                        "-m",
                        "meerkat_computer.worker",
                        "--desktop",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(worker_port),
                        "--state-dir",
                        str(state / "worker"),
                        "--workspace",
                        str(state / "workspace"),
                    ],
                    {**os.environ, "MEERKAT_COMPUTER_WORKER_TOKEN": token},
                ),
            ]:
                log = (evidence / (name + ".log")).open("w")
                logs.append(log)
                processes.append(
                    await asyncio.create_subprocess_exec(
                        *argv,
                        env=env,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                    )
                )
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{worker_port}",
                headers={"Authorization": "Bearer " + token},
                timeout=160,
                trust_env=False,
            ) as client:
                await wait_ready(client, processes[-1])
                fixture_log = (evidence / "fixture.log").open("w")
                logs.append(fixture_log)
                processes.append(
                    await asyncio.create_subprocess_exec(
                        sys.executable,
                        str(ROOT / "tests/desktop/native_fixture.py"),
                        stdout=fixture_log,
                        stderr=fixture_log,
                        start_new_session=True,
                    )
                )
                lease = {
                    "lease_id": str(uuid.uuid4()),
                    "epoch": 1,
                    "expires_at_ms": int(time.time() * 1000) + 900_000,
                    "capabilities": CAPABILITIES,
                    "observation": {
                        "surface": "desktop",
                        "mode": "hybrid",
                        "width": 1280,
                        "height": 720,
                    },
                }
                response = await client.post("/v1/lease", json=lease)
                assert response.is_success, response.text
                smoke = Smoke(client, lease, evidence, app_names, state / "workspace")
                result = {
                    "scope": "real Linux native worker and MCP transport",
                    "uid": os.getuid(),
                    "gtk": await smoke.native_fixture(),
                }
                binding = state / "binding.json"
                binding.write_text(json.dumps({"token": control_token}))
                binding.chmod(0o600)
                bridge_server = uvicorn.Server(
                    uvicorn.Config(
                        bridge_app(smoke, control_token),
                        host="127.0.0.1",
                        port=bridge_port,
                        log_level="warning",
                        access_log=False,
                    )
                )
                bridge_task = asyncio.create_task(bridge_server.serve())
                while not bridge_server.started:
                    await asyncio.sleep(0.05)
                result["mcp"] = await mcp_surface_flow(
                    smoke, f"http://127.0.0.1:{bridge_port}", binding
                )
                result["apps"] = await smoke.installed_apps()
                revoke = {
                    **lease,
                    "lease_id": str(uuid.uuid4()),
                    "epoch": 2,
                    "expires_at_ms": 0,
                    "capabilities": [],
                }
                assert (await client.post("/v1/lease", json=revoke)).is_success
                stale = await smoke.call("computer_observe", {"surface": "desktop"}, fail=True)
                assert stale["error"]["code"] == "stale_lease"
                assert (await client.post("/v1/lease", json=lease)).status_code == 409
                result["grant_fencing"] = True
                result["duration_seconds"] = round(time.monotonic() - started, 3)
                result["status"] = "passed"
                (evidence / "report.json").write_text(json.dumps(result, indent=2))
                print(json.dumps(result, indent=2))
        finally:
            if bridge_server is not None:
                bridge_server.should_exit = True
            if bridge_task is not None:
                await bridge_task
            for process in reversed(processes):
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            for process in reversed(processes):
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            for log in logs:
                log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument(
        "--apps",
        nargs="+",
        choices=("inkscape", "gimp", "libreoffice", "blender"),
        default=("inkscape", "gimp", "libreoffice", "blender"),
    )
    args = parser.parse_args()
    asyncio.run(run(args.evidence, args.apps))
