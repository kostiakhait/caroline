"""Linux port (2026-10-04): window enumeration/info, the analog of
native_exe.py-backed inspect.exe (window_list/window_children/window_info)
plus the geometry lookup window_mouse/window_keyboard/window_screenshot's
Linux branches need to turn a window handle into screen coordinates.

Deliberately does NOT use _NET_CLIENT_LIST/_NET_CLIENT_LIST_STACKING:
confirmed live (WSL2 + WSLg, 2026-10-04) that its window manager doesn't
set that property at all, so relying on it would return nothing on a
real test environment this project already treats as a first-class
target. Recursive XQueryTree from the root window -- filtering to
windows that carry a WM_NAME or WM_CLASS -- is the WM-independent
fallback every X11 automation tool (xdotool included) falls back to for
exactly this reason, so it's used unconditionally here rather than as a
fallback-only path.

Window handles are returned/accepted as hex strings ("0x600000"), same
convention inspect_plugin.py's Windows hwnd already uses, so chain/
window-* plugins' Linux branches don't need a parallel string format.

Known environment-specific caveat (confirmed live, same session): WSLg's
window manager reports a sentinel (-32768, -32768) geometry for its own
top-level frame windows, which would corrupt a naive "sum ancestor
geometry up to root" absolute-position computation. get_window_rect
detects that sentinel and returns None fields rather than garbage --
untested whether a real (non-WSLg) X.Org/Xwayland desktop ever produces
the same sentinel, but the defensive check costs nothing either way.
"""

from __future__ import annotations

from typing import Any

import Xlib.X
import Xlib.Xatom
import Xlib.display

_SENTINEL = -32768


def _d() -> Xlib.display.Display:
    from app.plugins._x11_input import _d as shared_display  # share the one connection

    return shared_display()


def _parse_hwnd(hwnd: str) -> int:
    return int(hwnd, 16) if hwnd.lower().startswith("0x") else int(hwnd)


def window_for(hwnd: str):
    return _d().create_resource_object("window", _parse_hwnd(hwnd))


def _wm_name(w: Any) -> str | None:
    """Confirmed live (Vultr Ubuntu 24.04 + Xvfb/openbox, 2026-10-05):
    Chromium (and modern apps generally) sets ONLY the EWMH _NET_WM_NAME
    property (UTF8_STRING) and leaves the legacy ICCCM WM_NAME python-
    xlib's own get_wm_name() reads either empty or in a format it can't
    decode -- a window with a real, visible title came back as "" every
    time, which silently broke title-based matching (app_browser's
    is_visible_on_top/real_os_click). _NET_WM_NAME is checked first since
    it's what actually carries the title on a modern window; WM_NAME
    stays as the fallback for older/simpler clients that only set that."""
    try:
        d = _d()
        net_wm_name = d.intern_atom("_NET_WM_NAME")
        utf8_string = d.intern_atom("UTF8_STRING")
        prop = w.get_full_property(net_wm_name, utf8_string)
        if prop and prop.value:
            return bytes(prop.value).decode("utf-8", errors="replace")
    except Exception:
        pass
    try:
        name = w.get_wm_name()
        return name if isinstance(name, str) and name else None
    except Exception:
        return None


def _wm_class(w: Any) -> tuple[str, str] | None:
    try:
        return w.get_wm_class()
    except Exception:
        return None


def _pid(w: Any) -> int | None:
    try:
        d = _d()
        atom = d.intern_atom("_NET_WM_PID")
        prop = w.get_full_property(atom, Xlib.Xatom.CARDINAL)
        return int(prop.value[0]) if prop and prop.value else None
    except Exception:
        return None


def get_window_rect(hwnd_or_window: Any) -> dict[str, int | None]:
    """Best-effort absolute screen rect by summing each ancestor's own
    (parent-relative) geometry up to root. Returns all-None fields if any
    ancestor reports the WSLg sentinel geometry (see module docstring) --
    callers must treat a None rect as "position unknown", not "0,0".

    Retries a few times with a short sleep first: confirmed live (WSL2 +
    WSLg, 2026-10-04) that a window queried immediately after creation can
    transiently report the sentinel for a few hundred ms before the WM
    finishes reparenting/positioning it, then settles to the correct
    value on its own -- same class of race chain.exe's own wait_window
    step exists to guard against, just scoped tighter here since a
    geometry lookup is expected to be fast, not an open-ended wait."""
    import time

    for attempt in range(5):
        rect = _get_window_rect_once(hwnd_or_window)
        if rect["x"] is not None or attempt == 4:
            return rect
        time.sleep(0.15)
    return rect  # pragma: no cover -- loop above always returns


def _get_window_rect_once(hwnd_or_window: Any) -> dict[str, int | None]:
    w = window_for(hwnd_or_window) if isinstance(hwnd_or_window, str) else hwnd_or_window
    root = _d().screen().root
    x = y = 0
    cur = w
    try:
        own_geom = cur.get_geometry()
        width, height = own_geom.width, own_geom.height
    except Exception:
        return {"x": None, "y": None, "width": None, "height": None}
    while True:
        try:
            geom = cur.get_geometry()
        except Exception:
            return {"x": None, "y": None, "width": None, "height": None}
        if geom.x <= _SENTINEL or geom.y <= _SENTINEL:
            return {"x": None, "y": None, "width": None, "height": None}
        x += geom.x + geom.border_width
        y += geom.y + geom.border_width
        try:
            parent = cur.query_tree().parent
        except Exception:
            return {"x": None, "y": None, "width": None, "height": None}
        if parent is None or parent.id == root.id:
            break
        cur = parent
    return {"x": x, "y": y, "width": width, "height": height}


def _window_record(w: Any) -> dict[str, Any] | None:
    name = _wm_name(w)
    cls = _wm_class(w)
    if name is None and cls is None:
        return None  # not a real client window -- a WM frame/container/etc.
    try:
        attrs = w.get_attributes()
        visible = attrs.map_state == Xlib.X.IsViewable
    except Exception:
        visible = None
    rect = get_window_rect(w)
    return {
        "hwnd": f"0x{w.id:X}",
        "title": name or "",
        "className": cls[1] if cls else "",
        "pid": _pid(w),
        "visible": visible,
        "x": rect["x"], "y": rect["y"], "width": rect["width"], "height": rect["height"],
    }


def _walk_client_windows(w: Any, depth: int, max_depth: int, out: list[Any]) -> None:
    if depth > max_depth:
        return
    try:
        children = w.query_tree().children
    except Exception:
        return
    for child in children:
        name = _wm_name(child)
        cls = _wm_class(child)
        if name is not None or cls is not None:
            out.append(child)
        _walk_client_windows(child, depth + 1, max_depth, out)


def list_windows(
    title_filter: str | None = None,
    class_name_filter: str | None = None,
    pid_filter: int | None = None,
    include_invisible: bool = False,
    max_depth: int = 6,
) -> list[dict[str, Any]]:
    root = _d().screen().root
    candidates: list[Any] = []
    _walk_client_windows(root, 0, max_depth, candidates)
    results: list[dict[str, Any]] = []
    for w in candidates:
        rec = _window_record(w)
        if rec is None:
            continue
        if not include_invisible and rec["visible"] is False:
            continue
        if title_filter and title_filter.lower() not in rec["title"].lower():
            continue
        if class_name_filter and class_name_filter.lower() not in rec["className"].lower():
            continue
        if pid_filter is not None and rec["pid"] != pid_filter:
            continue
        results.append(rec)
    return results


def window_children(
    hwnd: str,
    title_filter: str | None = None,
    class_name_filter: str | None = None,
    pid_filter: int | None = None,
    include_invisible: bool = False,
) -> list[dict[str, Any]]:
    w = window_for(hwnd)
    try:
        direct_children = w.query_tree().children
    except Exception:
        return []
    results: list[dict[str, Any]] = []
    for child in direct_children:
        rec = _window_record(child)
        if rec is None:
            rec = {
                "hwnd": f"0x{child.id:X}", "title": "", "className": "",
                "pid": _pid(child), "visible": None, **get_window_rect(child),
            }
        if not include_invisible and rec.get("visible") is False:
            continue
        if title_filter and title_filter.lower() not in (rec["title"] or "").lower():
            continue
        if class_name_filter and class_name_filter.lower() not in (rec["className"] or "").lower():
            continue
        if pid_filter is not None and rec["pid"] != pid_filter:
            continue
        results.append(rec)
    return results


def window_info(hwnd: str) -> dict[str, Any]:
    w = window_for(hwnd)
    rec = _window_record(w)
    if rec is None:
        rec = {
            "hwnd": f"0x{w.id:X}", "title": "", "className": "",
            "pid": _pid(w), "visible": None, **get_window_rect(w),
        }
    return rec
