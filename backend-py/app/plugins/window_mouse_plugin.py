"""windows-window-mouse -- posted-message (non-focus-stealing) window click.
Windows: in-process via app.plugins._win32 (2026-10-24; was windowmouse.exe).

Linux port (2026-10-04): XSendEvent via app.plugins._x11_input's
send_window_click instead of a native exe -- see that module's own
docstring for the same posted-event fidelity caveat (toolkits that check
send_event) this Windows tool's own docstring already carries."""

from __future__ import annotations

import sys
from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.policies import prefer_window_targeted_input_instruction

if sys.platform == "win32":
    from app.plugins import _win32 as win
else:
    from app.plugins import _x11_input as x11
    from app.plugins import _x11_window as x11win


async def click_window(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    hwnd, x, y = args["hwnd"], args["x"], args["y"]
    if sys.platform != "win32":
        import asyncio

        rect = await asyncio.to_thread(x11win.get_window_rect, hwnd)
        if rect["x"] is None:
            return {"text": f"Could not resolve window {hwnd}'s screen position.", "is_error": True}
        window = x11win.window_for(hwnd)
        await asyncio.to_thread(
            x11.send_window_click, window, x, y, rect["x"] + x, rect["y"] + y, args.get("button"),
        )
        return {"text": f"Clicked ({x}, {y}) in window {hwnd}."}
    import asyncio

    await asyncio.to_thread(win.post_click, win.parse_hwnd(hwnd), x, y, args.get("button") or "Left")
    return {"text": f"Clicked ({x}, {y}) in window {hwnd}."}


PLUGIN = Plugin(
    name="windows-window-mouse",
    usage_instructions=prefer_window_targeted_input_instruction(),
    tools=[
        PluginTool(
            "click_window",
            "Clicks at client-area coordinates (x, y relative to the window's own top-left, not the screen) "
            "inside a given window, by handle (from windows-inspect's window_list/window_children). Delivered "
            "as posted mouse messages directly to that window -- does NOT move the real mouse cursor, call "
            "SetForegroundWindow, or otherwise steal focus, and works even if the window is not currently "
            "active. Works for standard Win32 controls; GPU-rendered custom controls (Chromium/Electron, "
            "games) may not respond to posted clicks and need a real click via windows-mouse instead.",
            {"hwnd": str, "x": int, "y": int, "button": str | None},
            click_window,
        ),
    ],
)
