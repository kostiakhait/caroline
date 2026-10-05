"""windows-inspect -- ports mcp-servers-src/inspect/src/index.ts's Windows
UI Automation window enumeration (inspect.exe), unchanged native binary.
Returns raw JSON text from the exe (stdout is already a JSON array/object),
matching the current TS wrapper's own jsonResult() passthrough.

Linux port (2026-10-04): in-process recursive XQueryTree via
app.plugins._x11_window instead of a native exe -- see that module's own
docstring for why it doesn't rely on _NET_CLIENT_LIST (confirmed live:
not supported by the WSLg window manager used to test this)."""

from __future__ import annotations

import json
import sys
from typing import Any

from app.plugins.loader import Plugin, PluginTool

if sys.platform == "win32":
    from app.plugins.native_exe import exe_path, run_exe

    EXE = exe_path("inspect", "inspect.exe")
else:
    import asyncio

    from app.plugins import _x11_window as x11win


def _filter_args(args: dict[str, Any]) -> list[str]:
    cli: list[str] = []
    if args.get("titleFilter") is not None:
        cli += ["--titleFilter", args["titleFilter"]]
    if args.get("classNameFilter") is not None:
        cli += ["--classNameFilter", args["classNameFilter"]]
    if args.get("pid") is not None:
        cli += ["--pid", str(args["pid"])]
    if args.get("includeInvisible") is not None:
        cli += ["--includeInvisible", str(args["includeInvisible"])]
    return cli


async def window_list(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    if sys.platform != "win32":
        records = await asyncio.to_thread(
            x11win.list_windows,
            args.get("titleFilter"), args.get("classNameFilter"), args.get("pid"),
            bool(args.get("includeInvisible")),
        )
        return {"text": json.dumps(records)}
    out = await run_exe(EXE, ["--action", "list", *_filter_args(args)])
    return {"text": out}


async def window_children(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    if sys.platform != "win32":
        records = await asyncio.to_thread(
            x11win.window_children,
            args["hwnd"], args.get("titleFilter"), args.get("classNameFilter"), args.get("pid"),
            bool(args.get("includeInvisible")),
        )
        return {"text": json.dumps(records)}
    out = await run_exe(EXE, ["--action", "children", "--hwnd", args["hwnd"], *_filter_args(args)])
    return {"text": out}


async def window_info(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    if sys.platform != "win32":
        record = await asyncio.to_thread(x11win.window_info, args["hwnd"])
        return {"text": json.dumps(record)}
    out = await run_exe(EXE, ["--action", "info", "--hwnd", args["hwnd"]])
    return {"text": out}


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
