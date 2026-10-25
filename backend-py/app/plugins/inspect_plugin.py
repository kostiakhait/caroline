"""windows-inspect -- window enumeration (EnumWindows/EnumChildWindows).
Windows: in-process via app.plugins._win32 (2026-10-24; was inspect.exe),
same records and JSON.

Linux port (2026-10-04): in-process recursive XQueryTree via
app.plugins._x11_window instead of a native exe -- see that module's own
docstring for why it doesn't rely on _NET_CLIENT_LIST (confirmed live:
not supported by the WSLg window manager used to test this)."""

from __future__ import annotations

import json
import sys
from typing import Any

from app.plugins.loader import Plugin, PluginTool

import asyncio

if sys.platform == "win32":
    from app.context_cap import truncate
    from app.plugins import _win32 as win
else:
    from app.plugins import _x11_window as x11win


def _win_json(value: Any) -> str:
    """The records as JSON, capped like every tool result (a busy desktop's
    whole window tree has no natural size ceiling)."""
    text, note = truncate(json.dumps(value))
    if note is None:
        return text
    return text + "\n\n[... output truncated: too large to return in full; narrow what you're asking for (a specific window/control instead of the whole desktop) ...]"


async def window_list(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    if sys.platform != "win32":
        records = await asyncio.to_thread(
            x11win.list_windows,
            args.get("titleFilter"), args.get("classNameFilter"), args.get("pid"),
            bool(args.get("includeInvisible")),
        )
        return {"text": json.dumps(records)}
    records = await asyncio.to_thread(
        win.list_windows, args.get("titleFilter"), args.get("classNameFilter"), args.get("pid"),
        bool(args.get("includeInvisible")),
    )
    return {"text": _win_json(records)}


async def window_children(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    if sys.platform != "win32":
        records = await asyncio.to_thread(
            x11win.window_children,
            args["hwnd"], args.get("titleFilter"), args.get("classNameFilter"), args.get("pid"),
            bool(args.get("includeInvisible")),
        )
        return {"text": json.dumps(records)}
    records = await asyncio.to_thread(
        win.window_children, win.parse_hwnd(args["hwnd"]), args.get("titleFilter"), args.get("classNameFilter"),
        args.get("pid"), bool(args.get("includeInvisible")),
    )
    return {"text": _win_json(records)}


async def window_info(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    if sys.platform != "win32":
        record = await asyncio.to_thread(x11win.window_info, args["hwnd"])
        return {"text": json.dumps(record)}
    record = await asyncio.to_thread(win.window_info, win.parse_hwnd(args["hwnd"]))
    return {"text": _win_json(record)}


_FILTER_SCHEMA = {
    "titleFilter": str | None, "classNameFilter": str | None,
    "pid": int | None, "includeInvisible": bool | None,
}

PLUGIN = Plugin(
    name="windows-inspect",
    tools=[
        PluginTool(
            "window_list",
            "Enumerates top-level windows (like Spy++/WinSpy's window browser), returning handle, title, class "
            "name, owning process, screen rect, and visible/enabled state for each. Use titleFilter/"
            "classNameFilter/pid to narrow down a busy desktop. The returned hwnd (a hex string, e.g. "
            '"0x001A04F2") is what you pass to window_children/window_info and to the windows-window-screenshot/'
            "-mouse/-keyboard tools.",
            dict(_FILTER_SCHEMA), window_list,
        ),
        PluginTool(
            "window_children",
            "Enumerates the direct and nested child windows/controls of a given window (e.g. the edit box "
            "inside a dialog) via EnumChildWindows, in the same shape as window_list -- this is how you find "
            "the specific control's hwnd to target for a click or keystroke.",
            {"hwnd": str, **_FILTER_SCHEMA}, window_children,
        ),
        PluginTool(
            "window_info",
            "Returns the full record (handle, title, class name, owning process, rect, client rect, "
            "visible/enabled, control id, parent handle) for a single window handle.",
            {"hwnd": str}, window_info,
        ),
    ],
)
