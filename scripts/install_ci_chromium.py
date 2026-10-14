#!/usr/bin/env python3
"""Install Chromium, retrying only apt lock contention for at most three minutes.

The CI job timeout bounds the installer itself. Never remove package-manager
locks, interrupt another owner, or retry tests and unrelated setup failures.
"""

import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

USERNS_RESTRICTION = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")


def render_sandbox_profile(executable):
    """Use the same exact-binary userns policy as infra/chromium-profile.py."""
    if (
        not re.fullmatch(r"/[A-Za-z0-9_./ -]+", executable)
        or any(part in {".", "..", ""} for part in executable.split("/")[1:])
        or Path(executable).name not in {"chrome", "chrome-headless-shell"}
    ):
        raise ValueError("expected one exact installed Chromium executable path")
    identifier = hashlib.sha256(executable.encode()).hexdigest()
    return (
        f'profile meerkat-computer-ci-chromium-{identifier} "{executable}" '
        "flags=(unconfined) {\n  userns,\n}\n"
    )


def installed_executables(output):
    """Read this installed Playwright version's dry-run inventory, not old caches."""
    locations = {}
    selected = None
    for line in output.splitlines():
        header = re.fullmatch(r".*\(playwright (chromium|chromium-headless-shell) v[0-9]+\)", line)
        if header:
            selected = header[1]
        elif line.startswith("  Install location:") and selected:
            if selected in locations:
                raise ValueError("duplicate Chromium install location")
            locations[selected] = Path(line.split(":", 1)[1].strip())
            selected = None
        elif line and not line.startswith(" "):
            selected = None
    if set(locations) != {"chromium", "chromium-headless-shell"}:
        raise ValueError("expected Chromium and headless-shell install locations")
    result = []
    for browser, name in (("chromium", "chrome"), ("chromium-headless-shell", "chrome-headless-shell")):
        root = locations[browser]
        if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
            raise ValueError("browser installation must be an exact directory")
        candidates = list(root.rglob(name))
        if len(candidates) != 1:
            raise ValueError("expected exactly one installed Chromium executable")
        executable = candidates[0]
        render_sandbox_profile(str(executable))
        if (
            not executable.is_file()
            or executable.is_symlink()
            or executable.resolve() != executable
            or not os.access(executable, os.X_OK)
        ):
            raise ValueError("browser must be executable at its exact regular-file path")
        with executable.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                raise ValueError("browser must be an ELF executable")
        result.append(str(executable))
    return result


def configure_sandbox():
    # Only disposable hosted jobs receive policy. Workstations and persistent
    # self-hosted runners retain their independently configured host policy.
    if (
        sys.platform != "linux"
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or not USERNS_RESTRICTION.exists()
    ):
        return 0
    restricted = USERNS_RESTRICTION.read_text().strip()
    if restricted == "0":
        return 0
    if restricted != "1":
        raise ValueError("unexpected AppArmor user namespace restriction value")
    inventory = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={**os.environ, "LC_ALL": "C"},
    )
    profiles = "abi <abi/4.0>,\ninclude <tunables/global>\n\n" + "\n".join(
        render_sandbox_profile(path) for path in installed_executables(inventory.stdout)
    )
    # apparmor_parser accepts stdin when no filename is supplied. --replace is
    # idempotent, and this job-local policy is not written to /etc or the repo.
    result = subprocess.run(
        ["sudo", "-n", "/usr/sbin/apparmor_parser", "--replace"],
        input=profiles,
        text=True,
        check=False,
        timeout=30,
    )
    return result.returncode


def apt_lock_failure(output):
    errors = [line.strip() for line in output.splitlines() if line.startswith("E:")]
    paths = r"/var/lib/(?:dpkg/lock(?:-frontend)?|apt/lists/lock)"
    held = rf"E: Could not get lock {paths}\. It is held by process [0-9]+ \([^\n]+\)"
    companion = (
        rf"E: Unable to acquire the dpkg frontend lock \({paths}\), "
        r"is another process using it\?"
        r"|E: Unable to lock directory /var/lib/apt/lists/"
        r"|E: Unable to lock the administration directory \(/var/lib/dpkg/\), "
        r"is another process using it\?"
    )
    return any(re.fullmatch(held, line) for line in errors) and all(
        re.fullmatch(held, line) or re.fullmatch(companion, line) for line in errors
    )


def install_chromium(*, lock_wait_seconds=180):
    deadline = time.monotonic() + lock_wait_seconds
    command = [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"]
    result = None
    while result is None or time.monotonic() < deadline:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
        print(result.stdout, end="", flush=True)
        if result.returncode == 0:
            return configure_sandbox()
        if not apt_lock_failure(result.stdout):
            return result.returncode
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        delay = min(5, remaining)
        print(f"APT lock busy; retrying dependency setup after {delay:g}s.", flush=True)
        time.sleep(delay)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(install_chromium())
