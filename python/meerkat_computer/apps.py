"""Discover and launch applications registered by the guest image."""

import asyncio
import configparser
import os
from pathlib import Path

from .protocol import WorkerError

DESKTOP_ENVIRONMENT = (
    "DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "AT_SPI_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    "NO_AT_BRIDGE",
    "GTK_MODULES",
    "XDG_CURRENT_DESKTOP",
    "QT_ACCESSIBILITY",
    "QT_LINUX_ACCESSIBILITY_ALWAYS_ON",
    "SAL_USE_VCLPLUGIN",
    "LIBGL_ALWAYS_SOFTWARE",
)


def desktop_environment() -> dict[str, str]:
    """Guest display/session wiring only, never controller or worker credentials."""
    return {name: os.environ[name] for name in DESKTOP_ENVIRONMENT if name in os.environ}


class Applications:
    def __init__(self, roots=None):
        self.roots = roots or (
            Path("/usr/local/share/applications"),
            Path("/usr/share/applications"),
        )
        self.children = set()

    def entries(self) -> dict:
        entries = {}
        for root in self.roots:
            for path in sorted(root.glob("*.desktop")):
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 131072:
                    continue
                parser = configparser.ConfigParser(interpolation=None, strict=False)
                try:
                    parser.read(path, encoding="utf-8")
                    entry = parser["Desktop Entry"]
                    if entry.get("Type") != "Application" or not entry.get("Name"):
                        continue
                    if any(
                        entry.getboolean(key, fallback=False)
                        for key in ("Hidden", "NoDisplay", "Terminal")
                    ):
                        continue
                    if not entry.get("Exec") and not entry.getboolean(
                        "DBusActivatable", fallback=False
                    ):
                        continue
                    app_id = path.name
                    entries.setdefault(
                        app_id,
                        {
                            "app_id": app_id,
                            "name": entry["Name"][:256],
                            "description": entry.get("Comment", "")[:512],
                            "categories": entry.get("Categories", "").split(";")[:12],
                            "path": path,
                        },
                    )
                except (OSError, ValueError, configparser.Error, KeyError):
                    continue
        return entries

    def list(self) -> dict:
        values = list(self.entries().values())
        return {
            "applications": [
                {k: v for k, v in entry.items() if k != "path"} for entry in values[:200]
            ],
            "truncated": len(values) > 200,
        }

    def resolve(self, app_id: str) -> Path:
        entry = self.entries().get(app_id)
        if entry is None:
            raise WorkerError(
                "unknown_application", "Choose an installed app_id from computer_apps list", 422
            )
        return entry["path"]

    async def launch(self, app_id: str):
        path = self.resolve(app_id)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(Path.home()),
            "LANG": "C.UTF-8",
            **desktop_environment(),
        }
        # GIO owns Desktop Entry Exec parsing and activation. Model arguments
        # never become a shell command or arbitrary executable path.
        process = await asyncio.create_subprocess_exec(
            "gio",
            "launch",
            str(path),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        task = asyncio.create_task(process.wait())
        self.children.add(task)
        task.add_done_callback(self.children.discard)
        try:
            code = await asyncio.wait_for(asyncio.shield(task), 5)
        except TimeoutError:
            # Some launchers remain until the window closes. Admission is known,
            # window readiness must come from a subsequent observation.
            return
        if code != 0:
            raise WorkerError(
                "application_launch_unknown",
                "Application launch was not acknowledged",
                effect="unknown",
            )
