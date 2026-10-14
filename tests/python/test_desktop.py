import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from meerkat_computer.desktop import Desktop
from meerkat_computer.desktop_atspi import (
    ACCESSIBLE,
    ACTION,
    COMPONENT,
    EDITABLE,
    ROOT,
    TEXT,
    AccessibilityUnavailable,
    Atspi,
    NativeAction,
    Node,
    Tree,
    decode_states,
    object_id,
)
from meerkat_computer.desktop_x11 import X11, DisplayState, x11_key
from meerkat_computer.protocol import ActArguments, Lease, ObserveArguments, WorkerError

APP = Node((":1.2", ROOT), "Native fixture", "application", 75, (), (), child_count=1)
WINDOW = Node(
    (":1.2", "/window"),
    "Editor",
    "frame",
    23,
    ("active", "enabled", "sensitive", "showing", "visible"),
    (COMPONENT,),
    bounds=(0, 0, 640, 480),
)
ENTRY = Node(
    (":1.2", "/entry"),
    "Title",
    "text",
    61,
    ("editable", "enabled", "focusable", "sensitive", "showing", "visible"),
    (COMPONENT, EDITABLE, TEXT),
    bounds=(20, 30, 200, 40),
    text="Initial title",
)
BUTTON = Node(
    (":1.2", "/button"),
    "Save",
    "push button",
    43,
    ("enabled", "focusable", "sensitive", "showing", "visible"),
    (COMPONENT, ACTION),
    bounds=(240, 30, 80, 40),
    actions=(NativeAction("click", "Save document", "Alt+s"),),
)


def lease(epoch=1, mode="hybrid", capabilities=None):
    return Lease(
        lease_id=f"lease-{epoch}",
        epoch=epoch,
        expires_at_ms=4_102_444_800_000,
        capabilities=capabilities
        if capabilities is not None
        else (
            ["desktop", "screenshots"]
            if mode == "visual"
            else ["desktop", "desktop_semantics"]
            if mode == "semantic"
            else ["desktop", "screenshots", "desktop_semantics"]
        ),
        observation={"surface": "desktop", "mode": mode, "width": 640, "height": 480},
    )


class FakeAccessibility(Atspi):
    def __init__(self):
        super().__init__()
        self.available = True
        self.revision = 0
        self.structural_revision = 0
        self.nodes = {node.key: node for node in [APP, WINDOW, ENTRY, BUTTON]}
        self.parents = {
            APP.key: None,
            WINDOW.key: APP.key,
            ENTRY.key: WINDOW.key,
            BUTTON.key: WINDOW.key,
        }
        self.inputs = []
        self.snapshots = 0
        self.reads = 0
        self.mutate_during_snapshot = 0
        self.fail_input = False

    async def start(self):
        pass

    async def close(self):
        self.available = False

    async def snapshot(self, *, root=None, parent=None, window=None):
        self.snapshots += 1
        if self.mutate_during_snapshot:
            self.revision += 1
            self.structural_revision += 1
            self.mutate_during_snapshot -= 1
        if not self.available:
            return Tree(issues=["accessibility_bus_unavailable"])
        for node in self.nodes.values():
            self.remember_role(node.key, node.role_id)
            self.remember_location(node.key, self.parents.get(node.key, WINDOW.key))
        keys = set(self.nodes)
        if root is not None:
            keys = {root.key}
            for _ in range(len(self.nodes)):
                expanded = {key for key, parent in self.parents.items() if parent in keys}
                if expanded <= keys:
                    break
                keys.update(expanded)
        return Tree(
            nodes=[
                (
                    replace(node, revision=self.node_revision(node.key)),
                    self.parents.get(node.key, WINDOW.key),
                    object_id("window_", self._object_windows[node.key])
                    if node.key in self._object_windows
                    else None,
                )
                for node in self.nodes.values()
                if node.key in keys
            ]
        )

    async def read_node(self, key, text_limit=2048):
        self.reads += 1
        if key not in self.nodes:
            raise AccessibilityUnavailable("Target removed")
        return replace(self.nodes[key], revision=self.node_revision(key))

    async def focus(self, node):
        self.inputs.append(("focus", node.key))

    async def fill(self, node, text):
        self.inputs.append(("fill", node.key, text))
        if self.fail_input:
            raise AccessibilityUnavailable("Acknowledgement lost")
        self.nodes[node.key] = replace(node, text=text)
        self.revision += 1

    async def invoke(self, node, index):
        self.inputs.append(("invoke", node.key, index))
        self.revision += 1


class FakeX11:
    def __init__(self):
        self.available = True
        self.screen = DisplayState(640, 480, "123")
        self.inputs = []
        self.screenshots = 0
        self.fail_screenshot = False
        self.fail_click = False

    async def start(self):
        pass

    async def close(self):
        self.available = False

    async def state(self):
        return self.screen

    async def screenshot(self):
        self.screenshots += 1
        if self.fail_screenshot:
            raise OSError("Display capture lost")
        return b"test-png"

    async def capture_with_fence(self):
        return await self.screenshot(), None

    async def click(self, x, y, button="left", count=1):
        self.inputs.append(("click", x, y, button, count))
        if self.fail_click:
            raise OSError("Mouse acknowledgement lost")

    async def type(self, text):
        self.inputs.append(("type", text))

    async def key(self, key):
        self.inputs.append(("key", key))

    async def move(self, x, y):
        self.inputs.append(("move", x, y))

    async def scroll(self, dx, dy):
        self.inputs.append(("scroll", dx, dy))

    async def drag(self, start, end, button="left"):
        self.inputs.append(("drag", start, end, button))


@pytest.fixture
async def desktop(tmp_path):
    driver = Desktop(tmp_path, accessibility=FakeAccessibility(), x11=FakeX11())
    await driver.start()
    await driver.configure(lease())
    yield driver
    await driver.close()


async def observe(driver, surface="desktop"):
    content, metrics = await driver.perform("computer_observe", ObserveArguments(surface=surface))
    return json.loads(content[0]["text"]), content, metrics


def act(body, **action):
    return ActArguments(observation_id=body["observation_id"], action=action)


def ref(body, name):
    return next(node["ref"] for node in body["elements"] if node["name"] == name)


async def test_hybrid_reports_native_app_window_tree_and_full_screen(desktop):
    body, content, metrics = await observe(desktop)
    assert body["surface"] == "desktop" and body["coordinate_space"] == "screen"
    assert (body["width"], body["height"]) == (640, 480)
    assert body["accessibility"]["status"] == "available"
    assert body["applications"][0]["name"] == "Native fixture"
    assert body["windows"][0]["name"] == "Editor"
    assert len(body["elements"]) == 4 and content[1]["mimeType"] == "image/png"
    assert next(e for e in body["elements"] if e["name"] == "Save")["actions"][0]["name"] == "click"
    assert metrics == {"observation_attempts": 1, "observations": 1}


async def test_visual_never_queries_or_returns_semantics(desktop):
    await observe(desktop)
    await desktop.configure(lease(2, "visual"))
    prior = desktop.accessibility.snapshots
    body, content, _ = await observe(desktop)
    assert len(content) == 2
    assert not ({"elements", "applications", "windows", "accessibility"} & body.keys())
    assert desktop.accessibility.snapshots == prior and not desktop.refs
    with pytest.raises(WorkerError, match="semantics capability"):
        await desktop.validate("computer_act", act(body, type="click", ref="e4"))
    assert not desktop.x11.inputs


async def test_semantic_capture_does_not_request_screenshot(desktop):
    await desktop.configure(lease(2, "semantic"))
    body, content, _ = await observe(desktop)
    assert body["elements"] and len(content) == 1 and desktop.x11.screenshots == 0


async def test_inaccessible_app_remains_visually_usable(desktop):
    desktop.accessibility.available = False
    body, content, _ = await observe(desktop)
    assert body["accessibility"]["status"] == "unavailable" and body["elements"] == []
    assert len(content) == 2
    _, metrics = await desktop.perform("computer_act", act(body, type="click", x=100, y=120))
    assert desktop.x11.inputs == [("click", 100, 120, "left", 1)]
    assert metrics["clicks"] == 1


@pytest.mark.parametrize("change", ["tree", "focus", "resolution", "disconnect"])
async def test_freshness_fence_rejects_changes_before_any_input(desktop, change):
    body, _, _ = await observe(desktop)
    if change == "tree":
        desktop.accessibility.revision += 1
        desktop.accessibility.structural_revision += 1
    elif change == "focus":
        desktop.x11.screen = replace(desktop.x11.screen, focus="456")
    elif change == "resolution":
        desktop.x11.screen = replace(desktop.x11.screen, width=800)
    else:
        desktop.accessibility.available = False
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="click", x=100, y=120))
    assert error.value.code == "stale_observation" and error.value.effect == "none"
    assert desktop.observation_id == "" and desktop.x11.inputs == []
    assert "ui_command_attempts" not in desktop.operation_metrics


async def test_ref_revalidation_catches_target_change_even_without_event(desktop):
    body, _, _ = await observe(desktop)
    desktop.accessibility.nodes[BUTTON.key] = replace(
        BUTTON, name="Delete", bounds=(20, 20, 50, 50)
    )
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="click", ref=ref(body, "Save")))
    assert error.value.code == "stale_ref" and not desktop.x11.inputs


async def test_removed_target_cannot_be_reused(desktop):
    body, _, _ = await observe(desktop)
    del desktop.accessibility.nodes[ENTRY.key]
    with pytest.raises(WorkerError) as error:
        await desktop.perform(
            "computer_act", act(body, type="fill", ref=ref(body, "Title"), text="New")
        )
    assert error.value.code == "stale_ref" and not desktop.accessibility.inputs


async def test_new_lease_invalidates_refs_and_revoke_denies_input(desktop):
    body, _, _ = await observe(desktop)
    await desktop.configure(lease(2))
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="click", x=10, y=10))
    assert error.value.code == "stale_observation"
    body, _, _ = await observe(desktop)
    await desktop.configure(lease(3, capabilities=[]).model_copy(update={"expires_at_ms": 0}))
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="click", x=10, y=10))
    assert error.value.code == "lease_expired" and not desktop.x11.inputs


async def test_expired_or_missing_capability_denied(desktop):
    await desktop.configure(lease().model_copy(update={"expires_at_ms": 1}))
    with pytest.raises(WorkerError) as error:
        await observe(desktop)
    assert error.value.code == "lease_expired"
    await desktop.configure(lease(capabilities=[]))
    with pytest.raises(WorkerError) as error:
        await observe(desktop)
    assert error.value.code == "capability_denied"


async def test_display_config_failure_drops_old_authority(desktop):
    await observe(desktop)
    desktop.x11.screen = replace(desktop.x11.screen, width=800)
    with pytest.raises(WorkerError) as error:
        await desktop.configure(lease(2))
    assert error.value.code == "desktop_size_mismatch"
    assert desktop.lease is None and desktop.observation_id == ""


async def test_native_fill_uses_advertised_editable_text_and_returns_updated_tree(desktop):
    body, _, _ = await observe(desktop)
    content, metrics = await desktop.perform(
        "computer_act", act(body, type="fill", ref=ref(body, "Title"), text="New title")
    )
    after = json.loads(content[0]["text"])
    assert desktop.accessibility.inputs == [("fill", ENTRY.key, "New title")]
    assert next(e for e in after["elements"] if e["name"] == "Title")["text"] == "New title"
    assert metrics["ui_commands"] == 1 and metrics["native_text_commands"] == 1
    assert desktop.x11.inputs == []


async def test_only_exact_advertised_native_action_can_be_invoked(desktop):
    body, _, _ = await observe(desktop)
    for name in ("press", "shell", "CLICK"):
        with pytest.raises(WorkerError) as error:
            await desktop.perform(
                "computer_act", act(body, type="invoke", ref=ref(body, "Save"), action_name=name)
            )
        assert error.value.code == "invalid_native_action"
    assert not desktop.accessibility.inputs
    _, metrics = await desktop.perform(
        "computer_act", act(body, type="invoke", ref=ref(body, "Save"), action_name="click")
    )
    assert desktop.accessibility.inputs == [("invoke", BUTTON.key, 0)]
    assert metrics["native_invocations"] == 1 and metrics["clicks"] == 0


async def test_disabled_and_readonly_targets_rejected_before_effect(desktop):
    desktop.accessibility.nodes[ENTRY.key] = replace(
        ENTRY, interfaces=(COMPONENT,), states=("enabled", "sensitive", "showing")
    )
    desktop.accessibility.nodes[BUTTON.key] = replace(BUTTON, states=("showing",))
    body, _, _ = await observe(desktop)
    for name, action, code in [
        ("Title", "fill", "not_editable"),
        ("Save", "click", "target_unavailable"),
    ]:
        with pytest.raises(WorkerError) as error:
            await desktop.perform(
                "computer_act",
                act(
                    body,
                    type=action,
                    ref=ref(body, name),
                    **({"text": "x"} if action == "fill" else {}),
                ),
            )
        assert error.value.code == code
    assert not desktop.x11.inputs and not desktop.accessibility.inputs


async def test_double_right_click_and_persisted_metrics_survive_capture_failure(desktop):
    body, _, _ = await observe(desktop)
    acknowledged = []
    desktop.metric_sink = lambda name, amount: acknowledged.append((name, amount))
    desktop.x11.fail_screenshot = True
    with pytest.raises(WorkerError) as error:
        await desktop.perform(
            "computer_act",
            act(body, type="click", ref=ref(body, "Save"), button="right", click_count=2),
        )
    assert error.value.effect == "unknown"
    assert desktop.x11.inputs == [("click", 280, 50, "right", 2)]
    assert (
        desktop.operation_metrics["clicks"] == 2 and desktop.operation_metrics["ui_commands"] == 1
    )
    assert ("clicks", 2) in acknowledged and ("ui_commands", 1) in acknowledged
    assert not desktop.observation_id and not desktop.refs
    with pytest.raises(WorkerError) as replay:
        await desktop.perform("computer_act", act(body, type="click", x=280, y=50))
    assert replay.value.code == "stale_observation" and len(desktop.x11.inputs) == 1


async def test_uncertain_mouse_ack_is_consumed_and_not_counted_as_confirmed(desktop):
    body, _, _ = await observe(desktop)
    desktop.x11.fail_click = True
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="click", x=20, y=30))
    assert error.value.effect == "unknown" and not desktop.observation_id
    assert desktop.operation_metrics["ui_command_attempts"] == 1
    assert (
        desktop.operation_metrics["clicks"] == 0 and "ui_commands" not in desktop.operation_metrics
    )


async def test_uncertain_native_ack_never_replays(desktop):
    body, _, _ = await observe(desktop)
    desktop.accessibility.fail_input = True
    arguments = act(body, type="fill", ref=ref(body, "Title"), text="Something")
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", arguments)
    assert error.value.effect == "unknown"
    with pytest.raises(WorkerError) as replay:
        await desktop.perform("computer_act", arguments)
    assert replay.value.code == "stale_observation" and len(desktop.accessibility.inputs) == 1


async def test_capture_retries_only_readonly_work_to_get_coherent_observation(desktop):
    desktop.accessibility.mutate_during_snapshot = 1
    body, _, _ = await observe(desktop)
    assert desktop.accessibility.snapshots == 2
    await desktop.validate("computer_act", act(body, type="click", x=20, y=30))
    assert not desktop.x11.inputs


async def test_continuous_mutation_yields_no_actionable_observation(desktop):
    await desktop.configure(lease(mode="semantic"))
    desktop.accessibility.mutate_during_snapshot = 4
    with pytest.raises(WorkerError) as error:
        await observe(desktop)
    assert error.value.code == "observation_unstable"
    assert not desktop.observation_id and not desktop.refs and not desktop.x11.inputs


async def test_ref_typing_focuses_native_target_before_x11_input(desktop):
    body, _, _ = await observe(desktop)
    _, metrics = await desktop.perform(
        "computer_act", act(body, type="type", ref=ref(body, "Title"), text="abc")
    )
    assert desktop.accessibility.inputs == [("focus", ENTRY.key)]
    assert desktop.x11.inputs == [("type", "abc")]
    assert metrics["native_focus_commands"] == 1 and metrics["clicks"] == 0


async def test_scroll_and_drag_use_screen_coordinates(desktop):
    body, _, _ = await observe(desktop)
    await desktop.perform(
        "computer_act", act(body, type="scroll", ref=ref(body, "Title"), delta_y=240)
    )
    assert desktop.x11.inputs == [("move", 120, 50), ("scroll", 0, 240)]
    body, _, _ = await observe(desktop)
    await desktop.perform(
        "computer_act", act(body, type="drag", x=1, y=2, end_x=300, end_y=400, button="right")
    )
    assert desktop.x11.inputs[-1] == ("drag", (1, 2), (300, 400), "right")


def test_atspi_states_are_decoded_as_bit_words():
    assert decode_states([(1 << 7) | (1 << 25), (1 << 11)]) == ("editable", "showing", "read_only")


class ReadOnlyAtspi(Atspi):
    def __init__(self, role_id=61):
        super().__init__()
        self.available = True
        self.role_id = role_id
        self.requests = []

    async def properties(self, key, interface):
        if interface == TEXT:
            return {"CharacterCount": 10_000_000}
        return {"Name": "Native field", "ChildCount": 0}

    async def call(self, key, interface, member, signature="", body=None):
        self.requests.append((interface, member, signature, body))
        return {
            "GetRole": [self.role_id],
            "GetRoleName": ["text"],
            "GetState": [[1 << 7, 0]],
            "GetInterfaces": [[TEXT]],
            "GetText": ["a" * (body[1] if body else 0)],
        }[member]


async def test_atspi_text_queries_are_bounded_and_password_text_is_never_requested():
    transport = ReadOnlyAtspi()
    node = await transport.read_node(ENTRY.key, text_limit=80)
    assert len(node.text) == 80 and node.text_truncated
    assert (TEXT, "GetText", "ii", [0, 80]) in transport.requests
    transport = ReadOnlyAtspi(role_id=40)
    assert (await transport.read_node(ENTRY.key)).text is None
    assert all(request[1] != "GetText" for request in transport.requests)


async def test_atspi_reports_application_metadata_truncated_by_read_limits():
    transport = ReadOnlyAtspi()
    original = transport.properties

    async def properties(key, interface):
        if interface == ACCESSIBLE:
            return {
                "Name": "n" * 1000,
                "Description": "d" * 1000,
                "AccessibleId": "i" * 1000,
                "ChildCount": 0,
            }
        return await original(key, interface)

    transport.properties = properties
    node = await transport.read_node(ENTRY.key)
    assert len(node.name) == 300
    assert {"name_truncated", "description_truncated", "accessible_id_truncated"} <= set(
        node.issues
    )


async def test_atspi_tree_budget_reports_truncation(monkeypatch):
    import meerkat_computer.desktop_atspi as implementation

    monkeypatch.setattr(implementation, "MAX_NODES", 2)
    transport = ReadOnlyAtspi()

    async def read_node(key, text_limit=2048):
        return replace(APP, key=key, child_count=5)

    async def call(key, interface, member, signature="", body=None):
        return [[":1.2", f"{key[1]}/child{body[0]}"]]

    transport.read_node = read_node
    transport.call = call
    tree = await transport.snapshot()
    assert len(tree.nodes) == 2 and tree.truncated


def test_key_translation_does_not_accept_xdotool_command_fragments():
    assert x11_key("ControlOrMeta+A") == "ctrl+A"
    assert x11_key("Alt+F4") == "alt+F4"
    assert x11_key("F24") == "F24"
    for value in ["ctrl+a", "Return mousemove 0 0", "Control+", "Meta+;", "F25"]:
        with pytest.raises(WorkerError):
            x11_key(value)


async def test_x11_drag_releases_button_when_mousedown_ack_fails():
    driver = X11(":999")
    commands = []

    async def command(*arguments, **kwargs):
        commands.append(arguments)
        if arguments == ("mousedown", "3"):
            raise OSError("Lost acknowledgement")

    driver.command = command
    with pytest.raises(OSError):
        await driver.drag((1, 2), (3, 4), button="right")
    assert ("mousedown", "3") in commands and commands[-1] == ("mouseup", "3")


@pytest.mark.parametrize("text", ["print(str((1, 2)))", ")" * 16384])
async def test_x11_typing_spaces_repeated_keys_and_keeps_text_on_stdin(text):
    driver = X11(":999")
    driver.command = AsyncMock()
    await driver.type(text)
    call = driver.command.await_args
    assert call.args == ("type", "--clearmodifiers", "--delay", "2", "--file", "-")
    assert call.kwargs["input_text"] == text
    # The deadline covers the requested delay plus scheduler overhead at the full protocol limit.
    assert max(8, 5 + len(text) * 0.006) <= call.kwargs["timeout"] <= 104


async def test_x11_command_honors_deadline_and_kills_unfinished_input(monkeypatch):
    import meerkat_computer.desktop_x11 as implementation

    stopped = asyncio.Event()
    process = SimpleNamespace(
        returncode=None,
        stdout=SimpleNamespace(read=AsyncMock(return_value=b"")),
    )

    async def wait():
        await stopped.wait()

    def kill():
        process.returncode = -9
        stopped.set()

    process.wait = wait
    process.kill = Mock(side_effect=kill)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(implementation.asyncio, "create_subprocess_exec", spawn)
    driver = X11(":999")
    with pytest.raises(TimeoutError):
        await driver.command("key", "Return", timeout=0.01)
    process.kill.assert_called_once()
    for invalid in (0, -1, 105, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="timeout"):
            await driver.command("key", "Return", timeout=invalid)
    assert spawn.await_count == 1


async def test_filtered_native_action_keeps_original_protocol_index(desktop):
    class ActionAtspi(ReadOnlyAtspi):
        async def call(self, key, interface, member, signature="", body=None):
            if member == "GetInterfaces":
                return [[ACTION]]
            if member == "GetActions":
                return [[["a" * 201, "Oversized name", ""], ["activate", "Activate item", ""]]]
            return await super().call(key, interface, member, signature, body)

    transport = ActionAtspi()
    read = await transport.read_node(BUTTON.key)
    assert len(read.actions) == 1 and read.actions[0].index == 1
    assert read.actions[0].name == "activate" and "actions_truncated" in read.issues
    desktop.accessibility.nodes[BUTTON.key] = replace(BUTTON, actions=read.actions)
    body, _, _ = await observe(desktop)
    await desktop.perform(
        "computer_act", act(body, type="invoke", ref=ref(body, "Save"), action_name="activate")
    )
    assert desktop.accessibility.inputs == [("invoke", BUTTON.key, 1)]


async def test_registry_properties_fall_back_from_empty_getall_to_bounded_get():
    transport = Atspi()
    requests = []

    async def call(key, interface, member, signature="", body=None):
        requests.append((member, body))
        if member == "GetAll":
            return []
        return [
            SimpleNamespace(
                value={"Name": "Desktop", "ChildCount": 0, "Description": "", "AccessibleId": ""}[
                    body[1]
                ]
            )
        ]

    transport.call = call
    properties = await transport.properties(APP.key, ACCESSIBLE)
    assert properties["Name"] == "Desktop" and properties["ChildCount"] == 0
    assert len(requests) == 5 and requests[0] == ("GetAll", [ACCESSIBLE])


async def test_background_window_ref_cannot_click_an_overlapping_foreground_control(desktop):
    desktop.accessibility.nodes[WINDOW.key] = replace(
        WINDOW, states=("enabled", "sensitive", "showing", "visible")
    )
    body, _, _ = await observe(desktop)
    for kind in ("click", "scroll", "drag"):
        arguments = {"end_x": 400, "end_y": 400} if kind == "drag" else {}
        with pytest.raises(WorkerError) as error:
            await desktop.perform(
                "computer_act", act(body, type=kind, ref=ref(body, "Save"), **arguments)
            )
        assert error.value.code == "target_window_inactive"
    assert not desktop.x11.inputs and not desktop.accessibility.inputs
    # Direct native dispatch addresses the exact observed object without a positional click.
    await desktop.perform(
        "computer_act", act(body, type="invoke", ref=ref(body, "Save"), action_name="click")
    )
    assert desktop.accessibility.inputs == [("invoke", BUTTON.key, 0)]


async def test_window_losing_active_state_without_event_rejects_positional_ref(desktop):
    body, _, _ = await observe(desktop)
    desktop.accessibility.nodes[WINDOW.key] = replace(
        WINDOW, states=("enabled", "sensitive", "showing", "visible")
    )
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="click", ref=ref(body, "Save")))
    assert error.value.code == "stale_ref" and not desktop.x11.inputs


async def test_same_lease_renewal_preserves_current_refs_but_checks_display(desktop):
    body, _, _ = await observe(desktop)
    renewed = lease().model_copy(update={"expires_at_ms": lease().expires_at_ms + 60_000})
    await desktop.configure(renewed)
    assert desktop.observation_id == body["observation_id"]
    await desktop.validate("computer_act", act(body, type="click", ref=ref(body, "Save")))
    desktop.x11.screen = replace(desktop.x11.screen, width=800)
    with pytest.raises(WorkerError) as error:
        await desktop.configure(renewed)
    assert error.value.code == "desktop_size_mismatch"
    assert desktop.lease is None and desktop.observation_id == ""


@pytest.mark.parametrize(
    "kind,extra", [("type", {"text": "private input"}), ("key", {"key": "Control+A"})]
)
async def test_background_window_cannot_receive_ref_based_x11_keyboard_input(desktop, kind, extra):
    desktop.accessibility.nodes[WINDOW.key] = replace(
        WINDOW, states=("enabled", "sensitive", "showing", "visible")
    )
    body, _, _ = await observe(desktop)
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type=kind, ref=ref(body, "Title"), **extra))
    assert error.value.code == "target_window_inactive"
    assert "observe again" in error.value.message
    assert not desktop.x11.inputs and not desktop.accessibility.inputs
    # A separate semantic focus command can be followed by a fresh observation.
    await desktop.perform("computer_act", act(body, type="focus", ref=ref(body, "Title")))
    assert desktop.accessibility.inputs == [("focus", ENTRY.key)]
