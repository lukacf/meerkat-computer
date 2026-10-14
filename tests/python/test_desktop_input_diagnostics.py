"""Every native freshness rejection identifies only bounded, content-free causes."""

from dataclasses import replace

import pytest
from meerkat_computer.protocol import WorkerError
from test_desktop import BUTTON, act, observe, ref
from test_desktop import desktop as shared_desktop
from test_desktop_unstable_capture import noisy_accessibility

desktop = shared_desktop


@pytest.mark.parametrize(
    "change,reason",
    [
        ("pixels", "visual_pixels"),
        ("identity", "visual_topology"),
        ("bounds", "visual_topology"),
        ("topology", "visual_topology"),
        ("missing_anchor", "visual_unavailable"),
        ("event", "accessibility_events"),
        ("structure", "accessibility_structure"),
        ("disconnect", "accessibility_connection"),
        ("focus", "focus"),
        ("display", "display"),
    ],
)
async def test_rejection_identifies_exact_component_without_input(desktop, change, reason):
    noisy_accessibility(desktop)
    body, _, _ = await observe(desktop)
    if change == "missing_anchor":
        desktop.x11.anchor = None
    elif change == "event":
        desktop.accessibility.revision += 1
    elif change == "structure":
        desktop.accessibility.structural_revision += 1
    elif change == "disconnect":
        desktop.accessibility.available = False
    elif change == "focus":
        desktop.x11.screen = replace(desktop.x11.screen, focus="456")
    elif change == "display":
        desktop.x11.screen = replace(desktop.x11.screen, width=800)
    else:
        field, value = {
            "pixels": ("pixels", "changed single recipient character"),
            "identity": ("identity", 456),
            "bounds": ("visible_bounds", (20, 10, 610, 470)),
            "topology": ("topology", (1, 123, 456)),
        }[change]
        desktop.x11.anchor = replace(desktop.x11.anchor, **{field: value})
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="key", key="PageDown"))
    assert error.value.reasons == (reason,)
    assert error.value.effect == "none"
    assert not desktop.x11.inputs and not desktop.accessibility.inputs
    assert not desktop.observation_id
    assert "recipient" not in str(error.value.response())


async def test_validation_records_all_changed_components_not_only_the_first(desktop):
    noisy_accessibility(desktop)
    body, _, _ = await observe(desktop)
    original = desktop.x11.capture_with_fence

    async def changes_during_capture():
        desktop.x11.anchor = replace(desktop.x11.anchor, pixels="new", identity=456)
        result = await original()
        desktop.accessibility.revision += 1
        desktop.accessibility.structural_revision += 1
        desktop.accessibility.available = False
        return result

    desktop.x11.capture_with_fence = changes_during_capture
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="key", key="PageDown"))
    assert set(error.value.reasons) == {
        "visual_topology",
        "visual_pixels",
        "accessibility_structure",
        "accessibility_connection",
        "accessibility_events",
    }
    assert error.value.effect == "none" and not desktop.x11.inputs


@pytest.mark.parametrize("anchor", [True, False])
async def test_unusable_visual_target_has_explicit_reason(desktop, anchor):
    noisy_accessibility(desktop, anchor=anchor)
    body, _, _ = await observe(desktop)
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="click", x=0, y=0))
    assert error.value.reasons == ("visual_target_outside" if anchor else "visual_unavailable",)
    assert not desktop.x11.inputs


async def test_old_observation_id_and_changed_native_ref_are_distinct(desktop):
    body, _, _ = await observe(desktop)
    request = act(body, type="click", ref=ref(body, "Save"))
    desktop.accessibility.nodes[BUTTON.key] = replace(BUTTON, name="Delete")
    with pytest.raises(WorkerError) as changed:
        await desktop.perform("computer_act", request)
    assert changed.value.reasons == ("native_target",)
    with pytest.raises(WorkerError) as old:
        await desktop.perform("computer_act", request)
    assert old.value.reasons == ("observation_id",)
    assert not desktop.x11.inputs and not desktop.accessibility.inputs
