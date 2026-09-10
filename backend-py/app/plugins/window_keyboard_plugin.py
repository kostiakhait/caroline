"""windows-window-keyboard -- ports mcp-servers-src/window-keyboard/src/
index.ts's posted-message (non-focus-stealing) window keyboard input
(windowkeyboard.exe), unchanged native binary."""

from __future__ import annotations

from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.plugins.native_exe import exe_path, resolve_vk, run_exe
from app.policies import prefer_window_targeted_input_instruction

EXE = exe_path("window-keyboard", "windowkeyboard.exe")


async def type_window(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    hwnd, text = args["hwnd"], args["text"]
    cli = ["--action", "text", "--hwnd", hwnd, "--text", text]
    if args.get("delayMs") is not None:
        cli += ["--delayms", str(args["delayMs"])]
    await run_exe(EXE, cli)
    return {"text": f"Typed {len(text)} character(s) into window {hwnd}."}


async def press_window_key(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    hwnd, key = args["hwnd"], args["key"]
    modifiers = args.get("modifiers") or []
    vk = resolve_vk(key)
    mod_vks = [resolve_vk(m) for m in modifiers]
    cli = ["--action", "key", "--hwnd", hwnd, "--vk", str(vk)]
    if mod_vks:
        cli += ["--modifiers", ",".join(map(str, mod_vks))]
    await run_exe(EXE, cli)
    combo = "+".join([*modifiers, key])
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
