"""Bounded numeric X11 snapshots for visual desktop input."""

from copy import deepcopy

import pytest
from meerkat_computer.desktop_topology import collect
from meerkat_computer.desktop_x11 import X11


class Reader:
    root = 80

    def __init__(self, focus=20):
        self.focus = focus
        self.trees = {80: (0, [20, 30]), 20: (80, []), 30: (80, [])}
        self.windows = {
            80: ([0, 0, 1280, 960], 0, 1, 2, 0),
            20: ([10, 20, 640, 480], 1, 1, 2, 0),
            30: ([200, 100, 40, 60], 0, 2, 2, 1),
        }
        self.calls = 0

    def tree(self, window):
        self.calls += 1
        return deepcopy(self.trees[window])

    def geometry(self, window):
        return deepcopy(self.windows[window])

    def input_focus(self):
        return self.focus

    def finish(self):
        pass


def test_numeric_snapshot_includes_root_input_only_and_native_order():
    value = collect(Reader())
    assert value == {
        "version": 1,
        "root": 80,
        "focus": 20,
        "size": [1280, 960],
        "nodes": [
            [80, 0, [20, 30], [0, 0, 1280, 960], 0, 1, 2, 0],
            [20, 80, [], [10, 20, 640, 480], 1, 1, 2, 0],
            [30, 80, [], [200, 100, 40, 60], 0, 2, 2, 1],
        ],
    }


@pytest.mark.parametrize("focus", [0, 1, 80])
def test_numeric_snapshot_empty_root(focus):
    reader = Reader(focus)
    reader.trees = {80: (0, [])}
    assert collect(reader)["nodes"] == [[80, 0, [], [0, 0, 1280, 960], 0, 1, 2, 0]]


@pytest.mark.parametrize("change", ["reorder", "focus", "size", "error"])
def test_snapshot_rejects_races_and_read_errors(change):
    reader = Reader()
    original = reader.tree

    def tree(window):
        if reader.calls >= 3:
            if change == "reorder":
                reader.trees[80] = (0, [30, 20])
            elif change == "focus":
                reader.focus = 30
            elif change == "size":
                reader.windows[80] = ([0, 0, 1920, 1080], 0, 1, 2, 0)
            elif change == "error":
                raise ValueError("destroyed window")
        return original(window)

    reader.tree = tree
    with pytest.raises(ValueError):
        collect(reader)


@pytest.mark.parametrize("change", ["cycle", "parent", "duplicate", "oversize", "focus"])
def test_snapshot_rejects_invalid_or_oversize_native_tree(change):
    reader = Reader()
    if change == "cycle":
        reader.trees[20] = (80, [80])
    elif change == "parent":
        reader.trees[20] = (30, [])
    elif change == "duplicate":
        reader.trees[80] = (0, [20, 20])
    elif change == "oversize":
        reader.trees[80] = (0, list(range(1000, 1512)))
    else:
        reader.focus = 999
    with pytest.raises(ValueError):
        collect(reader)


def test_helper_output_decodes_complete_numeric_context():
    topology = X11.parse_desktop_topology(collect(Reader()))
    assert topology.screen.focus == "20" and topology.root == 80
    assert topology.windows[2].window_class == "InputOnly"
    assert topology.windows[2].override_redirect
    assert topology.windows[1].bounds == (10, 20, 640, 480)


@pytest.mark.parametrize("change", ["bool", "missing", "reparent", "duplicate", "focus", "extra"])
def test_helper_output_is_validated_before_it_can_authorize_input(change):
    value = collect(Reader())
    if change == "bool":
        value["nodes"][1][3][0] = True
    elif change == "missing":
        value["nodes"].pop()
    elif change == "reparent":
        value["nodes"][1][1] = 30
    elif change == "duplicate":
        value["nodes"].append(value["nodes"][1])
    elif change == "focus":
        value["focus"] = 999
    else:
        value["secret"] = "unexpected"
    with pytest.raises(ValueError):
        X11.parse_desktop_topology(value)


@pytest.mark.parametrize("focus", [0, 1, 80])
async def test_state_recovers_only_native_root_focus_when_client_lookup_fails(focus):
    from unittest.mock import AsyncMock

    from meerkat_computer.protocol import WorkerError

    reader = Reader(focus)
    reader.trees = {80: (0, [])}
    x11 = X11(":99")
    x11.command = AsyncMock(side_effect=["1280 960\n", WorkerError("desktop_io_failed", "no client")])
    x11.desktop_topology = AsyncMock(return_value=X11.parse_desktop_topology(collect(reader)))
    state = await x11.state()
    assert (state.width, state.height, state.focus) == (1280, 960, str(focus))
    x11.desktop_topology.assert_awaited_once()


@pytest.mark.parametrize("change", ["nonroot", "unavailable", "size"])
async def test_state_keeps_nonroot_or_unverified_focus_failure_closed(change):
    from unittest.mock import AsyncMock

    from meerkat_computer.protocol import WorkerError

    value = collect(Reader(1 if change == "size" else 20))
    if change == "size":
        value["size"] = [1920, 1080]
        value["nodes"][0][3] = [0, 0, 1920, 1080]
    topology = None if change == "unavailable" else X11.parse_desktop_topology(value)
    error = WorkerError("desktop_io_failed", "lookup failed")
    x11 = X11(":99")
    x11.command = AsyncMock(side_effect=["1280 960\n", error])
    x11.desktop_topology = AsyncMock(return_value=topology)
    with pytest.raises(WorkerError) as raised:
        await x11.state()
    assert raised.value is error


async def test_state_preserves_normal_focused_client_lookup():
    from unittest.mock import AsyncMock

    x11 = X11(":99")
    x11.command = AsyncMock(side_effect=["1280 960\n", "123\n"])
    x11.desktop_topology = AsyncMock()
    assert (await x11.state()).focus == "123"
    x11.desktop_topology.assert_not_awaited()
