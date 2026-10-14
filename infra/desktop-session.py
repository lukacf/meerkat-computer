#!/usr/bin/env python3
"""Run one command in an owned, private X11 and AT-SPI desktop session."""

from __future__ import annotations

import argparse
import ast
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def dimensions(width: int, height: int) -> tuple[int, int]:
    if not 160 <= width <= 3840 or not 120 <= height <= 2160:
        raise ValueError("desktop dimensions must be within 160x120 and 3840x2160")
    return width, height


def session_environment(original: dict[str, str], runtime: Path, display: int) -> dict[str, str]:
    if not 1 <= display <= 65535:
        raise ValueError("display must be an integer in 1..65535")
    env = {
        key: value
        for key, value in original.items()
        if not key.startswith(("DBUS_", "AT_SPI_"))
        and key not in {"DISPLAY", "XAUTHORITY", "SESSION_MANAGER", "WAYLAND_DISPLAY"}
    }
    env.update(
        DISPLAY=f":{display}",
        XAUTHORITY=str(runtime / "Xauthority"),
        XDG_RUNTIME_DIR=str(runtime),
        DBUS_SESSION_BUS_ADDRESS="unix:path=" + str(runtime / "session-bus"),
        XDG_SESSION_TYPE="x11",
        XDG_CURRENT_DESKTOP="Openbox",
        DESKTOP_SESSION="openbox",
        NO_AT_BRIDGE="0",
        GTK_MODULES="gail:atk-bridge",
        # Fading overlay scrollbars can invalidate an otherwise unchanged visual input target.
        GTK_OVERLAY_SCROLLING="0",
        QT_LINUX_ACCESSIBILITY_ALWAYS_ON="1",
        GDK_BACKEND="x11",
        LIBGL_ALWAYS_SOFTWARE="1",
        GALLIUM_DRIVER="llvmpipe",
        SAL_USE_VCLPLUGIN="gtk3",
    )
    return env


def xvfb_command(env: dict[str, str], width: int, height: int) -> list[str]:
    dimensions(width, height)
    return [
        "Xvfb",
        env["DISPLAY"],
        "-screen",
        "0",
        f"{width}x{height}x24",
        "-nolisten",
        "tcp",
        "-auth",
        env["XAUTHORITY"],
        "-noreset",
    ]


def accessibility_address(reply: str) -> str:
    parsed = ast.literal_eval(reply)
    if (
        not isinstance(parsed, tuple)
        or len(parsed) != 1
        or not isinstance(parsed[0], str)
        or not parsed[0].startswith("unix:")
        or ";" in parsed[0]
    ):
        raise ValueError("AT-SPI must return one local Unix bus address")
    return parsed[0]


def tool_path(name: str) -> str:
    for directory in ("/usr/libexec", "/usr/lib/at-spi2-core"):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise RuntimeError(f"required accessibility executable is missing: {name}")


def kill_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def stop_processes(processes: list[subprocess.Popen], grace_seconds: float = 2) -> None:
    # Each managed child owns a process group. Include its descendants even when
    # the group leader has exited. The production service also owns their cgroup.
    for process in reversed(processes):
        kill_group(process, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    for process in reversed(processes):
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    for process in reversed(processes):
        kill_group(process, signal.SIGKILL)
    deadline = time.monotonic() + 1
    for process in reversed(processes):
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass


class Session:
    def __init__(self, env: dict[str, str], startup_seconds: float):
        self.env = env
        self.processes: list[subprocess.Popen] = []
        self.worker: subprocess.Popen | None = None
        self.deadline = time.monotonic() + startup_seconds
        self.stopping = False

    def spawn(self, command: list[str], *, worker: bool = False) -> subprocess.Popen:
        env = self.env.copy()
        if not worker:
            env.pop("MEERKAT_COMPUTER_WORKER_TOKEN", None)
        process = subprocess.Popen(command, env=env, start_new_session=True)
        self.processes.append(process)
        if worker:
            self.worker = process
        return process

    def close(self, worker_grace_seconds: float = 12, grace_seconds: float = 2) -> None:
        # Signal only the worker first. Its lifespan closes Chromium's persistent
        # context while the browser driver, X11 and DBus are still available.
        if self.worker is not None and self.worker.poll() is None:
            self.worker.terminate()
            try:
                self.worker.wait(timeout=worker_grace_seconds)
            except subprocess.TimeoutExpired:
                pass
        # Bound escalation and reap every owned group, including descendants of
        # an exited worker. The service cgroup is the final containment boundary.
        stop_processes(self.processes, grace_seconds)

    def check(self) -> None:
        if self.stopping:
            raise InterruptedError("desktop session stopped")
        if any(process.poll() is not None for process in self.processes):
            raise RuntimeError("a required desktop process exited")

    def command(self, argv: list[str], *, input: str | None = None) -> str:
        self.check()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("desktop session startup timed out")
        return subprocess.run(
            argv,
            env=self.env,
            input=input,
            text=True,
            capture_output=True,
            check=True,
            timeout=min(2, remaining),
        ).stdout.strip()

    def ready(self, argv: list[str], *, contains: str | None = None) -> str:
        while True:
            try:
                reply = self.command(argv)
                if contains is None or contains in reply:
                    return reply
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                self.check()
                if time.monotonic() >= self.deadline:
                    raise TimeoutError("desktop session startup timed out") from None
            time.sleep(0.05)

    def start(self, width: int, height: int) -> None:
        # The random cookie is sent on stdin, never exposed in process arguments.
        authority = Path(self.env["XAUTHORITY"])
        authority.touch(mode=0o600)
        self.command(
            ["xauth", "-f", str(authority), "-q"],
            input=f"add {self.env['DISPLAY']} MIT-MAGIC-COOKIE-1 {secrets.token_hex(16)}\n",
        )
        self.spawn(xvfb_command(self.env, width, height))
        self.ready(["xdpyinfo"])
        self.spawn(
            [
                "dbus-daemon",
                "--session",
                "--nofork",
                "--address=" + self.env["DBUS_SESSION_BUS_ADDRESS"],
            ]
        )
        self.ready(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.freedesktop.DBus",
                "--object-path",
                "/org/freedesktop/DBus",
                "--method",
                "org.freedesktop.DBus.ListNames",
            ]
        )
        self.spawn([tool_path("at-spi-bus-launcher"), "--launch-immediately"])
        self.ready(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.freedesktop.DBus",
                "--object-path",
                "/org/freedesktop/DBus",
                "--method",
                "org.freedesktop.DBus.NameHasOwner",
                "org.a11y.Bus",
            ],
            contains="true",
        )
        reply = self.ready(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.a11y.Bus",
                "--object-path",
                "/org/a11y/bus",
                "--method",
                "org.a11y.Bus.GetAddress",
            ]
        )
        self.env["AT_SPI_BUS_ADDRESS"] = accessibility_address(reply)
        self.command(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.a11y.Bus",
                "--object-path",
                "/org/a11y/bus",
                "--method",
                "org.freedesktop.DBus.Properties.Set",
                "org.a11y.Status",
                "IsEnabled",
                "<true>",
            ]
        )
        self.spawn([tool_path("at-spi2-registryd")])
        self.ready(
            [
                "gdbus",
                "call",
                "--address",
                self.env["AT_SPI_BUS_ADDRESS"],
                "--dest",
                "org.freedesktop.DBus",
                "--object-path",
                "/org/freedesktop/DBus",
                "--method",
                "org.freedesktop.DBus.NameHasOwner",
                "org.a11y.atspi.Registry",
            ],
            contains="true",
        )
        self.ready(
            [
                "gdbus",
                "call",
                "--address",
                self.env["AT_SPI_BUS_ADDRESS"],
                "--dest",
                "org.a11y.atspi.Registry",
                "--object-path",
                "/org/a11y/atspi/accessible/root",
                "--method",
                "org.a11y.atspi.Accessible.GetChildren",
            ]
        )
        self.spawn(["openbox", "--sm-disable"])
        # Window manager ownership, not merely X server readiness.
        self.ready(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"], contains="WINDOW")

    def run(self, command: list[str]) -> int:
        child = self.spawn(command, worker=True)
        while child.poll() is None and not self.stopping:
            if any(process.poll() is not None for process in self.processes[:-1]):
                raise RuntimeError("a required desktop process exited")
            time.sleep(0.05)
        return child.returncode if child.returncode is not None else 128 + signal.SIGTERM


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--width", type=int, default=int(os.getenv("MEERKAT_COMPUTER_WIDTH", "1280"))
    )
    parser.add_argument(
        "--height", type=int, default=int(os.getenv("MEERKAT_COMPUTER_HEIGHT", "720"))
    )
    parser.add_argument("--display", type=int, default=99)
    parser.add_argument("--startup-seconds", type=float, default=30)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    dimensions(args.width, args.height)
    if not command or not 1 <= args.startup_seconds <= 120:
        parser.error("a command and startup timeout in 1..120 seconds are required")
    # Never attach to a pre-existing desktop, including one owned by this user.
    if any(
        Path(path).exists()
        for path in (f"/tmp/.X{args.display}-lock", f"/tmp/.X11-unix/X{args.display}")
    ):
        raise RuntimeError("requested X11 display already exists; refusing to adopt it")
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="mc-desktop-", dir="/tmp") as directory:
        env = session_environment(dict(os.environ), Path(directory), args.display)
        session = Session(env, args.startup_seconds)

        def stop(signum, frame):
            session.stopping = True

        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            session.start(args.width, args.height)
            return session.run(command)
        finally:
            session.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, TimeoutError, InterruptedError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
