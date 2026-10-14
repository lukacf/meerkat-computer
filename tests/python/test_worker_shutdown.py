"""Real worker and supervisor shutdown with a persistent Chromium profile."""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.browser
ROOT = Path(__file__).resolve().parents[2]
TOKEN = "shutdown-test-token-" + "0" * 32
SUPERVISOR = """
import importlib.util
import sys

spec = importlib.util.spec_from_file_location('desktop_session', sys.argv[1])
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)
# The worker uses real headless Chromium. Only X11/DBus startup is unnecessary
# on the local test host; the supervisor's signal and shutdown paths are real.
desktop.Session.start = lambda self, width, height: None
sys.argv = [sys.argv[1], '--', *sys.argv[2:]]
sys.exit(desktop.main())
"""


@pytest.fixture
def worker_dir():
    # Leave room for the private Unix socket within the platform path limit.
    with tempfile.TemporaryDirectory(prefix="mc-stop-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def login_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (
                b'<form method="post"><button>Sign in</button></form>'
                if self.path == "/login"
                else b"Signed in"
                if "session=shutdown-proof" in self.headers.get("Cookie", "")
                else b"No session"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(303)
            self.send_header(
                "Set-Cookie",
                "session=shutdown-proof; Max-Age=86400; Path=/; HttpOnly; SameSite=Lax",
            )
            self.send_header("Location", "/session")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


async def ready(client, process):
    async with asyncio.timeout(15):
        while True:
            assert process.poll() is None, "worker exited before becoming ready"
            try:
                response = await client.get("/health")
                if response.is_success and response.json()["ready"]:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.05)


async def post(client, path, body):
    response = await client.post(path, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def call(client, name, arguments, action_id):
    body = await post(
        client,
        "/v1/call",
        {
            "lease_id": "shutdown-lease",
            "epoch": 1,
            "name": name,
            "arguments": arguments,
            "action_id": action_id,
        },
    )
    return json.loads(body["content"][0]["text"])


async def stop(process):
    process.send_signal(signal.SIGTERM)
    try:
        await asyncio.to_thread(process.wait, timeout=18)
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait, timeout=2)


@pytest.mark.parametrize(
    "desktop",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                os.environ.get("MEERKAT_COMPUTER_DESKTOP_TEST") != "1",
                reason="requires the isolated Linux desktop test image",
            ),
        ),
    ],
    ids=["headless", "desktop"],
)
@pytest.mark.parametrize(
    "inflight", [None, 1, 60, "private-body"], ids=["idle", "drained", "cancelled", "private-body"]
)
async def test_persistent_login_survives_supervisor_sigterm(
    worker_dir, login_site, desktop, inflight
):
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    supervisor = str(ROOT / "infra" / "desktop-session.py")
    command = (
        [sys.executable, supervisor, "--"]
        if desktop
        else [sys.executable, "-c", SUPERVISOR, supervisor]
    ) + [
        sys.executable,
        "-m",
        "meerkat_computer.worker",
        *(["--desktop"] if desktop else []),
        "--port",
        str(port),
        "--state-dir",
        str(worker_dir / "state"),
        "--workspace",
        str(worker_dir / "workspace"),
    ]
    env = dict(os.environ, MEERKAT_COMPUTER_WORKER_TOKEN=TOKEN)
    action = {
        "lease_id": "shutdown-lease",
        "epoch": 1,
        "name": "workspace_exec",
        "arguments": {
            "command": (
                f"printf started >> shutdown-effects; sleep {inflight}; "
                "printf finished >> shutdown-effects"
            ),
            "timeout_seconds": 120,
        },
        "action_id": "in-flight-effect",
    }
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": "Bearer " + TOKEN},
        timeout=10,
        trust_env=False,
    ) as client:
        for restart in (False, True):
            process = await asyncio.to_thread(subprocess.Popen, command, env=env)
            in_flight = None
            private_writer = None
            try:
                await ready(client, process)
                if not restart:
                    await post(
                        client,
                        "/v1/lease",
                        {
                            "lease_id": "shutdown-lease",
                            "epoch": 1,
                            "expires_at_ms": 9_000_000_000_000,
                            "capabilities": ["browser", "screenshots", "browser_semantics", "exec"],
                            "observation": {"mode": "hybrid", "width": 640, "height": 480},
                        },
                    )
                    view = await call(
                        client, "browser_navigate", {"url": login_site + "/login"}, "login"
                    )
                    button = next(x["ref"] for x in view["elements"] if x["label"] == "Sign in")
                    await call(
                        client,
                        "computer_act",
                        {
                            "observation_id": view["observation_id"],
                            "action": {"type": "click", "ref": button},
                        },
                        "submit",
                    )
                view = await call(
                    client,
                    "browser_navigate",
                    {"url": login_site + "/session"},
                    "after-restart" if restart else "before-restart",
                )
                assert "Signed in" in view["accessibility"]
                effects = worker_dir / "workspace" / "shutdown-effects"
                if inflight == "private-body" and not restart:
                    _, private_writer = await asyncio.open_unix_connection(
                        str(worker_dir / "state" / "secure-input.sock")
                    )
                    # A partial request must not consume the browser-close budget.
                    private_writer.write(
                        b"POST /v1/secure-input HTTP/1.1\r\nHost: localhost\r\n"
                        b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{"
                    )
                    await private_writer.drain()
                    await asyncio.sleep(0.1)
                elif isinstance(inflight, int):
                    if not restart:
                        in_flight = asyncio.create_task(client.post("/v1/call", json=action))
                        async with asyncio.timeout(5):
                            while not effects.exists():
                                if in_flight.done():
                                    pytest.fail(f"action did not start: {await in_flight}")
                                await asyncio.sleep(0.01)
                    else:
                        replay = await client.post("/v1/call", json=action)
                        if inflight == 60:
                            assert replay.status_code == 409, replay.text
                            assert replay.json()["error"]["effect"] == "unknown"
                            assert effects.read_text() == "started"
                        else:
                            assert replay.status_code == 200, replay.text
                            assert effects.read_text() == "startedfinished"
            finally:
                await stop(process)
                if in_flight is not None:
                    await asyncio.gather(in_flight, return_exceptions=True)
                if private_writer is not None:
                    private_writer.close()
                    await private_writer.wait_closed()
