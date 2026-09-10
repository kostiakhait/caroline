"""windows-window-mouse -- ports mcp-servers-src/window-mouse/src/index.ts's
posted-message (non-focus-stealing) window click (windowmouse.exe),
unchanged native binary."""

from __future__ import annotations

from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.plugins.native_exe import exe_path, run_exe
from app.policies import prefer_window_targeted_input_instruction

EXE = exe_path("window-mouse", "windowmouse.exe")


async def click_window(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    hwnd, x, y = args["hwnd"], args["x"], args["y"]
    cli = ["--hwnd", hwnd, "--x", str(x), "--y", str(y)]
    if args.get("button"):
        cli += ["--button", args["button"]]
    await run_exe(EXE, cli)
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
