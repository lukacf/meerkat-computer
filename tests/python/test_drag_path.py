"""One native drag owns the complete bounded path and its final button release."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from meerkat_computer.browser import Browser
from meerkat_computer.desktop_x11 import X11
from meerkat_computer.protocol import ActArguments, Action, Call, WorkerError
from meerkat_computer.worker import Worker
from pydantic import ValidationError
from test_desktop import act, lease, observe, ref
from test_desktop import desktop as desktop_fixture
from test_desktop_freshness import event
from test_desktop_visual_freshness import setup_dialog
from test_worker import TOKEN, FakeBrowser

desktop = desktop_fixture


def drag(**changes):
    return Action.model_validate({
        "type": "drag", "x": 10, "y": 20, "end_x": 90, "end_y": 80,
        "path": [{"x": 90, "y": 20}], **changes,
    })


def test_drag_path_is_optional_bounded_and_preserves_endpoint_contract():
    value = drag(path=[{"x": 1, "y": 2}] * 64)
    assert len(value.path) == 64
    assert value.path[0].x == 1 and value.end_x == 90
    legacy = Action(type="drag", x=1, y=2, end_x=3, end_y=4)
    assert legacy.path is None
    with pytest.raises(ValidationError):
        drag(end_x=None)


@pytest.mark.parametrize("path", [
    [], [{"x": 1, "y": 2}] * 65, "1,2", [[1, 2]], [{"x": 1}],
    [{"x": True, "y": 2}], [{"x": "1", "y": 2}],
    [{"x": float("nan"), "y": 2}], [{"x": 1, "y": float("inf")}],
    [{"x": 1, "y": 2, "command": "click"}],
])
def test_drag_path_rejects_malformed_waypoints(path):
    with pytest.raises(ValidationError):
        drag(path=path)


def test_drag_path_is_not_accepted_on_other_actions():
    with pytest.raises(ValidationError):
        drag(type="click")


@pytest.mark.parametrize("semantic_start", [False, True])
async def test_drag_path_validates_and_dispatches_every_point_once(desktop, semantic_start):
    body, _, _ = await observe(desktop)
    desktop.x11.drag = AsyncMock()
    target = {"ref": ref(body, "Save")} if semantic_start else {"x": 10, "y": 20}
    request = act(body, type="drag", end_x=400, end_y=400,
                  path=[{"x": 300, "y": 50}, {"x": 300, "y": 400}], **target)
    _, metrics = await desktop.perform("computer_act", request)
    desktop.x11.drag.assert_awaited_once_with(
        (280, 50) if semantic_start else (10, 20), (400, 400), button="left",
        path=[(300, 50), (300, 400)],
    )
    assert metrics["ui_command_attempts"] == metrics["ui_commands"] == 1
    assert metrics["clicks"] == 0
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", request)
    assert error.value.code == "stale_observation"
    assert desktop.x11.drag.await_count == 1


async def test_drag_path_outside_display_rejects_before_any_input(desktop):
    body, _, _ = await observe(desktop)
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="drag", x=10, y=20,
            end_x=90, end_y=80, path=[{"x": 640, "y": 20}]))
    assert error.value.code == "invalid_coordinates" and error.value.effect == "none"
    assert not desktop.x11.inputs
    assert desktop.operation_metrics.get("ui_command_attempts", 0) == 0


@pytest.mark.parametrize("semantic_start", [False, True])
async def test_drag_path_interior_context_is_revalidated_before_input(desktop, semantic_start):
    body, _, _ = await observe(desktop)
    original = desktop.verify_input_context

    async def changed_context(checked, point=None, **kwargs):
        if point == (90, 20):
            raise WorkerError("stale_observation", "Waypoint context changed")
        await original(checked, point, **kwargs)

    desktop.verify_input_context = changed_context
    target = {"ref": ref(body, "Save")} if semantic_start else {"x": 10, "y": 20}
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="drag", end_x=90, end_y=80,
            path=[{"x": 90, "y": 20}], **target))
    assert error.value.effect == "none"
    assert not desktop.x11.inputs


async def test_drag_path_browser_rejects_without_consuming_observation(tmp_path):
    browser = Browser(tmp_path)
    browser.observation_id = "before"
    browser.validate_observation = AsyncMock()
    arguments = ActArguments(observation_id="before", action=drag())
    with pytest.raises(WorkerError) as error:
        await browser.validate_action(arguments)
    assert error.value.code == "unsupported_action" and error.value.effect == "none"
    assert "desktop" in str(error.value).lower()
    assert browser.observation_id == "before"
    browser.validate_observation.assert_not_awaited()


async def test_drag_path_outside_focused_visual_window_rejects_before_input(desktop):
    body = await setup_dialog(desktop)
    event(desktop.accessibility, member="ChildrenChanged")
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="drag", x=20, y=20,
            end_x=300, end_y=300, path=[{"x": 0, "y": 0}]))
    assert error.value.effect == "none" and not desktop.x11.inputs


async def test_drag_path_uncertain_worker_receipt_cannot_replay_or_escape_lease(
    desktop, tmp_path,
):
    worker = Worker(tmp_path / "state", tmp_path / "workspace", TOKEN, FakeBrowser(),
                    desktop=desktop, clock=lambda: 1000)
    await worker.start()
    await worker.grant(lease())
    try:
        _, observation = await worker.call(Call(lease_id="lease-1", epoch=1,
            action_id="observe", name="computer_observe", arguments={"surface": "desktop"}))
        body = json.loads(observation["content"][0]["text"])
        request = Call(lease_id="lease-1", epoch=1, action_id="path", name="computer_act",
            arguments={"observation_id": body["observation_id"], "action": drag().model_dump()})
        desktop.x11.drag = AsyncMock(side_effect=OSError("Lost acknowledgement"))
        status, receipt = await worker.call(request)
        assert status == 409 and receipt["error"]["effect"] == "unknown"
        assert worker.journal.metrics()["ui_command_attempts"] == 1
        assert worker.journal.metrics().get("ui_commands", 0) == 0
        assert await worker.call(request) == (status, receipt)
        desktop.x11.drag.assert_awaited_once()
        await worker.grant(lease(epoch=2))
        with pytest.raises(WorkerError):
            await worker.call(request.model_copy(update={"action_id": "old-lease"}))
        desktop.x11.drag.assert_awaited_once()
    finally:
        await worker.close()


async def test_drag_path_x11_traces_turns_with_one_press_and_release(monkeypatch):
    driver = X11(":999")
    driver.command = AsyncMock()
    monkeypatch.setattr("meerkat_computer.desktop_x11.asyncio.sleep", AsyncMock())
    await driver.drag((0, 0), (80, 80), path=[(80, 0)], button="right")
    calls = [call.args for call in driver.command.await_args_list]
    assert calls[0] == ("mousemove", "0", "0")
    assert calls[1] == ("mousedown", "3")
    assert calls[9] == ("mousemove", "80", "0")
    assert calls[-2:] == [("mousemove", "80", "80"), ("mouseup", "3")]
    assert sum(call[0] == "mousedown" for call in calls) == 1
    assert sum(call[0] == "mouseup" for call in calls) == 1
    assert not any(call == ("mousemove", "10", "10") for call in calls)


async def test_drag_path_x11_failure_releases_without_replaying(monkeypatch):
    driver = X11(":999")
    calls = []

    async def command(*args):
        calls.append(args)
        if args == ("mousemove", "80", "10"):
            raise OSError("Lost input acknowledgement")

    driver.command = command
    monkeypatch.setattr("meerkat_computer.desktop_x11.asyncio.sleep", AsyncMock())
    with pytest.raises(OSError):
        await driver.drag((0, 0), (80, 80), path=[(80, 0)])
    assert calls[-1] == ("mouseup", "1")
    assert calls.count(("mousemove", "80", "10")) == 1


async def test_drag_path_cancellation_waits_for_release_even_if_cancelled_again():
    driver = X11(":999")
    pressed, releasing, finish_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def command(*args):
        calls.append(args)
        if args[0] == "mousedown":
            pressed.set()
            await asyncio.Event().wait()
        if args[0] == "mouseup":
            releasing.set()
            await finish_release.wait()

    driver.command = command
    task = asyncio.create_task(driver.drag((0, 0), (80, 80), path=[(80, 0)]))
    try:
        await asyncio.wait_for(pressed.wait(), 1)
        task.cancel()
        await asyncio.wait_for(releasing.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls.count(("mouseup", "1")) == 1
    finally:
        finish_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_drag_path_has_a_total_input_deadline_and_releases(monkeypatch):
    driver = X11(":999")
    calls = []

    async def command(*args):
        calls.append(args)
        if args[0] == "mousedown":
            await asyncio.Event().wait()

    driver.command = command
    monkeypatch.setattr("meerkat_computer.desktop_x11.MAX_DRAG_PATH_SECONDS", .02)
    with pytest.raises(TimeoutError):
        await driver.drag((0, 0), (80, 80), path=[(80, 0)])
    assert calls[-1] == ("mouseup", "1")
