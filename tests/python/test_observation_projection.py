"""Projection budgets must never weaken the full private action fence."""

import json
from dataclasses import replace

import pytest
from meerkat_computer.desktop_atspi import REGISTRY, ROOT, Atspi, NativeAction
from meerkat_computer.protocol import ObserveArguments, WorkerError
from pydantic import ValidationError
from test_desktop import APP, BUTTON, ENTRY, WINDOW, act, ref
from test_desktop import desktop as shared_desktop
from test_desktop_freshness import event

desktop = shared_desktop


async def view(desktop, **options):
    blocks, _ = await desktop.perform("computer_observe", ObserveArguments(**options))
    return json.loads(blocks[0]["text"])


def busy_desktop(desktop):
    for i in range(296):
        node = replace(
            ENTRY,
            key=(":1.2", f"/row/{i}"),
            name=f"Field {i}",
            text="Long document content " * 96,
        )
        desktop.accessibility.nodes[node.key] = node
        desktop.accessibility.parents[node.key] = WINDOW.key


async def test_compact_default_has_useful_refs_and_honest_bounded_projection(desktop):
    busy_desktop(desktop)
    body = await view(desktop)
    assert len(body["elements"]) <= 60
    assert len(json.dumps(body, ensure_ascii=True)) < 35_000
    assert body["projection"]["detail"] == "compact"
    assert body["projection"]["inspected_elements"] == 300
    assert body["projection"]["omitted_elements"] == 300 - len(body["elements"])
    assert body["projection"]["truncated"] is True
    assert body["projection"]["text_chars"] <= 6000
    assert body["accessibility"]["status"] == "available"
    assert body["applications"][0]["name"] == "Native fixture"
    assert body["windows"][0]["name"] == "Editor"
    assert len(desktop.observation_tree.nodes) == 300
    assert set(desktop.refs) == {node["ref"] for node in body["elements"]}
    await desktop.perform(
        "computer_act", act(body, type="invoke", ref=ref(body, "Save"), action_name="click")
    )
    assert desktop.accessibility.inputs == [("invoke", BUTTON.key, 0)]


async def test_full_is_explicit_and_obeys_profile_ceiling(desktop):
    busy_desktop(desktop)
    desktop.lease.observation.max_elements = 100
    desktop.lease.observation.max_text_chars = 9000
    body = await view(desktop, detail="full")
    assert len(body["elements"]) == 100
    assert body["projection"]["max_elements"] == 100
    assert body["projection"]["text_chars"] <= 9000
    assert body["projection"]["truncated"]
    for option in ({"max_elements": 101}, {"max_text_chars": 9001}):
        with pytest.raises(WorkerError) as error:
            await view(desktop, **option)
        assert error.value.code == "observation_limit_exceeded"


async def test_compact_does_not_spend_target_budget_on_hidden_native_menu_items(desktop):
    hidden = replace(
        BUTTON,
        key=(":1.2", "/hidden-menu-item"),
        name="Hidden menu command",
        states=("enabled", "sensitive"),
    )
    desktop.accessibility.nodes = {
        APP.key: APP,
        WINDOW.key: WINDOW,
        hidden.key: hidden,
        ENTRY.key: ENTRY,
        BUTTON.key: BUTTON,
    }
    desktop.accessibility.parents[hidden.key] = WINDOW.key
    body = await view(desktop, max_elements=4)
    assert "Save" in [item["name"] for item in body["elements"]]
    assert "Hidden menu command" not in [item["name"] for item in body["elements"]]
    full = await view(desktop, detail="full")
    assert "Hidden menu command" in [item["name"] for item in full["elements"]]


async def test_detail_expansion_preserves_fields_hidden_by_compact(desktop):
    body = await view(desktop)
    button = next(e for e in body["elements"] if e["name"] == "Save")
    assert button["actions"] == [{"name": "click"}]
    expanded = await view(
        desktop, scope_ref=button["ref"], observation_id=body["observation_id"], detail="full"
    )
    assert expanded["observation_id"] != body["observation_id"]
    assert [e["name"] for e in expanded["elements"]] == ["Save"]
    assert expanded["elements"][0]["actions"][0]["description"] == "Save document"
    assert expanded["projection"]["scope_ref"] == expanded["elements"][0]["ref"]
    await desktop.perform(
        "computer_act", act(expanded, type="invoke", ref=ref(expanded, "Save"), action_name="click")
    )


async def test_scope_uses_current_observation_and_rejects_stale_target(desktop):
    body = await view(desktop)
    desktop.accessibility.nodes[BUTTON.key] = replace(BUTTON, name="Delete")
    with pytest.raises(WorkerError) as error:
        await view(
            desktop,
            scope_ref=ref(body, "Save"),
            observation_id=body["observation_id"],
            detail="full",
        )
    assert error.value.code == "stale_ref"


async def test_scope_cannot_silently_retarget_when_changed_during_capture(desktop):
    body = await view(desktop)
    original = desktop.accessibility.snapshot

    async def changing_snapshot(**options):
        if options:
            desktop.accessibility.nodes[BUTTON.key] = replace(BUTTON, name="Delete")
        return await original(**options)

    desktop.accessibility.snapshot = changing_snapshot
    with pytest.raises(WorkerError) as error:
        await view(desktop, scope_ref=ref(body, "Save"), observation_id=body["observation_id"])
    assert error.value.code == "stale_ref"


async def test_scope_traverses_beyond_prior_global_node_budget(desktop, monkeypatch):
    import meerkat_computer.desktop_atspi as implementation

    monkeypatch.setattr(implementation, "MAX_NODES", 6)
    root = replace(APP, key=(REGISTRY, ROOT), name="Desktop", role="desktop frame", role_id=14)
    first = replace(
        APP,
        key=(":1.2", "/first"),
        name="First panel",
        role="panel",
        role_id=39,
        states=("showing",),
    )
    second = replace(first, key=(":1.2", "/second"), name="Second panel")
    leaf = replace(ENTRY, key=(":1.2", "/beyond-budget"), name="Expanded field")
    nodes = {node.key: node for node in (root, APP, WINDOW, first, second, ENTRY, leaf)}
    children = {
        root.key: [APP.key],
        APP.key: [WINDOW.key],
        WINDOW.key: [first.key, second.key],
        first.key: [ENTRY.key],
        second.key: [leaf.key],
    }
    transport = desktop.accessibility

    async def read_node(key, text_limit=2048):
        node = nodes[key]
        transport.remember_role(key, node.role_id)
        return replace(node, child_count=len(children.get(key, [])))

    async def call(key, _interface, member, signature="", body=None):
        assert member == "GetChildAtIndex"
        return [list(children[key][body[0]])]

    transport.read_node, transport.call = read_node, call
    transport.snapshot = lambda **options: Atspi.snapshot(transport, **options)
    body = await view(desktop)
    assert body["accessibility"]["truncated"]
    assert "Expanded field" not in [item["name"] for item in body["elements"]]
    expanded = await view(
        desktop,
        detail="full",
        scope_ref=ref(body, "Second panel"),
        observation_id=body["observation_id"],
    )
    assert "Expanded field" in [item["name"] for item in expanded["elements"]]


async def test_expansion_can_read_text_omitted_by_prior_global_text_budget(desktop):
    limited = replace(ENTRY, text="", text_truncated=True)
    desktop.accessibility.nodes[ENTRY.key] = limited
    body = await view(desktop)
    original = desktop.accessibility.snapshot

    async def expanded_snapshot(**options):
        tree = await original(**options)
        if options:
            tree.nodes = [
                (replace(node, text="Full visible text", text_truncated=False), parent, window)
                for node, parent, window in tree.nodes
            ]
        return tree

    desktop.accessibility.snapshot = expanded_snapshot
    expanded = await view(
        desktop, detail="full", scope_ref=ref(body, "Title"), observation_id=body["observation_id"]
    )
    assert expanded["elements"][0]["text"] == "Full visible text"


async def test_compact_omitted_sibling_event_still_fences_native_action(desktop):
    busy_desktop(desktop)
    body = await view(desktop, max_elements=5)
    event(desktop.accessibility, path="/row/295", member="TextChanged")
    with pytest.raises(WorkerError) as error:
        await desktop.perform(
            "computer_act", act(body, type="invoke", ref=ref(body, "Save"), action_name="click")
        )
    assert error.value.effect == "none" and not desktop.accessibility.inputs


async def test_scoped_observation_keeps_window_input_context(desktop):
    body = await view(desktop)
    expanded = await view(
        desktop, scope_ref=ref(body, "Save"), observation_id=body["observation_id"], detail="full"
    )
    event(desktop.accessibility, path=ENTRY.key[1], member="TextChanged")
    with pytest.raises(WorkerError):
        await desktop.perform(
            "computer_act", act(expanded, type="click", ref=ref(expanded, "Save"))
        )
    assert not desktop.x11.inputs


async def test_text_truncation_is_explicit_and_never_truncates_action_names(desktop):
    desktop.accessibility.nodes[ENTRY.key] = replace(ENTRY, text="x" * 2048)
    body = await view(desktop, max_text_chars=256)
    assert body["projection"]["text_chars"] <= 256
    assert body["projection"]["truncated_fields"] > 0
    entry = next(e for e in body["elements"] if e["ref"] == ref(body, "Title"))
    assert entry["text_truncated"]
    assert next(e for e in body["elements"] if e["name"] == "Save")["actions"] == [
        {"name": "click"}
    ]


@pytest.mark.parametrize(
    "options",
    [
        {"scope_ref": "e1"},
        {"observation_id": "prior"},
        {"max_elements": 301},
        {"max_text_chars": 24001},
    ],
)
def test_invalid_expansion_contract_is_rejected(options):
    with pytest.raises(ValidationError):
        ObserveArguments(**options)


async def test_full_projection_preserves_distinct_metadata_for_duplicate_native_action_names(
    desktop,
):
    desktop.accessibility.nodes[BUTTON.key] = replace(
        BUTTON,
        actions=(
            NativeAction("click", "First action", index=0),
            NativeAction("click", "Different action", index=1),
        ),
    )
    body = await view(desktop, detail="full")
    button = next(e for e in body["elements"] if e["name"] == "Save")
    assert [action["description"] for action in button["actions"]] == [
        "First action",
        "Different action",
    ]
    with pytest.raises(WorkerError):
        await desktop.perform(
            "computer_act", act(body, type="invoke", ref=button["ref"], action_name="click")
        )
