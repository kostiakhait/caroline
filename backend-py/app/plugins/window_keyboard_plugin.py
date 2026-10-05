"""windows-window-keyboard -- ports mcp-servers-src/window-keyboard/src/
index.ts's posted-message (non-focus-stealing) window keyboard input
(windowkeyboard.exe), unchanged native binary.

Linux port (2026-10-04): XSendEvent via app.plugins._x11_input's
send_window_text/send_window_combo instead of a native exe -- same
posted-event fidelity caveat as window_mouse_plugin.py's Linux branch."""

from __future__ import annotations

import sys
from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.policies import prefer_window_targeted_input_instruction

if sys.platform == "win32":
    from app.plugins.native_exe import exe_path, resolve_vk, run_exe

    EXE = exe_path("window-keyboard", "windowkeyboard.exe")
else:
    from app.plugins import _x11_input as x11
    from app.plugins import _x11_window as x11win


async def type_window(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    hwnd, text = args["hwnd"], args["text"]
    if sys.platform != "win32":
        import asyncio

        window = await asyncio.to_thread(x11win.window_for, hwnd)
        await asyncio.to_thread(x11.send_window_text, window, text)
        return {"text": f"Typed {len(text)} character(s) into window {hwnd}."}
    cli = ["--action", "text", "--hwnd", hwnd, "--text", text]
    if args.get("delayMs") is not None:
        cli += ["--delayms", str(args["delayMs"])]
    await run_exe(EXE, cli)
    return {"text": f"Typed {len(text)} character(s) into window {hwnd}."}


async def press_window_key(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    hwnd, key = args["hwnd"], args["key"]
    modifiers = args.get("modifiers") or []
    combo = "+".join([*modifiers, key])
    if sys.platform != "win32":
        import asyncio

        window = await asyncio.to_thread(x11win.window_for, hwnd)
        await asyncio.to_thread(x11.send_window_combo, window, key, modifiers)
        return {"text": f"Pressed {combo} in window {hwnd}."}
    vk = resolve_vk(key)
    mod_vks = [resolve_vk(m) for m in modifiers]
    cli = ["--action", "key", "--hwnd", hwnd, "--vk", str(vk)]
    if mod_vks:
        cli += ["--modifiers", ",".join(map(str, mod_vks))]
    await run_exe(EXE, cli)
    return {"text": f"Pressed {combo} in window {hwnd}."}


PLUGIN = Plugin(
    name="windows-window-keyboard",
    usage_instructions=prefer_window_targeted_input_instruction(),
    tools=[
        PluginTool(
            "type_window",
            "Types text directly into a given window or control, by handle. Delivered as posted WM_CHAR "
            "messages -- does NOT call SetFocus or SendInput, so it never steals focus and works even if the "
            "window is not currently active. Works for standard Win32 edit/static controls; GPU-rendered "
            "custom text inputs (Chromium/Electron) may ignore posted characters and need windows-keyboard's "
            "real SendInput instead.",
            {"hwnd": str, "text": str, "delayMs": int | None}, type_window,
        ),
        PluginTool(
            "press_window_key",
            'Presses a single named key (e.g. "Enter", "Tab", "a") with optional modifier keys (e.g. ["Ctrl"]) '
            "directly in a given window/control, by handle -- posted WM_KEYDOWN/WM_KEYUP messages, no focus "
            "change. Background modifier-combo fidelity isn't guaranteed against every app; reliable for plain "
            "keys and most standard-control shortcuts.",
            {"hwnd": str, "key": str, "modifiers": list | None}, press_window_key,
        ),
    ],
)
