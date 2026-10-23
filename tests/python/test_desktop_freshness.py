"""Synthetic AT-SPI event and target checks; real Linux remains an explicit lane."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from dbus_next import MessageType
from meerkat_computer.protocol import WorkerError
from test_desktop import APP, BUTTON, COMPONENT, ENTRY, WINDOW, Node, act, observe, ref
from test_desktop import desktop as shared_desktop

desktop = shared_desktop


BACKGROUND = replace(
    WINDOW,
    key=(":1.2", "/background"),
    name="Browser",
    states=("enabled", "sensitive", "showing", "visible"),
)
CLOCK = Node(
    (":1.2", "/clock"),
    "Clock",
    "section",
    85,
    ("showing",),
    (COMPONENT,),
    bounds=(500, 300, 50, 20),
)


def background_clock(desktop):
    desktop.accessibility.nodes.update({BACKGROUND.key: BACKGROUND, CLOCK.key: CLOCK})
    desktop.accessibility.parents.update({BACKGROUND.key: APP.key, CLOCK.key: BACKGROUND.key})


def event(
    transport,
    *,
    path="/clock",
    member="TextChanged",
    detail="insert",
    interface="Object",
    child=None,
):
    transport._event(
        SimpleNamespace(
            message_type=MessageType.SIGNAL,
            interface="org.a11y.atspi.Event." + interface,
            member=member,
            sender=":1.2",
            path=path,
            body=[detail, 0, 1, child if child is not None else "synthetic update"],
        )
    )


@pytest.mark.parametrize("member", ["TextChanged", "ChildrenChanged"])
async def test_continuous_unrelated_text_events_do_not_starve_capture(desktop, member):
    background_clock(desktop)
    original = desktop.accessibility.snapshot

    async def ticking_snapshot():
        tree = await original()
        event(desktop.accessibility, member=member)
        return tree

    desktop.accessibility.snapshot = ticking_snapshot
    body, _, metrics = await observe(desktop)
    assert body["elements"]
    assert desktop.accessibility.snapshots == 1
    assert metrics["observations"] == 1


@pytest.mark.parametrize("target", ["ref", "coordinates"])
async def test_unrelated_text_event_does_not_stale_known_click_target(desktop, target):
    background_clock(desktop)
    body, _, _ = await observe(desktop)
    event(desktop.accessibility)
    action = (
        {"type": "click", "ref": ref(body, "Save")}
        if target == "ref"
        else {
            "type": "click",
            "x": 280,
            "y": 50,
        }
    )
    _, metrics = await desktop.perform("computer_act", act(body, **action))
    assert desktop.x11.inputs == [("click", 280, 50, "left", 1)]
    assert metrics["ui_commands"] == 1


@pytest.mark.parametrize("action_kind", ["native_fill", "coordinate_click", "ref_click"])
async def test_window_event_during_last_validation_read_rejects_input(desktop, action_kind):
    sibling = replace(CLOCK, key=(":1.2", "/sibling"))
    desktop.accessibility.nodes[sibling.key] = sibling
    desktop.accessibility.parents[sibling.key] = WINDOW.key
    body, _, _ = await observe(desktop)
    original = desktop.accessibility.read_node
    button_reads = 0

    async def changed_during_read(key, text_limit=2048):
        nonlocal button_reads
        node = await original(key, text_limit)
        if key == BUTTON.key:
            button_reads += 1
        inject = (
            (action_kind == "native_fill" and key == WINDOW.key)
            or (action_kind == "coordinate_click" and key == BUTTON.key)
            or (action_kind == "ref_click" and key == BUTTON.key and button_reads == 2)
        )
        if inject:
            event(desktop.accessibility, path="/sibling", member="ChildrenChanged")
        return node

    desktop.accessibility.read_node = changed_during_read
    action = {
        "native_fill": {"type": "fill", "ref": ref(body, "Title"), "text": "new"},
        "coordinate_click": {"type": "click", "x": 280, "y": 50},
        "ref_click": {"type": "click", "ref": ref(body, "Save")},
    }[action_kind]
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, **action))
    assert error.value.effect == "none"
    assert not desktop.x11.inputs and not desktop.accessibility.inputs


@pytest.mark.parametrize("action_kind", ["fill", "invoke"])
async def test_remove_readd_equal_target_does_not_reuse_native_ref(desktop, action_kind):
    container = replace(CLOCK, key=(":1.2", "/container"))
    desktop.accessibility.nodes[container.key] = container
    desktop.accessibility.parents[container.key] = WINDOW.key
    target = ENTRY if action_kind == "fill" else BUTTON
    desktop.accessibility.parents[target.key] = container.key
    # Traverse the parent before its target, as the production breadth-first walk does.
    desktop.accessibility.nodes = {
        APP.key: APP,
        WINDOW.key: WINDOW,
        container.key: container,
        ENTRY.key: ENTRY,
        BUTTON.key: BUTTON,
    }
    body, _, _ = await observe(desktop)
    for detail in ("remove", "add"):
        event(
            desktop.accessibility,
            path="/container",
            member="ChildrenChanged",
            detail=detail,
            child=SimpleNamespace(value=list(target.key)),
        )
    assert desktop.accessibility.node_revision(target.key) > 0
    action = (
        {"type": "fill", "ref": ref(body, "Title"), "text": "new"}
        if action_kind == "fill"
        else {
            "type": "invoke",
            "ref": ref(body, "Save"),
            "action_name": "click",
        }
    )
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, **action))
    assert error.value.effect == "none" and not desktop.accessibility.inputs


@pytest.mark.parametrize(
    "member,interface,path",
    [
        ("ChildrenChanged", "Object", "/unknown-source"),
        ("ChildrenChanged", "Object", "/window"),
        ("BoundsChanged", "Object", "/clock"),
        ("StateChanged", "Object", "/clock"),
        ("Create", "Window", "/clock"),
        ("FutureUnknownEvent", "Object", "/clock"),
    ],
)
async def test_unknown_or_structural_events_keep_global_fence(desktop, member, interface, path):
    background_clock(desktop)
    body, _, _ = await observe(desktop)
    event(desktop.accessibility, member=member, interface=interface, path=path)
    with pytest.raises(WorkerError) as error:
        await desktop.perform(
            "computer_act", act(body, type="fill", ref=ref(body, "Title"), text="new")
        )
    assert error.value.effect == "none" and not desktop.accessibility.inputs


async def test_same_window_clock_capture_is_non_atomic_but_cannot_authorize_input(desktop):
    desktop.accessibility.nodes[CLOCK.key] = CLOCK
    desktop.accessibility.parents[CLOCK.key] = WINDOW.key
    original = desktop.accessibility.snapshot

    async def ticking_snapshot():
        tree = await original()
        event(desktop.accessibility, member="ChildrenChanged")
        return tree

    desktop.accessibility.snapshot = ticking_snapshot
    body, _, _ = await observe(desktop)
    assert desktop.accessibility.snapshots == 1
    with pytest.raises(WorkerError) as error:
        await desktop.perform(
            "computer_act", act(body, type="fill", ref=ref(body, "Title"), text="new")
        )
    assert error.value.effect == "none" and not desktop.accessibility.inputs


async def test_observation_after_acknowledged_input_may_show_a_ticking_window(desktop):
    background_clock(desktop)
    body, _, _ = await observe(desktop)
    original = desktop.accessibility.invoke

    async def close_picker(node, index):
        await original(node, index)
        desktop.accessibility.nodes[WINDOW.key] = replace(WINDOW, states=("showing",))
        desktop.accessibility.nodes[BACKGROUND.key] = replace(BACKGROUND, states=WINDOW.states)
        snapshot = desktop.accessibility.snapshot

        async def ticking_snapshot():
            tree = await snapshot()
            event(desktop.accessibility, member="ChildrenChanged")
            return tree

        desktop.accessibility.snapshot = ticking_snapshot

    desktop.accessibility.invoke = close_picker
    _, metrics = await desktop.perform(
        "computer_act", act(body, type="invoke", ref=ref(body, "Save"), action_name="click")
    )
    assert metrics["ui_commands"] == 1 and metrics["native_invocations"] == 1
    assert desktop.accessibility.inputs == [("invoke", BUTTON.key, 0)]


async def test_hybrid_unknown_hit_without_visual_anchor_keeps_full_event_fence(desktop):
    background_clock(desktop)
    body, _, _ = await observe(desktop)
    event(desktop.accessibility, member="ChildrenChanged")
    with pytest.raises(WorkerError):
        await desktop.perform("computer_act", act(body, type="click", x=400, y=200))
    assert not desktop.x11.inputs
