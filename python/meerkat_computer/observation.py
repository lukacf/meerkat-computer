"""Bounded presentation of application semantics, independent of action authority."""

from dataclasses import dataclass

from .desktop_atspi import REGISTRY, WINDOW_ROLES, Tree, object_id
from .protocol import Observation, ObserveArguments, WorkerError


@dataclass(frozen=True)
class Projection:
    detail: str
    max_elements: int
    max_text_chars: int

    @classmethod
    def resolve(cls, profile: Observation, arguments: ObserveArguments | None = None):
        arguments = arguments or ObserveArguments()
        detail = arguments.detail or profile.detail
        values = {}
        for name, compact in (("max_elements", 60), ("max_text_chars", 6000)):
            ceiling = getattr(profile, name)
            requested = getattr(arguments, name)
            if requested is not None and requested > ceiling:
                raise WorkerError(
                    "observation_limit_exceeded", "Observation exceeds the profile ceiling", 422
                )
            values[name] = requested or (min(compact, ceiling) if detail == "compact" else ceiling)
        return cls(detail, **values)

    def metadata(self, inspected: int, returned: int, budget, *, omitted: int | None = None):
        omitted = inspected - returned if omitted is None else omitted
        return {
            "detail": self.detail,
            "max_elements": self.max_elements,
            "max_text_chars": self.max_text_chars,
            "inspected_elements": inspected,
            "returned_elements": returned,
            "omitted_elements": omitted,
            "text_chars": budget.used,
            "truncated_fields": budget.truncated_fields,
            "truncated": bool(omitted or budget.truncated_fields),
        }


class TextBudget:
    def __init__(self, limit):
        self.limit = limit
        self.used = 0
        self.truncated_fields = 0

    def take(self, text, *, limit=None, exact=False):
        available = self.limit - self.used
        if limit is not None:
            available = min(available, limit)
        value = "" if exact and len(text) > available else text[:available]
        self.used += len(value)
        if value != text:
            self.truncated_fields += 1
        return value


def desktop_projection(tree: Tree, projection: Projection, *, scope_key=None):
    """Select presentation only. The caller retains the entire private fencing tree."""
    rows = tree.nodes
    key_refs = {node.key: f"e{i}" for i, (node, _, _) in enumerate(rows, 1)}
    parents = {node.key: parent for node, parent, _ in rows}
    windows = {window: node for node, _, window in rows if node.role_id in WINDOW_ROLES}
    if scope_key is not None:
        descendants = {scope_key}
        for _ in range(len(rows)):
            added = {key for key, parent in parents.items() if parent in descendants}
            if added <= descendants:
                break
            descendants.update(added)
        rows = [row for row in rows if row[0].key in descendants]
    inspected = len(rows)
    if projection.detail == "compact":

        def priority(row):
            node, _, window = row
            active = window in windows and "active" in windows[window].states
            if node.key == scope_key or node.role_id in WINDOW_ROLES | {75}:
                return 0
            if "focused" in node.states:
                return 1
            if active and (node.actions or "editable" in node.states):
                return 2
            return 3 if active else 4

        rows = sorted(rows, key=priority)
        rows = [
            row
            for row in rows
            if (
                row[0].key == scope_key
                or row[0].role_id in WINDOW_ROLES | {75}
                or (
                    ("showing" in row[0].states or "focused" in row[0].states)
                    and (
                        row[0].name or row[0].text or row[0].actions or "focusable" in row[0].states
                    )
                )
            )
        ]
    selected = rows[: projection.max_elements]
    selected_keys = {node.key for node, _, _ in selected}
    budget = TextBudget(projection.max_text_chars)
    compact = projection.detail == "compact"
    # Reserve a useful name and exact action names before large text values.
    name_limit = min(160 if compact else 300, max(16, budget.limit // max(len(selected), 1) // 2))
    elements, applications, window_list, refs, ref_windows = [], [], [], {}, {}
    selected_actions = {}
    for node, parent, window in selected:
        ref = key_refs[node.key]
        element = {
            "ref": ref,
            "role": budget.take(node.role, limit=100),
            "name": budget.take(node.name, limit=name_limit),
            "states": list(node.states),
            "actions": [],
        }
        if element["name"] != node.name:
            element["name_truncated"] = True
        if element["role"] != node.role:
            element["role_truncated"] = True
        selected_actions[ref] = []
        for action in node.actions:
            name = budget.take(action.name, exact=True)
            if name:
                element["actions"].append({"name": name})
                selected_actions[ref].append(action)
        if len(element["actions"]) != len(node.actions):
            element["actions_truncated"] = True
        ancestor = parent
        seen = set()
        while ancestor is not None and ancestor not in selected_keys and ancestor not in seen:
            seen.add(ancestor)
            ancestor = parents.get(ancestor)
        if ancestor in selected_keys:
            element["parent_ref"] = key_refs[ancestor]
        if node.child_count:
            element["child_count"] = node.child_count
        if node.bounds:
            element["bounds"] = dict(zip(("x", "y", "width", "height"), node.bounds))
        if node.issues:
            element["issues"] = list(node.issues)
        app_id = object_id("app_", (node.key[0], "")) if node.key[0] != REGISTRY else None
        element.update(app_id=app_id, window_id=window)
        if compact:
            if window in windows and windows[window].key in selected_keys:
                element["window_ref"] = key_refs[windows[window].key]
        else:
            element["role_id"] = node.role_id
        if node.role_id == 75:
            applications.append({"app_id": app_id, "ref": ref})
        if node.role_id in WINDOW_ROLES:
            window_list.append({"window_id": window, "app_id": app_id, "ref": ref})
        refs[ref] = node
        if window in windows:
            ref_windows[ref] = windows[window]
        elements.append(element)
    # Keep application/window names usable before large document text consumes
    # the remaining budget. Summary duplicates are counted too.
    by_ref = {element["ref"]: element for element in elements}
    for item in applications + window_list:
        name = by_ref[item["ref"]]["name"]
        item["name"] = budget.take(name)
        if item["name"] != name or by_ref[item["ref"]].get("name_truncated"):
            item["name_truncated"] = True
    for element, (node, _, _) in zip(elements, selected):
        if node.text is not None:
            # Native controls often expose the same string twice.
            if node.text == element["name"] and not node.text_truncated and compact:
                element["text_same_as_name"] = True
            else:
                text = budget.take(node.text, limit=512 if compact else None)
                element.update(text=text, text_truncated=node.text_truncated or text != node.text)
        if not compact:
            for field in ("description", "accessible_id"):
                value = getattr(node, field)
                if value:
                    element[field] = budget.take(value)
                    if element[field] != value:
                        element[field + "_truncated"] = True
            for item, action in zip(element["actions"], selected_actions[element["ref"]]):
                for field in ("description", "key_binding"):
                    item[field] = budget.take(getattr(action, field))
                    if item[field] != getattr(action, field):
                        item[field + "_truncated"] = True
    metadata = projection.metadata(inspected, len(elements), budget)
    if scope_key is not None:
        metadata["scope_ref"] = key_refs.get(scope_key)
    return elements, applications, window_list, refs, ref_windows, metadata
