"""GTK menu reads may repeat checked-state signals without a state transition."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dbus_next import MessageType
from meerkat_computer import desktop_atspi
from meerkat_computer.desktop_atspi import Atspi
from meerkat_computer.protocol import WorkerError
from test_desktop import BUTTON, ReadOnlyAtspi, act, lease, observe, ref
from test_desktop import desktop as shared_desktop

desktop = shared_desktop


def signal(
    transport,
    *,
    value=0,
    key=(":1.2", "/menu"),
    member="StateChanged",
    body=None,
    interface="org.a11y.atspi.Event.Object",
):
    transport._event(
        SimpleNamespace(
            message_type=MessageType.SIGNAL,
            interface=interface,
            member=member,
            sender=key[0],
            path=key[1],
            body=["checked", value, 0, None] if body is None else body,
        )
    )


def revisions(transport):
    return transport.revision, transport.structural_revision


@pytest.mark.parametrize("known", [False, True])
async def test_repeated_checked_notifications_allow_capture_and_input(desktop, known):
    transport = desktop.accessibility
    original = transport.snapshot
    key = BUTTON.key if known else (":1.9", "/unvisited-menu")

    async def query_emits_checked():
        tree = await original()
        signal(transport, key=key, value=1)
        return tree

    transport.snapshot = query_emits_checked
    body, _, _ = await observe(desktop)
    # First notification still invalidates; only the identical second read settles.
    assert transport.snapshots == 2
    before = revisions(transport)
    signal(transport, key=key, value=1)
    assert revisions(transport) == before
    await desktop.perform("computer_act", act(body, type="click", ref=ref(body, "Save")))
    assert desktop.x11.inputs == [("click", 280, 50, "left", 1)]


async def test_actual_checked_transitions_never_settle_capture(desktop):
    await desktop.configure(lease(mode="semantic"))
    transport = desktop.accessibility
    original = transport.snapshot

    async def changing_snapshot():
        tree = await original()
        signal(transport, value=transport.snapshots % 2)
        return tree

    transport.snapshot = changing_snapshot
    with pytest.raises(WorkerError, match="Desktop changed during capture"):
        await observe(desktop)
    assert transport.snapshots == 3
    assert not desktop.x11.inputs and not transport.inputs


@pytest.mark.parametrize("values", [[1], [1, 0]])
async def test_checked_transition_invalidates_old_input_even_if_value_returns(desktop, values):
    signal(desktop.accessibility, value=0)
    body, _, _ = await observe(desktop)
    for value in values:
        signal(desktop.accessibility, value=value)
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="click", ref=ref(body, "Save")))
    assert error.value.effect == "none"
    assert not desktop.x11.inputs and not desktop.accessibility.inputs


def test_checked_cache_is_per_bus_sender_and_object():
    transport = Atspi()
    for key in [(":1.2", "/one"), (":1.3", "/one"), (":1.2", "/two")]:
        before = revisions(transport)
        signal(transport, key=key)
        assert revisions(transport) == (before[0] + 1, before[1] + 1)
        signal(transport, key=key)
        assert revisions(transport) == (before[0] + 1, before[1] + 1)


@pytest.mark.parametrize(
    "body",
    [
        ["checked"],
        ["checked", 0],
        ["checked", 2, 0],
        ["checked", True, 0],
        ["checked", "0", 0],
        ["checked", 0, 1],
        ["checked", 0, False],
        ["focused", 0, 0],
        ["defunct", 0, 0],
        ["future-state", 0, 0],
    ],
)
def test_malformed_or_other_state_notifications_always_invalidate(body):
    transport = Atspi()
    signal(transport, body=body)
    before = revisions(transport)
    signal(transport, body=body)
    assert revisions(transport) == (before[0] + 1, before[1] + 1)


@pytest.mark.parametrize("key", [("", "/menu"), (":1.2", ""), ("org.example.App", "/menu")])
def test_unresolved_sender_or_object_cannot_prime_checked_cache(key):
    transport = Atspi()
    signal(transport, key=key)
    before = revisions(transport)
    signal(transport, key=key)
    assert revisions(transport) == (before[0] + 1, before[1] + 1)


@pytest.mark.parametrize(
    "change",
    [
        {
            "member": "ChildrenChanged",
            "key": (":1.2", "/unknown-parent"),
            "body": ["remove", 0, 0, [":1.2", "/menu"]],
        },
        {
            "member": "ChildrenChanged",
            "key": (":1.2", "/unknown-parent"),
            "body": ["add", 0, 0, [":1.2", "/menu"]],
        },
        {"body": ["defunct", 1, 0]},
        {"body": ["defunct", 0, 0]},
        {"member": "PropertyChange", "body": ["widget", 0, 0]},
        {"member": "PropertyChange", "body": ["accessible-role", 0, 0]},
        {"interface": "org.a11y.atspi.Event.Window", "member": "Destroy"},
        {
            "interface": "org.freedesktop.DBus",
            "member": "NameOwnerChanged",
            "key": ("org.freedesktop.DBus", "/org/freedesktop/DBus"),
            "body": [":1.2", ":1.2", ""],
        },
    ],
)
def test_lifecycle_events_forget_previous_checked_value(change):
    transport = Atspi()
    signal(transport)
    signal(transport, **change)
    before = revisions(transport)
    signal(transport)
    assert revisions(transport) == (before[0] + 1, before[1] + 1)
    signal(transport)
    assert revisions(transport) == (before[0] + 1, before[1] + 1)


@pytest.mark.parametrize("disconnect", [False, True])
async def test_connection_discontinuity_forgets_checked_values(disconnect):
    transport = Atspi()
    signal(transport)
    if disconnect:
        transport.bus = SimpleNamespace(wait_for_disconnect=AsyncMock())
        await transport._watch_disconnect()
    else:
        await transport.close()
    before = revisions(transport)
    signal(transport)
    assert revisions(transport) == (before[0] + 1, before[1] + 1)


def test_checked_cache_eviction_cannot_reuse_old_state(monkeypatch):
    monkeypatch.setattr(desktop_atspi, "MAX_EVENT_OBJECTS", 3)
    transport = Atspi()
    for index in range(4):
        signal(transport, key=(":1.2", f"/menu-{index}"))
    assert len(transport._checked_states) <= 3
    before = revisions(transport)
    signal(transport, key=(":1.2", "/menu-0"))
    assert transport.revision == before[0] + 1
    assert transport.structural_revision > before[1]


@pytest.mark.parametrize("announced", [0, 1])
async def test_stable_state_query_cannot_preserve_a_contradictory_checked_value(announced):
    transport = ReadOnlyAtspi()
    signal(transport, key=BUTTON.key, value=announced)
    # This fixture returns editable but not checked from GetState.
    await transport.read_node(BUTTON.key)
    before = revisions(transport)
    signal(transport, key=BUTTON.key, value=announced)
    increment = int(announced == 1)
    assert revisions(transport) == (before[0] + increment, before[1] + increment)


async def test_inflight_state_query_does_not_overwrite_a_newer_signal():
    transport = ReadOnlyAtspi()
    original = transport.call
    signal(transport, key=BUTTON.key, value=0)

    async def query_emits_transition(key, interface, member, *args):
        if member == "GetRoleName":
            signal(transport, key=key, value=1)
        return await original(key, interface, member, *args)

    transport.call = query_emits_transition
    node = await transport.read_node(BUTTON.key)
    assert "node_changed_during_read" in node.issues
    before = revisions(transport)
    signal(transport, key=BUTTON.key, value=1)
    assert revisions(transport) == before
