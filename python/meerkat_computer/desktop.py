"""Observation-fenced Linux desktop driver for an explicitly provisioned guest."""

import asyncio
import base64
import json
import math
import secrets
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .desktop_atspi import (
    COMPONENT,
    EDITABLE,
    WINDOW_ROLES,
    AccessibilityUnavailable,
    Atspi,
    Node,
    Tree,
)
from .desktop_x11 import (
    MAX_VISUAL_FENCE_SECONDS,
    X11,
    DesktopTopology,
    DisplayState,
    FocusedVisualFence,
    x11_key,
)
from .observation import Projection, desktop_projection
from .protocol import Lease, WorkerError


@dataclass(frozen=True)
class Fence:
    revision: int | None
    accessibility_available: bool | None
    screen: DisplayState


class Desktop:
    def __init__(
        self,
        state_dir: Path,
        *,
        display: str | None = None,
        bus_address: str | None = None,
        accessibility=None,
        x11=None,
    ):
        self.state_dir = state_dir
        self.accessibility = accessibility if accessibility is not None else Atspi(bus_address)
        self.x11 = x11 if x11 is not None else X11(display)
        self.lease: Lease | None = None
        self.observation_id = ""
        self.observation_signature: Fence | None = None
        self.observation_revision: int | None = None
        self.observation_window_revisions: dict[tuple[str, str], int] = {}
        self.observation_tree = Tree()
        self.observation_visual: FocusedVisualFence | None = None
        self.observation_topology: DesktopTopology | None = None
        self.refs: dict[str, Node] = {}
        self.ref_windows: dict[str, Node] = {}
        self.metric_sink = None
        self.persists_metrics = True
        self.operation_metrics = {}

    def record(self, name: str, amount: int = 1):
        self.operation_metrics[name] = self.operation_metrics.get(name, 0) + amount
        if self.metric_sink:
            self.metric_sink(name, amount)

    async def start(self):
        # No daemon or display is started here. The host explicitly supplies an isolated session.
        await self.x11.start()
        try:
            await self.accessibility.start()
        except BaseException:
            await self.x11.close()
            raise

    async def close(self):
        await self.invalidate()
        try:
            await self.accessibility.close()
        finally:
            await self.x11.close()

    def ready(self) -> bool:
        return self.x11.available

    async def configure(self, lease: Lease):
        previous = self.lease
        changed = previous is None or (
            previous.lease_id,
            previous.epoch,
            previous.capabilities,
            previous.observation,
        ) != (lease.lease_id, lease.epoch, lease.capabilities, lease.observation)
        if changed:
            await self.invalidate()
        # Drop the old authority even if applying the new display configuration fails.
        self.lease = None
        try:
            if "desktop" in lease.capabilities:
                screen = await self.x11.state()
                if (screen.width, screen.height) != (
                    lease.observation.width,
                    lease.observation.height,
                ):
                    raise WorkerError(
                        "desktop_size_mismatch",
                        "Guest display does not match profile dimensions",
                        409,
                    )
        except BaseException:
            await self.invalidate()
            raise
        self.lease = lease

    async def invalidate(self):
        self.observation_id = ""
        self.observation_signature = None
        self.observation_revision = None
        self.observation_window_revisions = {}
        self.observation_tree = Tree()
        self.observation_visual = None
        self.observation_topology = None
        self.refs = {}
        self.ref_windows = {}

    def authorize(self):
        if not self.lease or self.lease.expires_at_ms <= int(time.time() * 1000):
            raise WorkerError("lease_expired", "A current computer lease is required", 403)
        capabilities = self.lease.capabilities
        if "desktop" not in capabilities:
            raise WorkerError("capability_denied", "Desktop capability is required", 403)
        mode = self.lease.observation.mode
        if mode in {"visual", "hybrid"} and "screenshots" not in capabilities:
            raise WorkerError("capability_denied", "Screenshot capability is required", 403)
        if mode in {"semantic", "hybrid"} and "desktop_semantics" not in capabilities:
            raise WorkerError("capability_denied", "Desktop semantics capability is required", 403)
        if not self.ready():
            raise WorkerError("desktop_unavailable", "Guest desktop is unavailable", 503)

    async def signature(self) -> Fence:
        screen = await self.x11.state()
        if self.lease.observation.mode == "visual":
            # A screenshot grants no semantic target. Native window topology,
            # not unrelated AT-SPI activity, fences visual input below.
            return Fence(None, None, screen)
        return Fence(self.accessibility.structural_revision, self.accessibility.available, screen)

    def signature_changes(self, current: Fence) -> tuple[str, ...]:
        previous = self.observation_signature
        return tuple(
            reason
            for reason, changed in (
                (
                    "display",
                    (current.screen.width, current.screen.height)
                    != (previous.screen.width, previous.screen.height),
                ),
                ("focus", current.screen.focus != previous.screen.focus),
                (
                    "accessibility_connection",
                    current.accessibility_available != previous.accessibility_available,
                ),
                (
                    "accessibility_events"
                    if self.lease.observation.mode == "visual"
                    else "accessibility_structure",
                    current.revision != previous.revision,
                ),
            )
            if changed
        )

    async def verify_node(self, node: Node):
        try:
            current = await self.accessibility.read_node(
                node.key, text_limit=len(node.text) if node.text is not None else 2048
            )
        except AccessibilityUnavailable as exc:
            await self.invalidate()
            raise WorkerError(
                "stale_ref", "Native target is no longer available", reasons=("native_target",)
            ) from exc
        reasons = (
            ("native_target",)
            if (current != node or "node_changed_during_read" in node.issues)
            else self.signature_changes(await self.signature())
        )
        if reasons:
            await self.invalidate()
            raise WorkerError(
                "stale_ref",
                "Native target changed since observation; observe again",
                reasons=reasons,
            )

    @staticmethod
    def input_context(tree: Tree, point: tuple[int, int] | None, navigation: bool):
        windows = {window: node for node, _, window in tree.nodes if node.role_id in WINDOW_ROLES}
        targets = []
        for node, parent, window_id in tree.nodes:
            window = windows.get(window_id)
            if window is None or "active" not in window.states:
                continue
            if navigation:
                relevant = "focusable" in node.states
            elif point is None:
                relevant = "focused" in node.states
            else:
                bounds = node.bounds
                relevant = bool(
                    bounds
                    and bounds[0] <= point[0] < bounds[0] + bounds[2]
                    and bounds[1] <= point[1] < bounds[1] + bounds[3]
                    and "showing" in node.states
                )
            if relevant:
                targets.append((node, parent))
        return targets, list(windows.values())

    async def verify_input_context(
        self,
        checked: set[tuple[str, str] | None],
        point: tuple[int, int] | None = None,
        *,
        navigation=False,
        allow_visual=False,
    ):
        """Known hit/focus targets use their native window's complete event fence."""
        targets, _ = self.input_context(self.observation_tree, point, navigation)
        known = any(node.role_id not in WINDOW_ROLES | {75} for node, _ in targets)
        if not known:
            if (
                allow_visual
                and self.observation_visual is not None
                and self.lease.observation.mode == "hybrid"
            ):
                bounds = self.observation_visual.visible_bounds
                inside = point is None or (
                    bounds[0] <= point[0] < bounds[0] + bounds[2]
                    and bounds[1] <= point[1] < bounds[1] + bounds[3]
                )
                if inside:
                    _, current = await self.x11.capture_with_fence()
                    previous = self.observation_visual
                    reasons = tuple(
                        reason
                        for reason, changed in (
                            ("visual_unavailable", current is None),
                            (
                                "visual_topology",
                                current is not None and current.topology != previous.topology,
                            ),
                            (
                                "visual_pixels",
                                current is not None and current.rgb_sha256 != previous.rgb_sha256,
                            ),
                            (
                                "accessibility_structure",
                                self.accessibility.structural_revision
                                != self.observation_signature.revision,
                            ),
                            (
                                "accessibility_connection",
                                self.accessibility.available
                                != self.observation_signature.accessibility_available,
                            ),
                            (
                                "accessibility_events",
                                "unstable_tree" in self.observation_tree.issues
                                and self.accessibility.revision != self.observation_revision,
                            ),
                        )
                        if changed
                    )
                    if reasons:
                        await self.invalidate()
                        raise WorkerError(
                            "stale_observation",
                            "Native visual input context changed; observe again",
                            reasons=reasons,
                        )
                    return
            if "unstable_tree" in self.observation_tree.issues:
                raise WorkerError(
                    "stale_observation",
                    "Unstable accessibility requires an unchanged focused visual target; observe again",
                    reasons=(
                        "visual_unavailable"
                        if self.observation_visual is None
                        else "visual_target_outside",
                    ),
                )
            checked.add(None)
            # Visual-only, inaccessible or unrepresented input cannot prove a
            # local target. Preserve the former full event fence there.
            if self.accessibility.revision != self.observation_revision:
                await self.invalidate()
                raise WorkerError(
                    "stale_observation",
                    "Desktop input context changed; observe again",
                    reasons=("accessibility_events",),
                )
            return
        target_keys = {node.key for node, _ in targets}
        owning_windows = {
            window: node
            for node, _, window in self.observation_tree.nodes
            if node.role_id in WINDOW_ROLES
        }
        windows = {
            owning_windows[window].key: owning_windows[window]
            for node, _, window in self.observation_tree.nodes
            if node.key in target_keys and window in owning_windows
        }
        for window in windows.values():
            await self.verify_window(window, checked)
        for node, _ in targets:
            await self.verify_node(node)

    async def verify_window(self, window: Node | None, checked: set[tuple[str, str] | None]):
        checked.add(window.key if window is not None else None)
        if window is None:
            changed = self.accessibility.revision != self.observation_revision
        else:
            changed = self.accessibility.window_revisions.get(window.key, 0) != (
                self.observation_window_revisions.get(window.key, 0)
            )
        if changed:
            await self.invalidate()
            raise WorkerError(
                "stale_ref",
                "Native owning window changed; observe again",
                reasons=("native_window" if window is not None else "accessibility_events",),
            )
        if window is not None:
            await self.verify_node(window)

    async def finish_validation(self, checked: set[tuple[str, str] | None]):
        # No awaited read follows this comparison. A scoped event delivered
        # during the last D-Bus target/window read must still fence admission.
        changed = {
            "accessibility_events" if key is None else "native_window"
            for key in checked
            if (
                self.accessibility.revision != self.observation_revision
                if key is None
                else self.accessibility.window_revisions.get(key, 0)
                != self.observation_window_revisions.get(key, 0)
            )
        }
        if changed:
            await self.invalidate()
            raise WorkerError(
                "stale_ref",
                "Native owning window changed; observe again",
                reasons=tuple(sorted(changed)),
            )

    async def verify_visual_input(self, action):
        """Bind visual input to native window topology, not immutable canvas pixels."""
        if action.type == "scroll" and action.x is None:
            raise WorkerError(
                "invalid_coordinates",
                "Visual desktop scrolling requires screen coordinates",
                422,
            )
        previous = self.observation_topology
        reasons = ()
        if previous is None:
            reasons = ("visual_unavailable",)
        else:
            try:
                # At most two bounded topology reads per validation, regardless
                # of the number of drag points. No screenshot or semantic read.
                async with asyncio.timeout(MAX_VISUAL_FENCE_SECONDS):
                    before = await self.x11.desktop_topology()
                    after = await self.x11.desktop_topology()
                    current = await self.signature()
                reasons = self.signature_changes(current)
                if before is None or after is None:
                    reasons += ("visual_unavailable",)
                elif before != previous or after != previous:
                    reasons += ("visual_topology",)
            except TimeoutError:
                reasons = ("visual_unavailable",)
        if reasons:
            await self.invalidate()
            raise WorkerError(
                "stale_observation",
                "Native desktop window context changed or is unavailable; observe again",
                reasons=reasons,
            )
        self.authorize()

    def point(self, x: float, y: float) -> tuple[int, int]:
        size = self.lease.observation
        if (
            x is None
            or y is None
            or not math.isfinite(x)
            or not math.isfinite(y)
            or not (0 <= x < size.width and 0 <= y < size.height)
        ):
            raise WorkerError("invalid_coordinates", "Coordinates must be inside the desktop", 422)
        return int(x), int(y)

    def center(self, node: Node) -> tuple[int, int]:
        if not node.bounds or node.bounds[2] <= 0 or node.bounds[3] <= 0:
            raise WorkerError("target_unavailable", "Target has no usable screen bounds", 422)
        x, y, width, height = node.bounds
        return self.point(x + width / 2, y + height / 2)

    async def validate(self, name: str, arguments):
        self.authorize()
        if name == "computer_observe":
            # The shared surface router owns the optional arguments.surface selector.
            Projection.resolve(self.lease.observation, arguments)
            if arguments.scope_ref is not None:
                if self.lease.observation.mode == "visual":
                    raise WorkerError(
                        "capability_denied", "Scoped expansion requires semantics", 403
                    )
                if arguments.observation_id != self.observation_id or not self.observation_id:
                    raise WorkerError("stale_observation", "Expand a current observation ref")
                node = self.refs.get(arguments.scope_ref)
                if node is None:
                    raise WorkerError("stale_ref", "Scope is not in the current observation")
                checked = set()
                await self.verify_node(node)
                await self.verify_window(self.ref_windows.get(arguments.scope_ref), checked)
                await self.finish_validation(checked)
            return
        if name != "computer_act":
            raise WorkerError("unsupported_tool", "Tool is unavailable on the desktop", 422)
        await self.validate_action(arguments)

    async def validate_action(self, arguments):
        self.authorize()
        action = arguments.action
        checked: set[tuple[str, str] | None] = set()
        if not self.observation_id or arguments.observation_id != self.observation_id:
            raise WorkerError(
                "stale_observation",
                "Observe the desktop before acting",
                reasons=("observation_id",),
            )
        reasons = self.signature_changes(await self.signature())
        if reasons:
            await self.invalidate()
            raise WorkerError(
                "stale_observation",
                "Desktop tree, focus or display changed; observe again",
                reasons=reasons,
            )
        if action.type not in {"click", "fill", "type", "key", "scroll", "drag", "focus", "invoke"}:
            raise WorkerError("unsupported_action", "Unsupported desktop action", 422)
        if action.x is not None:
            self.point(action.x, action.y)
        if action.type == "drag":
            self.point(action.end_x, action.end_y)
            for point in action.path or ():
                self.point(point.x, point.y)
        if action.key:
            x11_key(action.key)
        if getattr(action, "button", "left") not in {"left", "middle", "right"} or getattr(
            action, "click_count", 1
        ) not in {1, 2}:
            raise WorkerError("invalid_action", "Unsupported mouse button or click count", 422)
        if action.type in {"focus", "invoke"} and not action.ref:
            raise WorkerError(
                "invalid_action", "Native focus and invoke require an observed ref", 422
            )
        if not action.ref:
            if self.lease.observation.mode == "visual":
                await self.verify_visual_input(action)
                return
            if (
                "unstable_tree" in self.observation_tree.issues
                and action.type == "scroll"
                and action.x is None
            ):
                raise WorkerError(
                    "invalid_coordinates",
                    "Scrolling without stable accessibility requires screen coordinates",
                    422,
                )
            if action.x is not None:
                await self.verify_input_context(
                    checked, self.point(action.x, action.y), allow_visual=True
                )
            elif action.type in {"fill", "type", "key"}:
                await self.verify_input_context(
                    checked, navigation=action.key in {"Tab", "Shift+Tab"}, allow_visual=True
                )
            elif self.accessibility.revision != self.observation_revision:
                await self.invalidate()
                raise WorkerError(
                    "stale_observation",
                    "Desktop input context changed; observe again",
                    reasons=("accessibility_events",),
                )
            if action.type == "drag":
                for point in action.path or ():
                    await self.verify_input_context(
                        checked, self.point(point.x, point.y), allow_visual=True
                    )
                await self.verify_input_context(
                    checked, self.point(action.end_x, action.end_y), allow_visual=True
                )
            await self.finish_validation(checked)
            return
        if "desktop_semantics" not in self.lease.capabilities:
            raise WorkerError(
                "capability_denied", "Desktop semantics capability is required for refs", 403
            )
        node = self.refs.get(action.ref)
        if not node:
            raise WorkerError(
                "stale_ref",
                "Reference is not in the current desktop observation",
                reasons=("native_target",),
            )
        await self.verify_node(node)
        window = self.ref_windows.get(action.ref)
        await self.verify_window(window, checked)
        if "defunct" in node.states or "stale" in node.states:
            raise WorkerError(
                "stale_ref", "Native target is defunct or stale", reasons=("native_target",)
            )
        if not {"enabled", "sensitive", "showing"} <= set(node.states):
            raise WorkerError("target_unavailable", "Native target is disabled or not showing", 422)
        if action.type in {"click", "drag", "scroll", "key", "type"} and (
            window is None or "active" not in window.states
        ):
            raise WorkerError(
                "target_window_inactive",
                "X11 input by ref requires an active native owning window; focus the target and observe again, or use direct native fill/invoke or an explicit screen coordinate",
                422,
            )
        if action.type in {"click", "drag", "scroll"}:
            await self.verify_input_context(checked, self.center(node))
        if action.type == "drag":
            for point in action.path or ():
                await self.verify_input_context(checked, self.point(point.x, point.y))
            await self.verify_input_context(checked, self.point(action.end_x, action.end_y))
        if action.type == "key" and action.key in {"Tab", "Shift+Tab"}:
            await self.verify_input_context(checked, navigation=True)
        if action.type in {"focus", "key", "type"} and not (
            COMPONENT in node.interfaces and "focusable" in node.states
        ):
            raise WorkerError(
                "not_focusable", "Native target does not advertise keyboard focus", 422
            )
        if action.type == "fill" and not (
            EDITABLE in node.interfaces and "editable" in node.states
        ):
            raise WorkerError("not_editable", "Native target does not advertise editable text", 422)
        if action.type == "invoke":
            name = getattr(action, "action_name", None)
            if sum(item.name == name for item in node.actions) != 1:
                raise WorkerError(
                    "invalid_native_action", "Choose an exact advertised native action name", 422
                )
        await self.finish_validation(checked)

    async def observe(self, arguments=None) -> list[dict]:
        self.authorize()
        projection = Projection.resolve(self.lease.observation, arguments)
        scope = None
        if arguments is not None and arguments.scope_ref is not None:
            await self.validate("computer_observe", arguments)
            target = self.refs[arguments.scope_ref]
            scope = next(row for row in self.observation_tree.nodes if row[0].key == target.key)
        self.record("observation_attempts")
        await self.invalidate()
        observation = self.lease.observation
        for attempt in range(4 if observation.mode == "hybrid" else 3):
            # A final screenshot can remain useful when semantics never settle.
            # Its empty tree grants no semantic references or weaker input checks.
            fallback = attempt == 3
            revision = self.accessibility.revision
            window_revisions = dict(self.accessibility.window_revisions)
            before = await self.signature()
            if (before.screen.width, before.screen.height) != (
                observation.width,
                observation.height,
            ):
                raise WorkerError("desktop_size_mismatch", "Guest display changed dimensions", 409)
            body = {
                "surface": "desktop",
                "observation_id": secrets.token_hex(16),
                "mode": observation.mode,
                "width": before.screen.width,
                "height": before.screen.height,
                "coordinate_space": "screen",
                "screen_origin": {"x": 0, "y": 0},
                "focused_x11_window": before.screen.focus,
                "consistency": (
                    "desktop_window_topology"
                    if observation.mode == "visual"
                    else "target_revalidation_and_structural_events"
                ),
            }
            new_refs = {}
            new_windows = {}
            tree = Tree()
            if observation.mode in {"semantic", "hybrid"}:
                tree = (
                    Tree(issues=["unstable_tree"])
                    if fallback
                    else await self.accessibility.snapshot()
                )
                if scope is not None and not fallback:
                    expanded = await self.accessibility.snapshot(
                        root=scope[0], parent=scope[1], window=scope[2]
                    )
                    if not any(row[0].key == scope[0].key for row in expanded.nodes):
                        raise WorkerError("stale_ref", "Native scope is no longer available")
                    expanded_root = next(
                        row[0] for row in expanded.nodes if row[0].key == scope[0].key
                    )
                    comparable = expanded_root
                    if scope[0].text is not None and expanded_root.text is not None:
                        comparable = replace(
                            expanded_root,
                            text=expanded_root.text[: len(scope[0].text)],
                            text_truncated=expanded_root.text_truncated
                            or len(expanded_root.text) > len(scope[0].text),
                        )
                    if comparable != scope[0]:
                        raise WorkerError(
                            "stale_ref", "Native scope changed during expansion; observe again"
                        )
                    combined = {
                        node.key: (node, parent, window) for node, parent, window in tree.nodes
                    }
                    combined.update(
                        {
                            node.key: (node, parent, window)
                            for node, parent, window in expanded.nodes
                        }
                    )
                    tree.nodes = list(combined.values())
                    tree.issues.extend(expanded.issues)
                    tree.truncated |= expanded.truncated
                elements, applications, windows, new_refs, new_windows, metadata = (
                    desktop_projection(
                        tree,
                        projection,
                        scope_key=scope[0].key if scope is not None and not fallback else None,
                    )
                )
                issues = list(tree.issues)
                if not fallback and not any(node.role_id == 75 for node, _, _ in tree.nodes):
                    issues.append("no_accessible_applications")
                status = "available"
                if not self.accessibility.available:
                    status = "unavailable"
                elif issues or tree.truncated:
                    status = "partial"
                body.update(
                    elements=elements,
                    applications=applications,
                    windows=windows,
                    projection=metadata,
                    accessibility={
                        "status": status,
                        "truncated": tree.truncated,
                        "issues": sorted(set(issues)),
                        "source": "AT-SPI2",
                        "coverage": "application_provided",
                    },
                )
            screenshot = None
            visual_fence = None
            desktop_topology = None
            if observation.mode == "hybrid":
                screenshot, visual_fence = await self.x11.capture_with_fence()
            elif observation.mode == "visual":
                screenshot, desktop_topology = await self.x11.capture_desktop_with_fence()
            self.authorize()
            after = await self.signature()
            if before.screen != after.screen or (not fallback and before != after):
                continue
            self.authorize()
            if observation.mode == "visual":
                body["input"] = {
                    "status": "observe_only" if desktop_topology is None else "requires_revalidation"
                }
            if fallback:
                # Publish pixels even if accessibility changed during capture,
                # but never turn that unstable interval into input authority.
                if before != after or revision != self.accessibility.revision:
                    visual_fence = None
                body["input"] = {
                    "status": "observe_only" if visual_fence is None else "requires_revalidation"
                }
                self.record("observation_fallbacks")
            self.observation_id = body["observation_id"]
            self.observation_signature = after
            self.observation_revision = revision
            self.observation_window_revisions = window_revisions
            self.observation_tree = tree
            self.observation_visual = visual_fence
            self.observation_topology = desktop_topology
            self.refs = new_refs
            self.ref_windows = new_windows
            content = [{"type": "text", "text": json.dumps(body, ensure_ascii=True)}]
            if screenshot is not None:
                content.append(
                    {
                        "type": "image",
                        "mimeType": "image/png",
                        "data": base64.b64encode(screenshot).decode("ascii"),
                    }
                )
            self.record("observations")
            return content
        raise WorkerError("observation_unstable", "Desktop changed during capture; observe again")

    async def perform(self, name: str, arguments) -> tuple[list[dict], dict]:
        self.operation_metrics = {}
        await self.validate(name, arguments)
        if name == "computer_observe":
            return await self.observe(arguments), self.operation_metrics
        action = arguments.action
        node = self.refs.get(action.ref)
        point = self.point(action.x, action.y) if action.x is not None else None
        if node and action.type in {"click", "drag", "scroll"}:
            point = self.center(node)
        # The observation is consumed before the first input. A failed acknowledgement cannot replay.
        await self.invalidate()
        self.record("ui_command_attempts")
        self.record("clicks", 0)
        try:
            if action.type == "click":
                count = getattr(action, "click_count", 1)
                await self.x11.click(*point, button=getattr(action, "button", "left"), count=count)
                self.record("clicks", count)
            elif action.type == "focus":
                await self.accessibility.focus(node)
                self.record("native_focus_commands")
            elif action.type == "invoke":
                index = next(item.index for item in node.actions if item.name == action.action_name)
                await self.accessibility.invoke(node, index)
                self.record("native_invocations")
            elif action.type == "fill" and node:
                await self.accessibility.fill(node, action.text)
                self.record("native_text_commands")
            elif action.type in {"fill", "type", "key", "scroll"}:
                if action.type == "scroll":
                    if point:
                        await self.x11.move(*point)
                    await self.x11.scroll(action.delta_x, action.delta_y)
                else:
                    if node:
                        await self.accessibility.focus(node)
                        self.record("native_focus_commands")
                    elif point:
                        await self.x11.click(*point)
                        self.record("clicks")
                    if action.type == "fill":
                        await self.x11.key("Control+A")
                        await self.x11.type(action.text)
                    elif action.type == "type":
                        await self.x11.type(action.text)
                    else:
                        await self.x11.key(action.key)
            elif action.type == "drag":
                path = (
                    {"path": [self.point(item.x, item.y) for item in action.path]}
                    if action.path is not None else {}
                )
                await self.x11.drag(
                    point,
                    self.point(action.end_x, action.end_y),
                    button=getattr(action, "button", "left"),
                    **path,
                )
            self.record("ui_commands")
            content = await self.observe()
            return content, self.operation_metrics
        except Exception as exc:
            await self.invalidate()
            # Both a partial input and an acknowledged input followed by failed capture are uncertain.
            raise WorkerError(
                "action_effect_unknown",
                "Desktop input may have occurred; observe before retrying",
                effect="unknown",
            ) from exc
