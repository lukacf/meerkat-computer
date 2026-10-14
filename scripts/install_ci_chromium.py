#!/usr/bin/env python3
"""Install Chromium, retrying only apt lock contention for at most three minutes.

The CI job timeout bounds the installer itself. Never remove package-manager
locks, interrupt another owner, or retry tests and unrelated setup failures.
"""

import os
import re
import subprocess
import sys
import time


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
        if result.returncode == 0 or not apt_lock_failure(result.stdout):
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
