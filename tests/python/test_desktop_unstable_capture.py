"""Unstable semantics must not suppress screenshots or authorize stale input."""

import asyncio
import base64
from dataclasses import replace

import pytest
from meerkat_computer.protocol import ObserveArguments, WorkerError
from test_desktop import act, lease, observe, ref
from test_desktop import desktop as shared_desktop
from test_desktop_visual_freshness import VisualAnchor

desktop = shared_desktop


def noisy_accessibility(desktop, *, anchor=True):
    desktop.accessibility.mutate_during_snapshot = 100
    desktop.x11.anchor = VisualAnchor() if anchor else None

    async def capture():
        png = await desktop.x11.screenshot()
        anchor = desktop.x11.anchor
        return png + str(desktop.x11.screenshots).encode(), anchor.fence() if anchor else None

    desktop.x11.capture_with_fence = capture


async def test_hybrid_returns_fresh_screenshot_without_unstable_or_previous_refs(desktop):
    previous, _, _ = await observe(desktop)
    old_ref = ref(previous, "Save")
    noisy_accessibility(desktop)
    body, content, metrics = await observe(desktop)
    assert body["observation_id"] != previous["observation_id"]
    assert body["accessibility"]["status"] == "partial"
    assert "unstable_tree" in body["accessibility"]["issues"]
    assert not body["elements"] and not body["applications"] and not body["windows"]
    assert not desktop.refs and not desktop.ref_windows and not desktop.observation_tree.nodes
    assert base64.b64decode(content[1]["data"]) == b"test-png5"
    assert desktop.accessibility.snapshots == 4  # One prior observation, three bounded retries.
    assert metrics["observations"] == metrics["observation_fallbacks"] == 1
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="click", ref=old_ref))
    assert error.value.code == "stale_ref" and error.value.effect == "none"
    assert not desktop.x11.inputs and not desktop.accessibility.inputs


async def test_settled_visual_fallback_can_navigate_without_querying_unstable_tree(desktop):
    noisy_accessibility(desktop)
    body, _, _ = await observe(desktop)
    assert body["input"]["status"] == "requires_revalidation"
    _, metrics = await desktop.perform("computer_act", act(body, type="key", key="PageDown"))
    assert desktop.x11.inputs == [("key", "PageDown")]
    assert metrics["ui_commands"] == 1


@pytest.mark.parametrize("anchor", [False, True])
async def test_unstable_capture_can_be_viewed_without_authorizing_input(desktop, anchor):
    noisy_accessibility(desktop, anchor=anchor)
    original = desktop.x11.capture_with_fence

    async def event_during_fallback():
        captured = await original()
        if desktop.x11.screenshots == 4 and anchor:
            desktop.accessibility.revision += 1
            desktop.accessibility.structural_revision += 1
        return captured

    desktop.x11.capture_with_fence = event_during_fallback
    body, content, _ = await observe(desktop)
    assert len(content) == 2 and body["input"]["status"] == "observe_only"
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="key", key="PageDown"))
    assert error.value.effect == "none" and not desktop.x11.inputs


@pytest.mark.parametrize(
    "change",
    [
        "event",
        "structural_event",
        "disconnect",
        "focus",
        "size",
        "pixels",
        "identity",
        "bounds",
        "topology",
        "missing_anchor",
    ],
)
async def test_fallback_rejects_changed_state_before_input(desktop, change):
    noisy_accessibility(desktop)
    body, _, _ = await observe(desktop)
    if change == "event":
        desktop.accessibility.revision += 1
    elif change == "structural_event":
        desktop.accessibility.structural_revision += 1
    elif change == "disconnect":
        desktop.accessibility.available = False
    elif change == "focus":
        desktop.x11.screen = replace(desktop.x11.screen, focus="999")
    elif change == "size":
        desktop.x11.screen = replace(desktop.x11.screen, width=800)
    elif change == "missing_anchor":
        desktop.x11.anchor = None
    else:
        value = {
            "pixels": "different",
            "identity": 456,
            "bounds": (20, 20, 500, 400),
            "topology": (1, 123, 456),
        }[change]
        desktop.x11.anchor = replace(
            desktop.x11.anchor, **{"visible_bounds" if change == "bounds" else change: value}
        )
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="key", key="PageDown"))
    assert error.value.effect == "none" and not desktop.x11.inputs


@pytest.mark.parametrize(
    "action",
    [
        {"type": "click", "x": 0, "y": 0},
        {"type": "scroll", "delta_y": 120},
        {"type": "drag", "x": 100, "y": 100, "end_x": 0, "end_y": 0},
    ],
)
async def test_fallback_rejects_unfenced_input_even_without_new_events(desktop, action):
    noisy_accessibility(desktop)
    body, _, _ = await observe(desktop)
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, **action))
    assert error.value.effect == "none" and not desktop.x11.inputs


@pytest.mark.parametrize("change", ["focus", "lease", "capture_failure"])
async def test_fallback_does_not_mask_display_or_authority_failures(desktop, change):
    noisy_accessibility(desktop)
    original = desktop.x11.capture_with_fence

    async def changed_capture():
        captured = await original()
        if desktop.x11.screenshots == 4:
            if change == "focus":
                desktop.x11.screen = replace(desktop.x11.screen, focus="999")
            elif change == "lease":
                desktop.lease = None
            else:
                raise WorkerError("desktop_io_failed", "Capture failed", 503)
        return captured

    desktop.x11.capture_with_fence = changed_capture
    with pytest.raises(WorkerError):
        await observe(desktop)
    assert not desktop.observation_id and not desktop.refs and not desktop.x11.inputs


async def test_semantic_only_unstable_capture_never_uses_screenshots(desktop):
    await desktop.configure(lease(mode="semantic"))
    noisy_accessibility(desktop)
    with pytest.raises(WorkerError) as error:
        await observe(desktop)
    assert error.value.code == "observation_unstable"
    assert not desktop.x11.screenshots and not desktop.observation_id


async def test_fallback_rechecks_events_during_final_input_validation(desktop):
    noisy_accessibility(desktop)
    body, _, _ = await observe(desktop)
    original = desktop.x11.capture_with_fence

    async def new_event():
        capture = await original()
        desktop.accessibility.revision += 1
        return capture

    desktop.x11.capture_with_fence = new_event
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="key", key="PageDown"))
    assert error.value.effect == "none" and not desktop.x11.inputs


async def test_coherent_observation_restores_refs_and_replaces_fallback_restrictions(desktop):
    noisy_accessibility(desktop, anchor=False)
    fallback, _, _ = await observe(desktop)
    desktop.accessibility.mutate_during_snapshot = 0
    body, _, _ = await observe(desktop)
    assert "unstable_tree" not in desktop.observation_tree.issues
    assert body["accessibility"]["status"] == "available"
    assert body["observation_id"] != fallback["observation_id"]
    await desktop.perform("computer_act", act(body, type="click", ref=ref(body, "Save")))
    assert desktop.x11.inputs == [("click", 280, 50, "left", 1)]


async def test_fallback_cannot_reuse_previous_id_or_expand_previous_scope(desktop):
    previous, _, _ = await observe(desktop)
    noisy_accessibility(desktop)
    body, _, _ = await observe(desktop)
    with pytest.raises(WorkerError):
        await desktop.perform("computer_act", act(previous, type="key", key="PageDown"))
    with pytest.raises(WorkerError):
        await desktop.perform(
            "computer_observe",
            ObserveArguments(
                observation_id=body["observation_id"], scope_ref=ref(previous, "Save")
            ),
        )
    assert not desktop.x11.inputs


async def test_scoped_capture_fallback_does_not_publish_unstable_scope(desktop):
    previous, _, _ = await observe(desktop)
    noisy_accessibility(desktop)
    await desktop.perform(
        "computer_observe",
        ObserveArguments(
            observation_id=previous["observation_id"], scope_ref=ref(previous, "Save")
        ),
    )
    assert "unstable_tree" in desktop.observation_tree.issues
    assert not desktop.refs and not desktop.ref_windows


@pytest.mark.parametrize("capture_fails", [False, True])
async def test_acknowledged_input_followed_by_fallback_preserves_effect_semantics(
    desktop, capture_fails
):
    previous, _, _ = await observe(desktop)
    noisy_accessibility(desktop)
    original = desktop.x11.capture_with_fence

    async def last_capture():
        if capture_fails and desktop.x11.screenshots == 4:
            raise WorkerError("desktop_io_failed", "Capture failed", 503)
        return await original()

    desktop.x11.capture_with_fence = last_capture
    request = act(previous, type="click", ref=ref(previous, "Save"))
    if capture_fails:
        with pytest.raises(WorkerError) as error:
            await desktop.perform("computer_act", request)
        assert error.value.effect == "unknown"
    else:
        content, metrics = await desktop.perform("computer_act", request)
        assert len(content) == 2
        assert metrics["ui_commands"] == metrics["observation_fallbacks"] == 1
    assert desktop.x11.inputs == [("click", 280, 50, "left", 1)]
    with pytest.raises(WorkerError):
        await desktop.perform("computer_act", request)
    assert len(desktop.x11.inputs) == 1


async def test_cancelled_fallback_never_restores_old_refs(desktop):
    await observe(desktop)
    noisy_accessibility(desktop)
    original = desktop.x11.capture_with_fence

    async def cancelled_capture():
        if desktop.x11.screenshots == 4:
            raise asyncio.CancelledError()
        return await original()

    desktop.x11.capture_with_fence = cancelled_capture
    with pytest.raises(asyncio.CancelledError):
        await observe(desktop)
    assert not desktop.refs and not desktop.observation_id
