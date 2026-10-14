"""Bounded AT-SPI2 D-Bus client, using the GNOME interface contracts.

Protocol source: https://gitlab.gnome.org/GNOME/at-spi2-core/-/tree/main/xml
The application owns roles, names, states and actions. This client projects them.
"""

import asyncio
import contextlib
import hashlib
import os
from collections import deque
from dataclasses import dataclass, field

ACCESSIBLE = "org.a11y.atspi.Accessible"
COMPONENT = "org.a11y.atspi.Component"
ACTION = "org.a11y.atspi.Action"
TEXT = "org.a11y.atspi.Text"
EDITABLE = "org.a11y.atspi.EditableText"
REGISTRY = "org.a11y.atspi.Registry"
ROOT = "/org/a11y/atspi/accessible/root"
NULL = "/org/a11y/atspi/null"
MAX_NODES = 300
MAX_CHILDREN = 128
MAX_DEPTH = 24
MAX_TEXT = 2048
MAX_TOTAL_TEXT = 24_000
MAX_EVENT_OBJECTS = 4096

# AtspiStateType bit positions, not a second accessibility ontology.
STATE_NAMES = (
    "invalid",
    "active",
    "armed",
    "busy",
    "checked",
    "collapsed",
    "defunct",
    "editable",
    "enabled",
    "expandable",
    "expanded",
    "focusable",
    "focused",
    "has_tooltip",
    "horizontal",
    "iconified",
    "modal",
    "multi_line",
    "multiselectable",
    "opaque",
    "pressed",
    "resizable",
    "selectable",
    "selected",
    "sensitive",
    "showing",
    "single_line",
    "stale",
    "transient",
    "vertical",
    "visible",
    "manages_descendants",
    "indeterminate",
    "required",
    "truncated",
    "animated",
    "invalid_entry",
    "supports_autocompletion",
    "selectable_text",
    "is_default",
    "visited",
    "checkable",
    "has_popup",
    "read_only",
)
WINDOW_ROLES = {16, 23, 69}  # dialog, frame, window
PASSWORD_ROLE = 40


def decode_states(words: list[int]) -> tuple[str, ...]:
    return tuple(
        STATE_NAMES[index] if index < len(STATE_NAMES) else f"state_{index}"
        for index in range(min(len(words), 4) * 32)
        if words[index // 32] & (1 << (index % 32))
    )


def object_id(prefix: str, key: tuple[str, str]) -> str:
    return prefix + hashlib.sha256((key[0] + "\0" + key[1]).encode()).hexdigest()[:20]


class AccessibilityUnavailable(Exception):
    """A bounded protocol failure whose message never includes application data."""


@dataclass(frozen=True)
class NativeAction:
    name: str
    description: str = ""
    key_binding: str = ""
    index: int = 0

    def public(self):
        return {"name": self.name, "description": self.description, "key_binding": self.key_binding}


@dataclass(frozen=True)
class Node:
    key: tuple[str, str]
    name: str
    role: str
    role_id: int
    states: tuple[str, ...]
    interfaces: tuple[str, ...]
    child_count: int = 0
    bounds: tuple[int, int, int, int] | None = None
    actions: tuple[NativeAction, ...] = ()
    text: str | None = None
    text_truncated: bool = False
    description: str = ""
    accessible_id: str = ""
    issues: tuple[str, ...] = ()
    revision: int = 0


@dataclass
class Tree:
    nodes: list[tuple[Node, tuple[str, str] | None, str | None]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    truncated: bool = False


class Atspi:
    def __init__(self, address: str | None = None):
        self.address = address
        self.bus = None
        self.available = False
        self.revision = 0
        self.structural_revision = 0
        self._object_revisions: dict[tuple[str, str], int] = {}
        self._structural_objects: set[tuple[str, str]] = set()
        self._roles: dict[tuple[str, str], int] = {}
        self._parents: dict[tuple[str, str], tuple[str, str] | None] = {}
        self._object_windows: dict[tuple[str, str], tuple[str, str]] = {}
        self.window_revisions: dict[tuple[str, str], int] = {}
        self._checked_states: dict[tuple[str, str], int] = {}
        self.start_issue = None
        self._disconnect_task = None

    async def start(self):
        from dbus_next.aio import MessageBus

        self._checked_states.clear()
        address = self.address or os.environ.get("AT_SPI_BUS_ADDRESS")
        session = None
        try:
            if not address:
                session = await asyncio.wait_for(MessageBus().connect(), 3)
                result = await self._call_on(
                    session, "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress"
                )
                address = result[0]
            self.bus = await asyncio.wait_for(MessageBus(bus_address=address).connect(), 3)
            self.bus.add_message_handler(self._event)
            await self._call_on(
                self.bus,
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "AddMatch",
                "s",
                ["type='signal'"],
            )
            for event in ("object:", "window:", "focus:"):
                await self._call_on(
                    self.bus,
                    REGISTRY,
                    "/org/a11y/atspi/registry",
                    REGISTRY,
                    "RegisterEvent",
                    "sass",
                    [event, [], ""],
                )
            self.available = True
            self.start_issue = None
            self._disconnect_task = asyncio.create_task(self._watch_disconnect())
        except (TimeoutError, AccessibilityUnavailable, OSError, ValueError):
            await self.close()
            self.start_issue = "accessibility_bus_unavailable"
        finally:
            if session:
                session.disconnect()

    async def _watch_disconnect(self):
        try:
            await self.bus.wait_for_disconnect()
        except Exception:  # noqa: BLE001 - D-Bus failures must not leak application messages.
            self.start_issue = "accessibility_bus_disconnected"
        self.available = False
        self._checked_states.clear()
        self.revision += 1
        self.structural_revision += 1

    async def close(self):
        self.available = False
        self._checked_states.clear()
        self.revision += 1
        self.structural_revision += 1
        if self._disconnect_task:
            self._disconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._disconnect_task
            self._disconnect_task = None
        if self.bus:
            self.bus.disconnect()
            self.bus = None

    def _duplicate_checked_event(self, message, key: tuple[str, str]) -> bool:
        body = message.body
        detail = body[0] if body else None
        object_event = message.interface == "org.a11y.atspi.Event.Object"
        if (
            not object_event
            or message.member == "ChildrenChanged"
            or (message.member == "StateChanged" and detail == "defunct")
            or (message.member == "PropertyChange" and detail in ("widget", "accessible-role"))
        ):
            # A removed/replaced ancestor may be outside our bounded tree.
            # Never carry a remembered state across a possible object lifetime.
            self._checked_states.clear()
        if not (
            object_event
            and message.member == "StateChanged"
            and detail == "checked"
            and len(body) >= 3
            and type(body[1]) is int
            and body[1] in (0, 1)
            and type(body[2]) is int
            and body[2] == 0
            and key[0].startswith(":")
            and key[1].startswith("/")
            and key[1] not in (ROOT, NULL)
        ):
            self._checked_states.pop(key, None)
            return False
        value = body[1]
        if self._checked_states.get(key) == value:
            # GTK menu traversal can emit the same checked value on every read.
            # First notifications and every value transition still invalidate.
            return True
        if key not in self._checked_states and len(self._checked_states) >= MAX_EVENT_OBJECTS:
            self._checked_states.clear()
        self._checked_states[key] = value
        return False

    def _event(self, message):
        from dbus_next import MessageType

        if message.message_type == MessageType.SIGNAL and (
            (message.interface or "").startswith("org.a11y.atspi.Event.")
            or (
                message.interface == "org.freedesktop.DBus" and message.member == "NameOwnerChanged"
            )
        ):
            key = (message.sender or "", message.path or "")
            if self._duplicate_checked_event(message, key):
                return
            self.revision += 1
            if (
                key not in self._object_revisions
                and len(self._object_revisions) >= MAX_EVENT_OBJECTS
            ):
                # Bounded event memory; eviction invalidates every outstanding fence.
                self._object_revisions.clear()
                self._checked_states.clear()
                self.structural_revision += 1
            self._object_revisions[key] = self.revision
            detail = message.body[0] if message.body else None
            content_only = message.interface == "org.a11y.atspi.Event.Object" and (
                message.member in {"TextChanged", "TextCaretMoved", "TextSelectionChanged"}
                or (
                    message.member == "ChildrenChanged"
                    and key[0]
                    and key[1]
                    and key[1] != ROOT
                    and key not in self._structural_objects
                )
                or (
                    message.member == "PropertyChange"
                    and isinstance(detail, str)
                    and detail in {"accessible-name", "accessible-description"}
                )
            )
            window = self._object_windows.get(key)
            # Scope only known source ancestry. Unknown sources and native
            # window/focus/state/bounds changes retain the global fence.
            if content_only and window is not None:
                self.window_revisions[window] = self.revision
                if message.member == "ChildrenChanged" and len(message.body) > 3:
                    child = getattr(message.body[3], "value", message.body[3])
                    if (
                        isinstance(child, (list, tuple))
                        and len(child) == 2
                        and all(isinstance(value, str) for value in child)
                    ):
                        child_key = (child[0] or key[0], child[1])
                        affected = {child_key}
                        for _ in range(MAX_DEPTH):
                            descendants = {
                                node for node, parent in self._parents.items() if parent in affected
                            }
                            if descendants <= affected:
                                break
                            affected.update(descendants)
                        for node in affected:
                            self._object_revisions[node] = self.revision
                            self._object_windows.pop(node, None)
                        if len(self._object_revisions) > MAX_EVENT_OBJECTS:
                            self._object_revisions.clear()
                            self._checked_states.clear()
                            self.structural_revision += 1
            else:
                self.structural_revision += 1

    def node_revision(self, key: tuple[str, str]) -> int:
        return self._object_revisions.get(key, 0)

    def remember_role(self, key: tuple[str, str], role_id: int):
        self._roles[key] = role_id
        if role_id in WINDOW_ROLES | {75}:
            if len(self._structural_objects) >= MAX_EVENT_OBJECTS:
                self._structural_objects.clear()
                self._checked_states.clear()
                self.structural_revision += 1
            self._structural_objects.add(key)

    def remember_location(self, key: tuple[str, str], parent: tuple[str, str] | None):
        if key not in self._parents and len(self._parents) >= MAX_EVENT_OBJECTS:
            self._parents.clear()
            self._roles.clear()
            self._object_windows.clear()
            self.window_revisions.clear()
            self._checked_states.clear()
            self.structural_revision += 1
        self._parents[key] = parent
        window = key if self._roles.get(key) in WINDOW_ROLES else self._object_windows.get(parent)
        if window is not None:
            self._object_windows[key] = window
        else:
            self._object_windows.pop(key, None)

    async def _call_on(self, bus, destination, path, interface, member, signature="", body=None):
        from dbus_next import Message, MessageType

        try:
            reply = await asyncio.wait_for(
                bus.call(
                    Message(
                        destination=destination,
                        path=path,
                        interface=interface,
                        member=member,
                        signature=signature,
                        body=body or [],
                    )
                ),
                1.5,
            )
        except (TimeoutError, OSError, EOFError) as exc:
            raise AccessibilityUnavailable("Accessibility request did not complete") from exc
        if reply.message_type == MessageType.ERROR:
            raise AccessibilityUnavailable("Accessibility request was rejected")
        return reply.body

    async def call(self, key, interface, member, signature="", body=None):
        if not self.available or not self.bus:
            raise AccessibilityUnavailable("Accessibility bus is unavailable")
        result = await self._call_on(self.bus, key[0], key[1], interface, member, signature, body)
        if member != "GetAll" and len(result) != 1:
            raise AccessibilityUnavailable("Accessibility response has an unsupported shape")
        return result

    async def properties(self, key, interface):
        properties_interface = "org.freedesktop.DBus.Properties"
        value = None
        try:
            value = await self.call(key, properties_interface, "GetAll", "s", [interface])
        except AccessibilityUnavailable:
            value = None
        if value and len(value) == 1 and isinstance(value[0], dict):
            return {name: variant.value for name, variant in value[0].items()}
        # Ubuntu's AT-SPI registry supports Properties.Get but returns an empty
        # method body for GetAll. Ask only for the bounded set of fields we use.
        names = (
            ("Name", "ChildCount", "Description", "AccessibleId")
            if interface == ACCESSIBLE
            else ("CharacterCount",)
        )
        required = {"Name", "ChildCount"} if interface == ACCESSIBLE else {"CharacterCount"}
        result = {}
        for name in names:
            try:
                result[name] = (
                    await self.call(key, properties_interface, "Get", "ss", [interface, name])
                )[0].value
            except AccessibilityUnavailable:
                if name in required:
                    raise
        return result

    async def read_node(self, key: tuple[str, str], text_limit: int = MAX_TEXT) -> Node:
        revision = self.node_revision(key)
        properties, role, states, interfaces = await asyncio.gather(
            self.properties(key, ACCESSIBLE),
            self.call(key, ACCESSIBLE, "GetRole"),
            self.call(key, ACCESSIBLE, "GetState"),
            self.call(key, ACCESSIBLE, "GetInterfaces"),
        )
        role_id = int(role[0])
        self.remember_role(key, role_id)
        supported = tuple(interfaces[0])
        issues = []
        try:
            role_name = str((await self.call(key, ACCESSIBLE, "GetRoleName"))[0])
            if len(role_name) > 100:
                issues.append("role_truncated")
                role_name = role_name[:100]
        except AccessibilityUnavailable:
            role_name = f"role_{role_id}"
        bounds, actions, text, text_truncated = None, (), None, False
        if COMPONENT in supported:
            try:
                bounds = tuple((await self.call(key, COMPONENT, "GetExtents", "u", [0]))[0])
            except AccessibilityUnavailable:
                issues.append("bounds_unavailable")
        if ACTION in supported:
            try:
                advertised = (await self.call(key, ACTION, "GetActions"))[0]
                actions = tuple(
                    NativeAction(str(a[0]), str(a[1])[:300], str(a[2])[:100], index)
                    for index, a in enumerate(advertised[:32])
                    if 0 < len(str(a[0])) <= 200
                )
                if len(advertised) != len(actions):
                    issues.append("actions_truncated")
                if any(len(str(a[1])) > 300 or len(str(a[2])) > 100 for a in advertised[:32]):
                    issues.append("action_details_truncated")
            except AccessibilityUnavailable:
                issues.append("actions_unavailable")
        if TEXT in supported and role_id != PASSWORD_ROLE:
            try:
                count = max(0, int((await self.properties(key, TEXT))["CharacterCount"]))
                limit = min(count, text_limit)
                text = str((await self.call(key, TEXT, "GetText", "ii", [0, limit]))[0])[:limit]
                text_truncated = count > limit
            except (AccessibilityUnavailable, KeyError, ValueError):
                issues.append("text_unavailable")
        decoded_states = decode_states(states[0])
        if revision != self.node_revision(key):
            issues.append("node_changed_during_read")
        elif key in self._checked_states and self._checked_states[key] != int(
            "checked" in decoded_states
        ):
            # A stable query contradicted the last event. Forget it rather than
            # suppressing a later transition, or replacing a newer in-flight event.
            self._checked_states.pop(key, None)
        for source, public in (
            ("Name", "name"),
            ("Description", "description"),
            ("AccessibleId", "accessible_id"),
        ):
            if len(str(properties.get(source, ""))) > 300:
                issues.append(public + "_truncated")
        return Node(
            key=key,
            name=str(properties.get("Name", ""))[:300],
            role=role_name,
            role_id=role_id,
            states=decoded_states,
            interfaces=supported,
            child_count=max(0, int(properties.get("ChildCount", 0))),
            bounds=bounds,
            actions=actions,
            text=text,
            text_truncated=text_truncated,
            description=str(properties.get("Description", ""))[:300],
            accessible_id=str(properties.get("AccessibleId", ""))[:300],
            issues=tuple(issues),
            revision=revision,
        )

    async def snapshot(self, *, root: Node | None = None, parent=None, window=None) -> Tree:
        tree = Tree()
        initial = (root.key if root is not None else (REGISTRY, ROOT), parent, window, 0)
        if not self.available:
            tree.issues.append(self.start_issue or "accessibility_bus_unavailable")
            return tree

        async def walk():
            pending = deque([initial])
            seen = set()
            text_remaining = MAX_TOTAL_TEXT
            while pending and len(tree.nodes) < MAX_NODES:
                key, parent, window, depth = pending.popleft()
                if key in seen or key[1] == NULL:
                    continue
                seen.add(key)
                self.remember_location(key, parent)
                try:
                    node = await self.read_node(key, min(MAX_TEXT, text_remaining))
                except AccessibilityUnavailable:
                    tree.issues.append("node_unavailable")
                    continue
                self.remember_location(key, parent)
                text_remaining -= len(node.text or "")
                if node.role_id in WINDOW_ROLES:
                    window = object_id("window_", key)
                tree.nodes.append((node, parent, window))
                tree.issues.extend(node.issues)
                if depth >= MAX_DEPTH:
                    tree.truncated |= node.child_count > 0
                    continue
                limit = min(node.child_count, MAX_CHILDREN, MAX_NODES - len(tree.nodes))
                tree.truncated |= limit < node.child_count
                for index in range(limit):
                    try:
                        child = (await self.call(key, ACCESSIBLE, "GetChildAtIndex", "i", [index]))[
                            0
                        ]
                        pending.append((tuple(child), key, window, depth + 1))
                    except AccessibilityUnavailable:
                        tree.issues.append("child_unavailable")
            tree.truncated |= bool(pending)

        try:
            await asyncio.wait_for(walk(), 8)
        except TimeoutError:
            tree.truncated = True
            tree.issues.append("traversal_timeout")
        tree.issues = sorted(set(tree.issues))
        return tree

    async def focus(self, node: Node):
        result = await self.call(node.key, COMPONENT, "GrabFocus")
        if not result[0]:
            raise AccessibilityUnavailable("Application did not acknowledge focus")

    async def fill(self, node: Node, text: str):
        result = await self.call(node.key, EDITABLE, "SetTextContents", "s", [text])
        if not result[0]:
            raise AccessibilityUnavailable("Application did not acknowledge text replacement")

    async def invoke(self, node: Node, index: int):
        result = await self.call(node.key, ACTION, "DoAction", "i", [index])
        if not result[0]:
            raise AccessibilityUnavailable("Application did not acknowledge the action")
