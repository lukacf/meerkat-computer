"""Installer behavior with fake host commands; no system paths are modified."""

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_reinstall_restarts_running_service_and_checks_readiness(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    log = tmp_path / "calls.log"
    shim = f"""#!{sys.executable}
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ['INSTALL_TEST_LOG'], 'a') as out:
    out.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
if name == 'id': print('0')
if name == 'python3': sys.stdin.read()
"""
    for command in ["id", "getent", "useradd", "install", "python3", "systemctl"]:
        target = fake / command
        target.write_text(shim)
        target.chmod(0o755)
    binary = tmp_path / "rkat-computer"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    config = tmp_path / "config.toml"
    config.write_text('[server]\nlisten="127.0.0.1:18760"\n')
    result = subprocess.run(
        ["bash", str(ROOT / "infra/install-broker.sh"), str(binary), str(config), "--apply"],
        env={**os.environ, "PATH": str(fake) + os.pathsep + os.environ["PATH"],
             "INSTALL_TEST_LOG": str(log)},
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    restart = ["systemctl", "restart", "meerkat-computer.service"]
    assert restart in calls, "Updating installed files must restart an existing broker process"
    health = [index for index, call in enumerate(calls)
              if call[0] == "python3" and any("broker-readiness.py" in arg for arg in call)]
    assert health and health[0] > calls.index(restart), "Readiness must follow the service restart"


def readiness_module():
    path = ROOT / "infra/broker-readiness.py"
    spec = importlib.util.spec_from_file_location("broker_readiness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("response,status,expected", [
    ({"service": "meerkat-computer", "protocol_version": 1}, 200, True),
    ({"service": "another-service", "protocol_version": 1}, 200, False),
    ({"service": "meerkat-computer", "protocol_version": 2}, 200, False),
    ({"service": "meerkat-computer", "protocol_version": 1}, 503, False),
    ({"service": "meerkat-computer", "protocol_version": 1}, 302, False),
    ({"service": "meerkat-computer", "protocol_version": 1, "large": "x" * 5000}, 200, False),
])
def test_readiness_requires_bounded_correct_health_response(response, status, expected):
    module = readiness_module()
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(status)
            self.send_header("Location", "/redirect-target")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        assert module.wait_for_ready(f"127.0.0.1:{server.server_port}", timeout_seconds=0.15) is expected
        assert time.monotonic() - started < 1
        assert paths and set(paths) == {"/health"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("listen", ["example.com:80", "0.0.0.0:80", "10.0.0.2:80", "127.0.0.1:80/path"])
def test_readiness_rejects_non_loopback_or_malformed_target(listen):
    module = readiness_module()
    with pytest.raises(ValueError, match="loopback"):
        module.wait_for_ready(listen, timeout_seconds=0.01)
