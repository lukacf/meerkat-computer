"""Focused-window visual fallback for native dialogs omitted from AT-SPI."""

from dataclasses import dataclass, replace

import pytest
from meerkat_computer.protocol import WorkerError
from test_desktop import WINDOW, act, observe
from test_desktop import desktop as shared_desktop
from test_desktop_freshness import background_clock, event

desktop = shared_desktop


@dataclass(frozen=True)
class VisualAnchor:
    visible_bounds: tuple[int, int, int, int] = (10, 10, 620, 470)
    identity: int = 123
    pixels: str = "native-dialog"
    topology: tuple[int, ...] = (1, 123)

    def fence(self):
        from meerkat_computer.desktop_x11 import (
            DisplayState,
            FocusedTopology,
            FocusedVisualFence,
        )

        return FocusedVisualFence(
            FocusedTopology(
                DisplayState(640, 480, str(self.identity)),
                tuple(("window", 0, item) for item in self.topology),
                (),
                self.visible_bounds,
                self.visible_bounds,
            ),
            self.pixels,
        )


async def setup_dialog(desktop):
    background_clock(desktop)
    desktop.accessibility.nodes[WINDOW.key] = replace(WINDOW, states=("showing",))
    desktop.x11.anchor = VisualAnchor()

    async def capture_with_fence():
        anchor = desktop.x11.anchor
        return await desktop.x11.screenshot(), anchor.fence() if anchor else None

    desktop.x11.capture_with_fence = capture_with_fence
    return (await observe(desktop))[0]


@pytest.mark.parametrize(
    "action",
    [
        {"type": "key", "key": "Control+l"},
        {"type": "fill", "text": "/home/agent/upload.pdf"},
        {"type": "click", "x": 200, "y": 100},
    ],
)
async def test_unrepresented_dialog_uses_exact_visual_anchor_despite_background_clock(
    desktop, action
):
    body = await setup_dialog(desktop)
    event(desktop.accessibility, member="ChildrenChanged")
    _, metrics = await desktop.perform("computer_act", act(body, **action))
    assert metrics["ui_commands"] == 1 and desktop.x11.inputs


@pytest.mark.parametrize(
    "change",
    [
        {"pixels": "changed-dialog"},
        {"identity": 124},
        {"visible_bounds": (20, 10, 610, 470)},
        {"topology": (1, 123, 456)},
    ],
)
async def test_changed_native_visual_anchor_rejects_without_input(desktop, change):
    body = await setup_dialog(desktop)
    desktop.x11.anchor = replace(desktop.x11.anchor, **change)
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="key", key="Control+l"))
    assert error.value.effect == "none" and not desktop.x11.inputs


async def test_outside_visible_client_does_not_use_visual_anchor(desktop):
    body = await setup_dialog(desktop)
    event(desktop.accessibility, member="ChildrenChanged")
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="click", x=0, y=0))
    assert error.value.effect == "none" and not desktop.x11.inputs


async def test_unknown_current_visual_anchor_rejects_without_input(desktop):
    body = await setup_dialog(desktop)
    desktop.x11.anchor = None
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="key", key="Control+l"))
    assert error.value.effect == "none" and not desktop.x11.inputs


async def test_structural_change_during_visual_validation_rejects_input(desktop):
    body = await setup_dialog(desktop)
    original = desktop.x11.capture_with_fence

    async def changed_capture():
        result = await original()
        event(desktop.accessibility, path="/unknown", member="ChildrenChanged")
        return result

    desktop.x11.capture_with_fence = changed_capture
    with pytest.raises(WorkerError) as error:
        await desktop.perform("computer_act", act(body, type="key", key="Control+l"))
    assert error.value.effect == "none" and not desktop.x11.inputs


def tree_output(extra=""):
    return (
        "xwininfo: Window id: 0x1 (the root window) (has no name)\n"
        "  Root window id: 0x1 (the root window) (has no name)\n"
        "  Parent window id: 0x0 (none)\n"
        "     2 children:\n"
        '     0x7b "PRIVATE TITLE": ("chrome" "Chrome")  620x500+10+10  +10+10\n'
        "        1 child:\n"
        "        0x7c (has no name): ()  1x1+0+0  +10+10\n"
        "     0x7d (has no name): ()  640x480+0+0  +0+0\n" + extra
    )


def stats(
    window, *, bounds=(0, 0, 640, 480), kind="InputOutput", state="IsViewable", override="no"
):
    x, y, width, height = bounds
    return f"""xwininfo: Window id: {hex(window)} "PRIVATE TITLE"
  Absolute upper-left X: {x}
  Absolute upper-left Y: {y}
  Width: {width}
  Height: {height}
  Border width: 0
  Class: {kind}
  Map State: {state}
  Override Redirect State: {override}
"""


def test_tree_retains_order_child_counts_and_excludes_private_names():
    from meerkat_computer.desktop_x11 import X11

    root, tree = X11.parse_tree(tree_output())
    assert root == 1
    assert [value for kind, _, value in tree if kind == "window"] == [123, 124, 125]
    assert "PRIVATE" not in repr(tree)


@pytest.mark.parametrize(
    "output",
    [
        tree_output().replace("2 children", "3 children"),
        tree_output().replace("0x7d (has", "0x7c (has"),
        tree_output().replace("Root window id", "Unknown root"),
        tree_output().replace("1 child:", "unknown children:"),
    ],
)
def test_incomplete_or_duplicate_tree_is_rejected(output):
    from meerkat_computer.desktop_x11 import X11

    with pytest.raises(ValueError):
        X11.parse_tree(output)


async def test_topology_includes_clipped_dialog_and_unmapped_input_only_child():
    from meerkat_computer.desktop_x11 import X11

    driver = X11(":99")
    driver.xprop, driver.xwininfo = "xprop", "xwininfo"
    outputs = {
        ("getdisplaygeometry",): "640 480\n",
        ("getwindowfocus",): "123\n",
        ("-root", "_NET_ACTIVE_WINDOW"): "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x7b\n",
        ("-root", "-tree"): tree_output(),
        ("-id", "0x1", "-stats"): stats(1),
        ("-id", "0x7b", "-stats"): stats(123, bounds=(10, 10, 620, 500)),
        ("-id", "0x7c", "-stats"): stats(124, kind="InputOnly", state="IsUnMapped", override="yes"),
        ("-id", "0x7d", "-stats"): stats(125),
    }

    async def command(*arguments, **kwargs):
        return outputs[arguments]

    driver.command = command
    topology = await driver.focused_topology()
    assert topology.client_bounds == (10, 10, 620, 500)
    assert topology.visible_bounds == (10, 10, 620, 470)
    assert topology.windows[2].window_class == "InputOnly"
    assert topology.windows[2].map_state == "IsUnMapped"
    assert topology.windows[2].override_redirect is True
    assert "PRIVATE" not in repr(topology)


async def test_visual_fence_brackets_same_rgb_capture_and_rejects_changed_topology():
    from meerkat_computer.desktop_x11 import X11, FocusedVisualFence

    driver = X11(":99")
    order = []
    before = VisualAnchor()
    topologies = [before, before]

    async def topology():
        order.append("topology")
        return topologies.pop(0)

    async def capture(bounds):
        order.append(("capture", bounds))
        return b"exact-model-png", "same-frame-rgb"

    driver.focused_topology, driver.capture = topology, capture
    png, fence = await driver.capture_with_fence()
    assert png == b"exact-model-png"
    assert fence == FocusedVisualFence(before, "same-frame-rgb")
    assert order == ["topology", ("capture", before.visible_bounds), "topology"]
    topologies.extend([before, replace(before, topology=(1, 123, 456))])
    assert (await driver.capture_with_fence())[1] is None


async def test_visual_topology_deadline_cancels_read_and_falls_back(monkeypatch):
    import asyncio

    import meerkat_computer.desktop_x11 as module

    driver = module.X11(":99")
    cancelled = []

    async def slow_topology():
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    async def screenshot():
        return b"ordinary-screenshot"

    monkeypatch.setattr(module, "MAX_VISUAL_FENCE_SECONDS", 0.01)
    driver.focused_topology, driver.screenshot = slow_topology, screenshot
    assert await driver.capture_with_fence() == (b"ordinary-screenshot", None)
    assert cancelled == [True]


async def test_visual_digest_uses_visible_rgb_rows_from_returned_screenshot(monkeypatch):
    import hashlib
    from types import SimpleNamespace

    import mss
    import mss.tools
    from meerkat_computer.desktop_x11 import X11

    rgb = bytes(range(36))
    calls = []

    class Screen:
        def __init__(self):
            self.monitors = [{"left": 0, "top": 0, "width": 4, "height": 3}]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def grab(self, monitor):
            calls.append(monitor)
            return SimpleNamespace(rgb=rgb, size=(4, 3), width=4, height=3)

    monkeypatch.setattr(mss, "mss", lambda **kwargs: Screen())
    monkeypatch.setattr(mss.tools, "to_png", lambda value, size: b"PNG" + value)
    png, digest = await X11(":99").capture((1, 1, 2, 2))
    assert png == b"PNG" + rgb
    assert digest == hashlib.sha256(rgb[15:21] + rgb[27:33]).hexdigest()
    assert len(calls) == 1


def test_complete_real_two_client_openbox_tree_fits_bounded_topology():
    from pathlib import Path

    from meerkat_computer.desktop_x11 import X11

    fixture = Path(__file__).parents[1] / "fixtures" / "xwininfo-openbox-tree.txt"
    root, tree = X11.parse_tree(fixture.read_text())
    assert root > 0
    assert sum(kind == "window" for kind, _, _ in tree) == 180
    assert sum(value for kind, _, value in tree if kind == "children") == 180


@pytest.mark.parametrize("change", ["input_only_child", "active_window"])
async def test_topology_change_during_final_stats_batch_cannot_authorize_input(change):
    from meerkat_computer.desktop_x11 import X11

    driver = X11(":99")
    driver.xprop, driver.xwininfo = "xprop", "xwininfo"
    read_stats = False

    async def command(*arguments, **kwargs):
        nonlocal read_stats
        if arguments == ("getdisplaygeometry",):
            return "640 480\n"
        if arguments == ("getwindowfocus",):
            return "123\n"
        if arguments == ("-root", "_NET_ACTIVE_WINDOW"):
            active = "0x7d" if read_stats and change == "active_window" else "0x7b"
            return f"_NET_ACTIVE_WINDOW(WINDOW): window id # {active}\n"
        if arguments == ("-root", "-tree"):
            if read_stats and change == "input_only_child":
                return tree_output("     0x7e (has no name): ()  640x480+0+0  +0+0\n").replace(
                    "2 children:", "3 children:"
                )
            return tree_output()
        if arguments[0] == "-id":
            read_stats = True
            return stats(int(arguments[1], 16))
        raise AssertionError("Unexpected query")

    driver.command = command
    assert await driver.focused_topology() is None


@pytest.mark.parametrize("payload_kind", ["real_tree", "limit", "overflow"])
async def test_x11_query_drains_chunked_stdout_with_cumulative_limit(payload_kind):
    import sys
    from pathlib import Path

    from meerkat_computer.desktop_x11 import X11

    driver = X11(":99")
    driver.executable = sys.executable
    if payload_kind == "real_tree":
        fixture = Path(__file__).parents[1] / "fixtures" / "xwininfo-openbox-tree.txt"
        expected = fixture.read_text()
    else:
        expected = "x" * (65_537 if payload_kind == "overflow" else 65_536)
    writer = """import sys, time
value = sys.stdin.read()
sys.stdout.write(value[:64])
sys.stdout.flush()
time.sleep(0.03)
sys.stdout.write(value[64:])
sys.stdout.flush()
"""
    if payload_kind == "overflow":
        with pytest.raises(WorkerError, match="exceeded limit"):
            await driver.command("-c", writer, input_text=expected)
    else:
        output = await driver.command("-c", writer, input_text=expected)
        assert output == expected
        if payload_kind == "real_tree":
            assert len(X11.parse_tree(output)[1]) > 180
