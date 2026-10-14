#!/usr/bin/env python3
"""Exact-binary Ubuntu AppArmor userns permission inside a worker image builder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

PREFIX = "/opt/meerkat-computer/browsers/"


def render_profile(executable: str, index: int) -> str:
    if (
        not executable.startswith(PREFIX)
        or not re.fullmatch(r"[/A-Za-z0-9_.-]+", executable)
        or ".." in Path(executable).parts
        or Path(executable).name not in {"chrome", "chrome-headless-shell"}
        or type(index) is not int
        or not 0 <= index < 20
    ):
        raise ValueError("expected one exact installed Playwright Chromium executable")
    return (
        "abi <abi/4.0>,\ninclude <tunables/global>\n\n"
        f'profile meerkat-computer-chromium-{index} "{executable}" flags=(unconfined) {{\n'
        "  userns,\n}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(PREFIX)
    executables = sorted(
        path for path in root.rglob("*") if path.name in {"chrome", "chrome-headless-shell"}
    )
    if not executables:
        raise ValueError("no installed Playwright Chromium executable was found")
    for index, path in enumerate(executables):
        if not path.is_file() or path.is_symlink() or str(path.resolve()) != str(path):
            raise ValueError("browser must be a regular file at its exact path")
        with path.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                raise ValueError("browser must be an ELF executable")
        content = render_profile(str(path), index)
        target = Path(f"/etc/apparmor.d/meerkat-computer-chromium-{index}")
        if args.apply:
            if os.geteuid() != 0:
                raise PermissionError("profile installation requires guest root")
            if target.exists() and target.read_text() != content:
                raise ValueError("existing application profile differs")
            target.write_text(content)
            target.chmod(0o644)
            subprocess.run(["apparmor_parser", "--replace", str(target)], check=True, timeout=30)
        print(
            json.dumps(
                {
                    "executable": str(path),
                    "profile": str(target),
                    "profile_sha256": hashlib.sha256(content.encode()).hexdigest(),
                }
            )
        )


if __name__ == "__main__":
    main()
