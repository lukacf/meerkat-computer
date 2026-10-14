"""Rejected input has a durable receipt, without claiming that input was dispatched."""

import json
import sqlite3
from unittest.mock import AsyncMock

import httpx
import pytest
from meerkat_computer.client import EffectUnknown
from meerkat_computer.mcp_adapter import BrokerAdapter
from meerkat_computer.protocol import STALE_INPUT_REASONS, WorkerError
from meerkat_computer.worker import Worker, create_app
from test_client import Broker as ClientBroker
from test_worker import TOKEN, FakeBrowser, call, lease


def test_worker_error_reasons_are_optional_internal_and_bounded():
    default = WorkerError("stale_observation", "Observe again")
    assert default.reasons == ()
    error = WorkerError(
        "stale_observation",
        "Observe again",
        reasons=("focus", "focus", "private-app-title", 17, ["secret"], "visual_pixels"),
    )
    assert error.reasons == ("focus", "visual_pixels")
    assert error.response() == default.response()
    assert "private-app-title" not in repr(error)
    with pytest.raises(TypeError):
        WorkerError("stale_observation", "Observe again", 409, "none", ("focus",))


@pytest.mark.parametrize("reasons", [None, "focus", {"focus": "private"}, ["focus"]])
def test_invalid_worker_error_reason_container_is_ignored(reasons):
    assert WorkerError("stale_ref", "Observe again", reasons=reasons).reasons == ()


@pytest.fixture
async def worker(tmp_path):
    instance = Worker(
        tmp_path / "state", tmp_path / "workspace", TOKEN, FakeBrowser(), clock=lambda: 1000
    )
    await instance.start()
    await instance.grant(lease())
    yield instance
    await instance.close()


def input_call(**kwargs):
    return call(
        "computer_act",
        {"observation_id": "observed", "action": {"type": "click", "x": 10, "y": 10}},
        **kwargs,
    )


async def http_call(worker, request, *, authorization="Bearer " + TOKEN):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(worker)), base_url="http://worker"
    ) as client:
        return await client.post(
            "/v1/call", json=request.model_dump(), headers={"Authorization": authorization}
        )


async def test_http_rejection_has_fresh_metrics_and_replay_is_not_counted(worker):
    error = WorkerError("stale_observation", "Observe again", reasons=("focus",))
    # A prior successful input must not leak into this rejection's delta.
    worker.browser.operation_metrics = {"clicks": 7, "ui_commands": 7}
    worker.computer.current_target = worker.browser
    worker.computer.local_metrics = {"ui_command_attempts": 7}
    worker.journal.increment("clicks", 7)
    worker.computer.validate = AsyncMock(side_effect=error)
    request = input_call()
    response = await http_call(worker, request)
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == {"code": "stale_observation", "message": "Observe again", "effect": "none"}
    assert body["metrics"]["delta"] == {"ui_input_rejections": 1, "stale_input_focus": 1}
    assert body["metrics"]["action_id"] == request.action_id
    assert body["metrics"]["duration_ms"] >= 0
    assert body["metrics"]["totals"] == {
        "clicks": 7, "ui_input_rejections": 1, "stale_input_focus": 1,
    }
    assert worker.browser.calls == 0
    assert worker.journal.receipt(request) == (409, body)
    duplicate = await http_call(worker, request)
    assert duplicate.json() == body
    assert worker.computer.validate.await_count == 1
    assert worker.journal.metrics() == body["metrics"]["totals"]
    changed = request.model_copy(update={"arguments": {
        "observation_id": "different", "action": {"type": "click", "x": 10, "y": 10},
    }})
    assert (await http_call(worker, changed)).json()["error"]["code"] == "action_id_conflict"
    assert worker.journal.metrics() == body["metrics"]["totals"]


async def test_rejection_receipt_and_totals_survive_worker_restart(tmp_path):
    state, workspace = tmp_path / "state", tmp_path / "workspace"
    first = Worker(state, workspace, TOKEN, FakeBrowser(), clock=lambda: 1000)
    await first.start()
    await first.grant(lease())
    first.computer.validate = AsyncMock(side_effect=WorkerError(
        "stale_ref", "Observe again", reasons=("native_target", "native_window"),
    ))
    try:
        response = await http_call(first, input_call())
        assert response.status_code == 409
        body = response.json()
        assert body["metrics"]["delta"]["ui_input_rejections"] == 1
    finally:
        await first.close()
    restarted = Worker(state, workspace, TOKEN, FakeBrowser(), clock=lambda: 1000)
    await restarted.start()
    restarted.computer.validate = AsyncMock(side_effect=AssertionError("must replay receipt"))
    try:
        assert (await http_call(restarted, input_call())).json() == body
        assert restarted.journal.metrics() == body["metrics"]["totals"]
        restarted.computer.validate.assert_not_awaited()
        assert restarted.browser.calls == 0
    finally:
        await restarted.close()


@pytest.mark.parametrize("table,condition", [
    ("actions", "NEW.action_id = 'a'"),
    ("metrics", "NEW.name = 'stale_input_focus'"),
])
async def test_rejection_receipt_and_counters_roll_back_together(worker, table, condition):
    worker.computer.validate = AsyncMock(side_effect=WorkerError(
        "stale_observation", "Observe again", reasons=("focus",),
    ))
    worker.journal.db.executescript(
        f"CREATE TRIGGER fail_rejection BEFORE INSERT ON {table} WHEN {condition} "
        "BEGIN SELECT RAISE(ABORT, 'injected journal failure'); END;"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected journal failure"):
        await http_call(worker, input_call())
    assert worker.journal.metrics() == {}
    assert worker.journal.receipt(input_call()) is None
    assert worker.browser.calls == 0


async def test_default_and_mutated_unknown_reasons_cannot_create_counter_names(worker):
    error = WorkerError("stale_ref", "Observe again")
    worker.computer.validate = AsyncMock(side_effect=error)
    first = await http_call(worker, input_call())
    assert first.json()["metrics"]["delta"] == {"ui_input_rejections": 1}
    error.reasons = ("focus", "focus", "private-window-title", ["private"], 42)
    second = await http_call(worker, input_call(action_id="second"))
    assert second.json()["metrics"]["delta"] == {
        "ui_input_rejections": 1, "stale_input_focus": 1,
    }
    assert "private" not in second.text
    assert worker.journal.metrics() == {"ui_input_rejections": 2, "stale_input_focus": 1}


async def test_privacy_guard_rejection_is_counted_before_effect_admission(worker):
    worker.secure.guarded = AsyncMock(return_value=True)
    worker.computer.surface_for = lambda *_: "desktop"
    response = await http_call(worker, input_call())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "privacy_guarded"
    assert response.json()["metrics"]["delta"] == {"ui_input_rejections": 1}
    assert worker.journal.metrics() == {"ui_input_rejections": 1}
    assert worker.browser.calls == 0


@pytest.mark.parametrize("failure", ["auth", "lease", "schema"])
async def test_untrusted_or_invalid_input_is_not_rejection_telemetry(worker, failure):
    request, authorization = input_call(), "Bearer " + TOKEN
    if failure == "auth":
        authorization = "Bearer invalid"
    elif failure == "lease":
        request.epoch += 1
    else:
        request.arguments["unexpected"] = "private"
    response = await http_call(worker, request, authorization=authorization)
    assert response.status_code >= 400
    assert "metrics" not in response.json()
    assert worker.journal.metrics() == {}
    assert worker.journal.receipt(request) is None


async def test_post_admission_failure_remains_unknown_and_not_a_rejection(worker):
    worker.computer.validate = AsyncMock()
    worker.computer.perform = AsyncMock(side_effect=WorkerError(
        "stale_observation", "Observe again", reasons=("focus",),
    ))
    response = await http_call(worker, input_call())
    body = response.json()
    assert body["error"]["effect"] == "unknown"
    assert body["metrics"]["counts_are_lower_bounds"] is True
    assert "ui_input_rejections" not in body["metrics"]["totals"]
    assert "stale_input_focus" not in body["metrics"]["totals"]
    assert worker.journal.metrics() == {
        "tool_calls_admitted": 1, "tool_calls_failed": 1, "uncertain_effects": 1,
    }
    assert (await http_call(worker, input_call())).json() == body


@pytest.mark.parametrize("unknown", [False, True])
def test_sdk_preserves_only_known_rejection_and_fallback_counters(unknown):
    broker = ClientBroker()
    try:
        grant = broker.attach()
        allowed = {"ui_input_rejections": 1, "observation_fallbacks": 2}
        allowed.update({"stale_input_" + reason: 1 for reason in STALE_INPUT_REASONS})
        raw = {**allowed, "stale_input_private-window-title": 1, "private": "secret"}
        metrics = {"delta": raw, "totals": raw, "diagnostic": "private-window-title"}
        broker.reply = httpx.Response(409, json={
            "error": {"code": "conflict", "effect": "unknown" if unknown else "none"},
            "metrics": metrics,
        })
        if unknown:
            with pytest.raises(EffectUnknown) as caught:
                broker.client.call_tool(grant, "computer_act", {}, "rejected")
            result = caught.value.metrics
        else:
            receipt = broker.client.call_tool(grant, "computer_act", {}, "rejected")
            assert receipt.isError
            result = receipt.structuredContent["metrics"]
            assert json.loads(receipt.content[0].text)["metrics"] == result
        assert result == {"delta": allowed, "totals": allowed}
    finally:
        broker.client.close()


async def test_mcp_receipt_preserves_rejection_metrics_without_extra_requests():
    metrics = {"delta": {"ui_input_rejections": 1, "stale_input_visual_pixels": 1}}
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(409, json={
            "error": {"code": "conflict", "effect": "none"}, "metrics": metrics,
        })

    async with httpx.AsyncClient(
        base_url="http://broker", transport=httpx.MockTransport(handler)
    ) as client:
        receipt = await BrokerAdapter(client).call_tool("computer_act", {}, "rejected")
    assert receipt.isError
    assert receipt.structuredContent == {"action_id": "rejected", "metrics": metrics}
    assert json.loads(receipt.content[0].text)["metrics"] == metrics
    assert len(requests) == 1
