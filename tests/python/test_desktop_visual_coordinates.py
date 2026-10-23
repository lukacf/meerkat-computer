"""Visual input binds desktop window topology without freezing animated content."""

import asyncio
import json
from dataclasses import replace

import pytest
from meerkat_computer.desktop_x11 import X11, DesktopTopology, DisplayState, WindowGeometry
from meerkat_computer.protocol import WorkerError
from test_desktop import act, lease, observe
from test_desktop import desktop as shared_desktop
from test_desktop_freshness import event
from test_desktop_topology import Reader, collect
from test_desktop_visual_freshness import VisualAnchor

desktop = shared_desktop


def desktop_anchor(anchor):
    if anchor is None:
        return None
    return DesktopTopology(
        DisplayState(640, 480, str(anchor.identity)),
        1,
        tuple(("window", 0, window) for window in anchor.topology if window != 1),
        (
            WindowGeometry(1, (0, 0, 640, 480), 0, "InputOutput", "IsViewable", False),
            WindowGeometry(
                anchor.identity, anchor.visible_bounds, 0, "InputOutput", "IsViewable", False
            ),
        ),
    )


async def setup_visual(desktop):
    await desktop.configure(lease(mode="visual"))
    desktop.x11.anchor = VisualAnchor(topology=(1, 123, 124))
    desktop.x11.topology_reads = 0

    async def capture_desktop_with_fence():
        anchor = desktop.x11.anchor
        return await desktop.x11.screenshot(), desktop_anchor(anchor)

    async def desktop_topology():
        desktop.x11.topology_reads += 1
        anchor = desktop.x11.anchor
        return desktop_anchor(anchor)

    desktop.x11.capture_desktop_with_fence = capture_desktop_with_fence
    desktop.x11.desktop_topology = desktop_topology
    return (await observe(desktop))[0]


@pytest.mark.parametrize("event_kind", ["TextChanged", "ChildrenChanged", "disconnect"])
async def test_visual_coordinate_animation_and_unrelated_accessibility_allow_input(
    desktop, event_kind
):
    body = await setup_visual(desktop)
    desktop.x11.anchor = replace(desktop.x11.anchor, pixels="next animation frame")
    if event_kind == "disconnect":
        desktop.accessibility.available = False
    else:
        event(desktop.accessibility, path="/unrelated", member=event_kind)
    content, metrics = await desktop.perform("computer_act", act(body, type="click", x=200, y=100))
    assert desktop.x11.inputs == [("click", 200, 100, "left", 1)]
    assert metrics["ui_commands"] == 1
    assert desktop.accessibility.snapshots == desktop.accessibility.reads == 0
    assert content[0]["type"] == "text"


async def test_visual_coordinate_capture_ignores_accessibility_churn(desktop):
    await setup_visual(desktop)
    capture = desktop.x11.capture_desktop_with_fence

    async def changing_capture():
        result = await capture()
        event(desktop.accessibility, path="/unrelated", member="ChildrenChanged")
        return result

    desktop.x11.capture_desktop_with_fence = changing_capture
    body, _, _ = await observe(desktop)
    assert body["consistency"] == "desktop_window_topology"
    assert body["input"]["status"] == "requires_revalidation"
    await desktop.validate("computer_act", act(body, type="key", key="Enter"))
    assert not desktop.x11.inputs


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"identity": 456}, "visual_topology"),
        ({"visible_bounds": (20, 10, 610, 470)}, "visual_topology"),
        ({"topology": (1, 123, 456)}, "visual_topology"),
        ({"topology": (1, 124, 123)}, "visual_topology"),
        (None, "visual_unavailable"),
    ],
)
async def test_visual_coordinate_changed_window_rejects_before_input(desktop, change, reason):
    body = await setup_visual(desktop)
    desktop.x11.anchor = replace(desktop.x11.anchor, **change) if change else None
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="click", x=200, y=100))
    assert error.value.effect == "none" and reason in error.value.reasons
    assert not desktop.x11.inputs and not desktop.observation_id


@pytest.mark.parametrize(
    "change,reason", [({"focus": "456"}, "focus"), ({"width": 800}, "display")]
)
async def test_visual_coordinate_focus_and_display_still_reject(desktop, change, reason):
    body = await setup_visual(desktop)
    desktop.x11.screen = replace(desktop.x11.screen, **change)
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="key", key="Enter"))
    assert reason in error.value.reasons and not desktop.x11.inputs


@pytest.mark.parametrize("kind", ["map", "bounds", "input_only_overlay"])
async def test_visual_coordinate_native_overlay_metadata_rejects(desktop, kind):
    body = await setup_visual(desktop)
    before = desktop.observation_topology
    window = WindowGeometry(123, (10, 10, 620, 470), 0, "InputOutput", "IsViewable", False)
    desktop.observation_topology = replace(before, windows=(window,))
    change = {
        "map": replace(window, map_state="IsUnMapped"),
        "bounds": replace(window, bounds=(11, 10, 620, 470)),
        "input_only_overlay": WindowGeometry(
            456, (10, 10, 620, 470), 0, "InputOnly", "IsViewable", True
        ),
    }[kind]

    async def topology():
        return replace(before, windows=(change,))

    desktop.x11.desktop_topology = topology
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="key", key="Enter"))
    assert error.value.reasons == ("visual_topology",) and not desktop.x11.inputs


async def test_visual_coordinate_drag_revalidates_once_for_all_waypoints(desktop):
    body = await setup_visual(desktop)
    await desktop.validate(
        "computer_act",
        act(
            body,
            type="drag",
            x=20,
            y=20,
            end_x=300,
            end_y=300,
            path=[{"x": 30 + index, "y": 30 + index} for index in range(64)],
        ),
    )
    assert desktop.x11.topology_reads == 2
    assert not desktop.x11.inputs and desktop.x11.screenshots == 1


@pytest.mark.parametrize("target", ["start", "end", "waypoint"])
async def test_visual_coordinate_drag_never_routes_outside_desktop(desktop, target):
    body = await setup_visual(desktop)
    values = {
        "type": "drag",
        "x": 20,
        "y": 20,
        "end_x": 300,
        "end_y": 300,
        "path": [{"x": 30, "y": 30}],
    }
    values.update(
        {"start": {"x": -1}, "end": {"end_y": 480}, "waypoint": {"path": [{"x": 640, "y": 0}]}}[
            target
        ]
    )
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, **values))
    assert error.value.code == "invalid_coordinates" and not desktop.x11.inputs


async def test_visual_coordinate_scroll_requires_explicit_target(desktop):
    body = await setup_visual(desktop)
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="scroll", delta_y=120))
    assert error.value.code == "invalid_coordinates" and "coordinates" in str(error.value)
    assert not desktop.x11.inputs
    await desktop.perform("computer_act", act(body, type="scroll", x=200, y=100, delta_y=120))
    assert desktop.x11.inputs == [("move", 200, 100), ("scroll", 0, 120)]


@pytest.mark.parametrize(
    "action",
    [
        {"type": "click", "x": 0, "y": 0},
        {"type": "click", "x": 635, "y": 479},
        {"type": "drag", "x": 20, "y": 5, "end_x": 100, "end_y": 100},
    ],
)
async def test_visual_coordinate_other_app_taskbar_and_titlebar_remain_usable(desktop, action):
    body = await setup_visual(desktop)
    _, metrics = await desktop.perform("computer_act", act(body, **action))
    assert metrics["ui_commands"] == 1 and desktop.x11.inputs


async def test_visual_coordinate_root_focus_can_use_keyboard_launcher(desktop):
    await setup_visual(desktop)
    desktop.x11.screen = replace(desktop.x11.screen, focus="1")
    desktop.x11.anchor = replace(desktop.x11.anchor, identity=1)
    body, _, _ = await observe(desktop)
    await desktop.perform("computer_act", act(body, type="key", key="Alt+F2"))
    assert desktop.x11.inputs == [("key", "Alt+F2")]


async def test_visual_coordinate_missing_capture_topology_is_observe_only(desktop):
    await setup_visual(desktop)
    desktop.x11.anchor = None
    body, content, _ = await observe(desktop)
    assert len(content) == 2 and body["input"]["status"] == "observe_only"
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="key", key="Enter"))
    assert error.value.reasons == ("visual_unavailable",) and not desktop.x11.inputs


async def test_visual_coordinate_second_topology_read_catches_race(desktop):
    body = await setup_visual(desktop)
    original = desktop.x11.desktop_topology

    async def changed_topology():
        result = await original()
        desktop.x11.anchor = replace(desktop.x11.anchor, topology=(1, 123, 456))
        return result

    desktop.x11.desktop_topology = changed_topology
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="key", key="Enter"))
    assert error.value.reasons == ("visual_topology",) and not desktop.x11.inputs


async def test_visual_coordinate_final_focus_read_catches_race(desktop):
    body = await setup_visual(desktop)
    original = desktop.x11.desktop_topology

    async def changed_focus():
        result = await original()
        desktop.x11.screen = replace(desktop.x11.screen, focus="456")
        return result

    desktop.x11.desktop_topology = changed_focus
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="key", key="Enter"))
    assert error.value.reasons == ("focus",) and not desktop.x11.inputs


@pytest.mark.parametrize("uncertain", [False, True])
async def test_visual_coordinate_animated_drag_dispatches_once_and_never_replays(
    desktop, uncertain
):
    from unittest.mock import AsyncMock

    body = await setup_visual(desktop)
    desktop.x11.anchor = replace(desktop.x11.anchor, pixels="next animation frame")
    event(desktop.accessibility, path="/unrelated", member="ChildrenChanged")
    desktop.x11.drag = AsyncMock(side_effect=OSError("Lost acknowledgement") if uncertain else None)
    request = act(
        body,
        type="drag",
        x=20,
        y=20,
        end_x=300,
        end_y=300,
        path=[{"x": 30, "y": 40}, {"x": 50, "y": 60}],
    )
    if uncertain:
        with pytest.raises(WorkerError) as error:
            await desktop.perform("computer_act", request)
        assert error.value.effect == "unknown"
    else:
        _, metrics = await desktop.perform("computer_act", request)
        assert metrics["ui_command_attempts"] == metrics["ui_commands"] == 1
    desktop.x11.drag.assert_awaited_once_with(
        (20, 20), (300, 300), button="left", path=[(30, 40), (50, 60)]
    )
    with pytest.raises(WorkerError):
        await desktop.perform("computer_act", request)
    assert desktop.x11.drag.await_count == 1


async def test_visual_coordinate_topology_deadline_cancels_before_input(desktop, monkeypatch):
    import meerkat_computer.desktop as module

    body = await setup_visual(desktop)
    cancelled = []

    async def stalled_topology():
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(module, "MAX_VISUAL_FENCE_SECONDS", 0.01, raising=False)
    desktop.x11.desktop_topology = stalled_topology
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="key", key="Enter"))
    assert error.value.reasons == ("visual_unavailable",)
    assert cancelled == [True] and not desktop.x11.inputs


@pytest.mark.parametrize("focus", ["0", "1", "20"])
async def test_visual_coordinate_topology_allows_root_focus_without_active_client(focus):
    driver = X11(":99")
    calls = []

    async def command(*arguments, **kwargs):
        calls.append((arguments, kwargs))
        return json.dumps(collect(Reader(int(focus))))

    driver.command = command
    topology = await driver.desktop_topology()
    assert topology.root == 80 and topology.screen.focus == focus
    assert topology.windows[2].window_class == "InputOnly"
    assert topology.windows[2].override_redirect is True
    assert len(calls) == 1
    assert calls[0][0] == ("-m", "meerkat_computer.desktop_topology")
    assert calls[0][1]["timeout"] == 5


async def test_visual_coordinate_capture_brackets_full_desktop_without_rgb_comparison():
    driver = X11(":99")
    anchor = desktop_anchor(VisualAnchor())
    order = []
    topologies = [anchor, anchor]

    async def topology():
        order.append("topology")
        return topologies.pop(0)

    async def screenshot():
        order.append("screenshot")
        return b"exact model screenshot"

    driver.desktop_topology, driver.screenshot = topology, screenshot
    assert await driver.capture_desktop_with_fence() == (b"exact model screenshot", anchor)
    assert order == ["topology", "screenshot", "topology"]
    topologies.extend([anchor, replace(anchor, tree=anchor.tree + (("window", 0, 456),))])
    assert await driver.capture_desktop_with_fence() == (b"exact model screenshot", None)


async def test_visual_coordinate_capture_topology_deadline_preserves_screenshot(monkeypatch):
    import meerkat_computer.desktop_x11 as module

    driver = X11(":99")
    cancelled = []

    async def topology():
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)

    async def screenshot():
        return b"observe only screenshot"

    driver.desktop_topology, driver.screenshot = topology, screenshot
    monkeypatch.setattr(module, "MAX_VISUAL_FENCE_SECONDS", 0.01)
    assert await driver.capture_desktop_with_fence() == (b"observe only screenshot", None)
    assert cancelled == [True]


@pytest.mark.parametrize("focus", ["0", "1", "80"])
async def test_visual_coordinate_empty_root_desktop_is_actionable(focus):
    driver = X11(":99")
    reader = Reader(int(focus))
    reader.trees = {80: (0, [])}
    empty = (
        "xwininfo: Window id: 0x50 (the root window) (has no name)\n"
        "  Root window id: 0x50 (the root window) (has no name)\n"
        "  Parent window id: 0x0 (none)\n"
        "     0 children.\n"
    )
    assert X11.parse_tree(empty) == (80, (("children", 5, 0),))

    async def command(*arguments, **kwargs):
        return json.dumps(collect(reader))

    async def screenshot():
        return b"empty desktop screenshot"

    driver.command, driver.screenshot = command, screenshot
    png, topology = await driver.capture_desktop_with_fence()
    assert png == b"empty desktop screenshot" and topology.root == 80
    assert topology.screen.focus == focus and len(topology.windows) == 1
