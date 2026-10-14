"""Exercise the exact Rust guest bootstrap in an isolated disposable Linux guest."""

import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path


def main():
    assert sys.platform == "linux" and os.getuid() == 0
    source = Path("src/backend.rs").read_text()
    marker = 'const INSTALL_CREDENTIALS: &str = r#"'
    assert source.count(marker) == 1
    program = source.split(marker, 1)[1].split('"#;', 1)[0]
    payload = {
        "token": secrets.token_hex(32),
        "port": 18765,
        "width": 1280,
        "height": 720,
        "guest_admin": True,
    }

    def bootstrap():
        result = subprocess.run(
            [sys.executable, "-c", program],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, "Guest bootstrap failed (details intentionally not logged)"

    bootstrap()
    token_path = Path("/var/lib/meerkat-computer/worker.token")
    original_token = token_path.read_bytes()
    assert original_token.decode() == payload["token"]
    rule = Path("/etc/sudoers.d/meerkat-computer")
    assert stat.S_IMODE(rule.stat().st_mode) == 0o440 and rule.stat().st_uid == 0
    normal = subprocess.run(
        ["runuser", "-u", "meerkat-computer", "--", "id", "-u"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert int(normal.stdout) != 0
    elevated = subprocess.run(
        ["runuser", "-u", "meerkat-computer", "--", "sudo", "-n", "id", "-u"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert elevated.stdout.strip() == "0"
    payload["guest_admin"] = False
    bootstrap()
    assert not rule.exists() and token_path.read_bytes() == original_token
    denied = subprocess.run(
        ["runuser", "-u", "meerkat-computer", "--", "sudo", "-n", "id", "-u"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert denied.returncode != 0
    print(
        json.dumps(
            {
                "status": "passed",
                "scope": "Linux sudo guest bootstrap",
                "worker_uid": int(normal.stdout),
                "admin_uid": 0,
                "admin_revoked": True,
                "worker_credential_preserved": True,
                "production_bootstrap": "src/backend.rs:INSTALL_CREDENTIALS",
            }
        )
    )


if __name__ == "__main__":
    main()
