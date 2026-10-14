"""Run host installation against fake commands, with no changes to the host."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("module_failure", [False, True])
def test_install_requires_and_persists_bridge_netfilter(tmp_path, module_failure):
    fake = tmp_path / "bin"
    fake.mkdir()
    log = tmp_path / "calls.jsonl"
    shim = f"""#!{sys.executable}
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ['INSTALL_TEST_LOG'], 'a') as out:
    out.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
if name == 'id': print('0')
if name == 'modprobe' and os.environ['MODULE_FAIL'] == '1': sys.exit(1)
"""
    for name in ["id", "apt-get", "modprobe", "install"]:
        command = fake / name
        command.write_text(shim)
        command.chmod(0o755)
    # Model Ubuntu's release file without touching /etc on the test machine.
    bash_env = tmp_path / "bash-env"
    bash_env.write_text('source() { if [[ "$1" = /etc/os-release ]]; then ID=ubuntu; VERSION_ID=24.04; else builtin source "$@"; fi; }\n')
    result = subprocess.run(
        ["bash", str(ROOT / "infra/install-host.sh"), "--apply"],
        env={**os.environ, "PATH": str(fake) + os.pathsep + os.environ["PATH"],
             "INSTALL_TEST_LOG": str(log), "BASH_ENV": str(bash_env),
             "MODULE_FAIL": "1" if module_failure else "0"},
        capture_output=True, text=True, timeout=5, check=False,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    packages = next(call[3:] for call in calls if call[:3] == ["apt-get", "install", "-y"])
    assert {"git", "build-essential", "pkg-config", "kmod"} <= set(packages)
    load = ["modprobe", "br_netfilter"]
    assert load in calls, "Incus bridge filtering requires its host kernel module"
    if module_failure:
        assert result.returncode != 0
        assert "Installed host dependencies" not in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        installs = [call for call in calls if call[0] == "install"
                    and call[-1] == "/etc/modules-load.d/meerkat-computer.conf"]
        assert len(installs) == 1
        assert Path(installs[0][-2]).read_text() == "br_netfilter\n"
        assert calls.index(load) < calls.index(installs[0])
