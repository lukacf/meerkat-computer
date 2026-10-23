"""Numeric X11 metadata in a short-lived, externally bounded helper process.

No window titles, properties, pixels or accessibility text are read. Keeping
libX11 in a separate process lets the caller cancel a stuck display connection.
"""

import ctypes as C
import json
import sys

MAX_WINDOWS = 512


def integer(value, minimum=0, maximum=2**32 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid numeric X11 metadata")
    return value


def validate_snapshot(value):
    if not isinstance(value, dict) or set(value) != {"version", "root", "focus", "size", "nodes"}:
        raise ValueError("Invalid desktop snapshot")
    if integer(value["version"]) != 1:
        raise ValueError("Unsupported desktop snapshot")
    root, focus = integer(value["root"], 2), integer(value["focus"])
    size, nodes = value["size"], value["nodes"]
    if not isinstance(size, list) or len(size) != 2:
        raise ValueError("Invalid display size")
    for number in size:
        integer(number, 1, 2**31 - 1)
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_WINDOWS:
        raise ValueError("Invalid window count")
    indexed = {}
    for row in nodes:
        if not isinstance(row, list) or len(row) != 8:
            raise ValueError("Invalid window metadata")
        window, parent, children, bounds, border, kind, mapped, override = row
        integer(window, 2)
        integer(parent)
        if window in indexed or not isinstance(children, list) or len(children) >= MAX_WINDOWS:
            raise ValueError("Invalid window tree")
        for child in children:
            integer(child, 2)
        if len(set(children)) != len(children):
            raise ValueError("Duplicate window")
        if not isinstance(bounds, list) or len(bounds) != 4:
            raise ValueError("Invalid window bounds")
        for coordinate in bounds[:2]:
            integer(coordinate, -(2**31), 2**31 - 1)
        for dimension in bounds[2:]:
            integer(dimension, 1, 2**31 - 1)
        integer(border, 0, 2**31 - 1)
        integer(kind, 1, 2)
        integer(mapped, 0, 2)
        integer(override, 0, 1)
        indexed[window] = row
    if nodes[0][0] != root or nodes[0][1] != 0 or nodes[0][3] != [0, 0, *size]:
        raise ValueError("Invalid root window")
    seen, order, pending = set(), [], [(root, 0)]
    while pending:
        window, parent = pending.pop()
        if window in seen or window not in indexed or indexed[window][1] != parent:
            raise ValueError("Inconsistent window tree")
        seen.add(window)
        order.append(window)
        pending.extend((child, window) for child in reversed(indexed[window][2]))
    if order != [row[0] for row in nodes] or focus not in {0, 1, *seen}:
        raise ValueError("Incomplete window context")
    return value


def collect(reader):
    root, focus = reader.root, reader.input_focus()
    nodes, seen, pending = [], set(), [(root, 0)]
    while pending:
        window, expected_parent = pending.pop()
        if window in seen or len(seen) >= MAX_WINDOWS:
            raise ValueError("Window tree exceeds bounds or contains a cycle")
        seen.add(window)
        parent, children = reader.tree(window)
        if parent != expected_parent or len(seen) + len(pending) + len(children) > MAX_WINDOWS:
            raise ValueError("Window tree is inconsistent or too large")
        bounds, border, kind, mapped, override = reader.geometry(window)
        nodes.append([window, parent, list(children), list(bounds), border, kind, mapped, override])
        pending.extend((child, window) for child in reversed(children))
    # Recheck every relationship and stacking order after reading geometry.
    # Two complete snapshots at the call site additionally fence geometry races.
    for window, parent, children, *_ in nodes:
        if reader.tree(window) != (parent, children):
            raise ValueError("Window tree changed during capture")
    if reader.input_focus() != focus or list(reader.geometry(root)[0]) != nodes[0][3]:
        raise ValueError("Display or focus changed during capture")
    reader.finish()
    return validate_snapshot(
        {"version": 1, "root": root, "focus": focus, "size": nodes[0][3][2:], "nodes": nodes}
    )


class Attributes(C.Structure):
    _fields_ = [
        ("x", C.c_int),
        ("y", C.c_int),
        ("width", C.c_int),
        ("height", C.c_int),
        ("border_width", C.c_int),
        ("depth", C.c_int),
        ("visual", C.c_void_p),
        ("root", C.c_ulong),
        ("window_class", C.c_int),
        ("bit_gravity", C.c_int),
        ("win_gravity", C.c_int),
        ("backing_store", C.c_int),
        ("backing_planes", C.c_ulong),
        ("backing_pixel", C.c_ulong),
        ("save_under", C.c_int),
        ("colormap", C.c_ulong),
        ("map_installed", C.c_int),
        ("map_state", C.c_int),
        ("all_event_masks", C.c_long),
        ("your_event_mask", C.c_long),
        ("do_not_propagate_mask", C.c_long),
        ("override_redirect", C.c_int),
        ("screen", C.c_void_p),
    ]


class NativeReader:
    def __init__(self):
        # The Linux desktop already depends on libX11 through xdotool/Xvfb.
        self.lib = C.CDLL("libX11.so.6")
        display, window = C.c_void_p, C.c_ulong
        pointer = C.POINTER
        signatures = {
            "XOpenDisplay": ([C.c_char_p], display),
            "XDefaultRootWindow": ([display], window),
            "XGetInputFocus": ([display, pointer(window), pointer(C.c_int)], C.c_int),
            "XQueryTree": (
                [
                    display,
                    window,
                    pointer(window),
                    pointer(window),
                    pointer(pointer(window)),
                    pointer(C.c_uint),
                ],
                C.c_int,
            ),
            "XGetWindowAttributes": ([display, window, pointer(Attributes)], C.c_int),
            "XTranslateCoordinates": (
                [
                    display,
                    window,
                    window,
                    C.c_int,
                    C.c_int,
                    pointer(C.c_int),
                    pointer(C.c_int),
                    pointer(window),
                ],
                C.c_int,
            ),
            "XFree": ([C.c_void_p], C.c_int),
            "XSync": ([display, C.c_int], C.c_int),
            "XCloseDisplay": ([display], C.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = arguments, result
        self.error = False
        callback = C.CFUNCTYPE(C.c_int, display, C.c_void_p)

        def failed(_display, _event):
            self.error = True
            return 0

        self.error_handler = callback(failed)
        self.lib.XSetErrorHandler.argtypes = [callback]
        self.lib.XSetErrorHandler.restype = C.c_void_p
        self.lib.XSetErrorHandler(self.error_handler)
        self.display = self.lib.XOpenDisplay(None)
        if not self.display:
            raise ValueError("Display unavailable")
        self.root = self.lib.XDefaultRootWindow(self.display)

    def input_focus(self):
        focus, revert = C.c_ulong(), C.c_int()
        self.lib.XGetInputFocus(self.display, C.byref(focus), C.byref(revert))
        return focus.value

    def tree(self, window):
        root, parent, count = C.c_ulong(), C.c_ulong(), C.c_uint()
        children = C.POINTER(C.c_ulong)()
        try:
            ok = self.lib.XQueryTree(
                self.display,
                window,
                C.byref(root),
                C.byref(parent),
                C.byref(children),
                C.byref(count),
            )
            if not ok or root.value != self.root or count.value >= MAX_WINDOWS:
                raise ValueError("Window tree unavailable or oversized")
            return parent.value, [children[index] for index in range(count.value)]
        finally:
            if children:
                self.lib.XFree(children)

    def geometry(self, window):
        attrs, x, y, child = Attributes(), C.c_int(), C.c_int(), C.c_ulong()
        if not self.lib.XGetWindowAttributes(self.display, window, C.byref(attrs)):
            raise ValueError("Window attributes unavailable")
        if attrs.root != self.root or not self.lib.XTranslateCoordinates(
            self.display, window, self.root, 0, 0, C.byref(x), C.byref(y), C.byref(child)
        ):
            raise ValueError("Window bounds unavailable")
        return (
            [x.value, y.value, attrs.width, attrs.height],
            attrs.border_width,
            attrs.window_class,
            attrs.map_state,
            attrs.override_redirect,
        )

    def finish(self):
        self.lib.XSync(self.display, False)
        if self.error:
            raise ValueError("X11 read failed")

    def close(self):
        self.lib.XCloseDisplay(self.display)


def main():
    reader = None
    try:
        reader = NativeReader()
        value = collect(reader)
        print(json.dumps(value, separators=(",", ":")))
        return 0
    except (OSError, ValueError):
        return 1
    finally:
        if reader is not None:
            reader.close()


if __name__ == "__main__":
    sys.exit(main())
