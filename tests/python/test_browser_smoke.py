"""Explicit real Chromium checks. No remote network or credentials required."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from meerkat_computer.protocol import Call, Lease, WorkerError
from meerkat_computer.worker import Worker

pytestmark = pytest.mark.browser
TOKEN = "browser-test-token-" + "0" * 32
HTML = b"""<!doctype html><html><body><h1>Local browser test</h1>
<label>Display name <input id="name" aria-label="Display name"></label>
<button onclick="document.getElementById('status').textContent='Clicked'; localStorage.setItem('clicked','yes')">Continue</button>
<p id="status">Waiting</p><p id="persist"></p>
<script>document.getElementById('persist').textContent=localStorage.getItem('clicked') || 'fresh';</script>
</body></html>"""


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def grant(epoch=1, mode="hybrid"):
    caps = (
        ["browser", "screenshots"]
        if mode == "visual"
        else ["browser", "screenshots", "browser_semantics"]
    )
    return Lease(
        lease_id=f"lease-{epoch}",
        epoch=epoch,
        expires_at_ms=9_000_000_000_000,
        capabilities=caps,
        observation={"mode": mode, "width": 640, "height": 480},
    )


def request(name, args=None, action_id="call", epoch=1):
    return Call(
        lease_id=f"lease-{epoch}", epoch=epoch, name=name, arguments=args or {}, action_id=action_id
    )


def observation(receipt):
    assert receipt[0] == 200, receipt
    return json.loads(receipt[1]["content"][0]["text"])


async def test_real_browser_observations_fencing_actions_visual_and_persistence(tmp_path, site):
    worker = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await worker.start()
    try:
        await worker.grant(grant())
        first = await worker.call(request("browser_navigate", {"url": site}, "navigate"))
        view = observation(first)
        assert first[1]["content"][1]["mimeType"] == "image/png"
        button = next(
            element["ref"] for element in view["elements"] if element["label"] == "Continue"
        )
        action = request(
            "computer_act",
            {"observation_id": view["observation_id"], "action": {"type": "click", "ref": button}},
            "click",
        )
        clicked = await worker.call(action)
        assert "Clicked" in observation(clicked)["accessibility"]
        assert clicked[1]["metrics"]["delta"]["clicks"] == 1
        assert await worker.call(action) == clicked
        with pytest.raises(WorkerError, match="Observe"):
            await worker.call(action.model_copy(update={"action_id": "replayed-old-observation"}))
        current = observation(await worker.call(request("computer_observe", action_id="observe")))
        await worker.browser.page.evaluate(
            "document.querySelector('button').textContent='Changed action outside tool'"
        )
        with pytest.raises(WorkerError, match="changed"):
            await worker.call(
                request(
                    "computer_act",
                    {
                        "observation_id": current["observation_id"],
                        "action": {"type": "key", "key": "Tab"},
                    },
                    "stale-dom",
                )
            )
        await worker.grant(grant(2, "visual"))
        visual = await worker.call(request("computer_observe", action_id="visual", epoch=2))
        visual_view = observation(visual)
        assert visual_view["mode"] == "visual"
        assert "elements" not in visual_view and "accessibility" not in visual_view
        assert "Continue" not in visual[1]["content"][0]["text"]
        assert visual[1]["content"][1]["type"] == "image"
        with pytest.raises(WorkerError):
            await worker.call(
                request(
                    "computer_act",
                    {
                        "observation_id": visual_view["observation_id"],
                        "action": {"type": "click", "ref": button},
                    },
                    "visual-ref",
                    epoch=2,
                )
            )
        await worker.grant(grant(3))
        tabs = await worker.call(
            request("browser_tabs", {"action": "new", "url": site}, "new-tab", epoch=3)
        )
        assert observation(tabs)["tab_id"] != view["tab_id"]
        listed = await worker.call(request("browser_tabs", {"action": "list"}, "tabs", epoch=3))
        assert len(json.loads(listed[1]["content"][0]["text"])["tabs"]) == 2
    finally:
        await worker.close()
    restarted = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await restarted.start()
    try:
        await restarted.grant(grant(4))
        persisted = observation(
            await restarted.call(request("browser_navigate", {"url": site}, "persist", epoch=4))
        )
        assert "yes" in persisted["accessibility"]
    finally:
        await restarted.close()


async def test_real_browser_validation_precedes_effect_admission(tmp_path, site):
    worker = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await worker.start()
    try:
        await worker.grant(grant())
        view = observation(await worker.call(request("browser_navigate", {"url": site}, "nav")))
        for index, action in enumerate(
            [
                {"type": "click", "x": -10, "y": 20},
                {"type": "key", "key": "UnrecognizedKey"},
                {"type": "drag", "x": 1, "y": 1, "end_x": 10000, "end_y": 2},
            ]
        ):
            with pytest.raises(WorkerError) as error:
                await worker.call(
                    request(
                        "computer_act",
                        {"observation_id": view["observation_id"], "action": action},
                        f"bad-{index}",
                    )
                )
            assert error.value.effect == "none"
        assert worker.journal.metrics()["tool_calls_admitted"] == 1
        field = next(item["ref"] for item in view["elements"] if item["label"] == "Display name")
        filled = observation(
            await worker.call(
                request(
                    "computer_act",
                    {
                        "observation_id": view["observation_id"],
                        "action": {"type": "fill", "ref": field, "text": "Meerkat"},
                    },
                    "fill",
                )
            )
        )
        assert any(item.get("value") == "Meerkat" for item in filled["elements"])
    finally:
        await worker.close()


async def test_acknowledged_click_is_counted_when_followup_observation_fails(tmp_path, site):
    worker = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN)
    await worker.start()
    try:
        await worker.grant(grant())
        view = observation(await worker.call(request("browser_navigate", {"url": site}, "nav")))
        button = next(item["ref"] for item in view["elements"] if item["label"] == "Continue")

        async def failed_observation():
            raise WorkerError("observation_failed", "Simulated capture failure")

        worker.browser.observe = failed_observation
        action = request(
            "computer_act",
            {"observation_id": view["observation_id"], "action": {"type": "click", "ref": button}},
            "click",
        )
        status, body = await worker.call(action)
        assert status == 409 and body["error"]["effect"] == "unknown"
        assert body["metrics"]["delta"]["clicks"] == 1
        assert body["metrics"]["delta"]["ui_commands"] == 1
        assert body["metrics"]["counts_are_lower_bounds"] is True
        assert worker.journal.metrics()["clicks"] == 1
        assert await worker.call(action) == (status, body)
        assert worker.journal.metrics()["clicks"] == 1
    finally:
        await worker.close()
