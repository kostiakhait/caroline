"""windows-keyboard -- ports mcp-servers-src/keyboard/src/index.ts's real
SendInput keyboard control (keyboard.exe), unchanged native binary."""

from __future__ import annotations

from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.plugins.native_exe import exe_path, resolve_vk, run_exe
from app.policies import prefer_window_targeted_input_instruction

EXE = exe_path("keyboard", "keyboard.exe")


async def type_text(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    text = args["text"]
    delay_ms = args.get("delayMs")
    await run_exe(EXE, ["--action", "Type", "--text", text, "--delayms", str(delay_ms if delay_ms is not None else 10)])
    return {"text": f"Typed {len(text)} character(s)"}


async def press_key(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    key = args["key"]
    modifiers = args.get("modifiers") or []
    vk = resolve_vk(key)
    mod_vks = [resolve_vk(m) for m in modifiers]
    await run_exe(EXE, ["--action", "Press", "--vk", str(vk), "--modifiers", ",".join(map(str, mod_vks))])
    combo = "+".join([*modifiers, key])
    return {"text": f"Pressed {combo}"}


async def key_down(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    key = args["key"]
    await run_exe(EXE, ["--action", "Down", "--vk", str(resolve_vk(key))])
    return {"text": f"Holding {key} down"}


async def key_up(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    key = args["key"]
    await run_exe(EXE, ["--action", "Up", "--vk", str(resolve_vk(key))])
    return {"text": f"Released {key}"}


PLUGIN = Plugin(
    name="windows-keyboard",
    usage_instructions=prefer_window_targeted_input_instruction(),
    tools=[
        PluginTool(
            "type_text",
            "Types arbitrary Unicode text by injecting one keystroke per character. Newlines are sent as "
            "literal characters, which most text fields treat as Enter.",
            {"text": str, "delayMs": int | None}, type_text,
        ),
        PluginTool(
            "press_key",
            "Presses a single key, optionally combined with modifiers held down for the duration (e.g. key:'c', "
            "modifiers:['Ctrl'] for Ctrl+C). Key names: single letters/digits, or named keys like Enter, Escape, "
            "Tab, Backspace, Space, Left/Right/Up/Down, Home, End, PageUp, PageDown, Insert, Delete, F1-F24, "
            "Ctrl, Shift, Alt, Win.",
            {"key": str, "modifiers": list | None}, press_key,
        ),
        PluginTool(
            "key_down",
            "Presses a key down without releasing it. Pair with key_up to release, e.g. for holding a movement "
            "key or building a custom modifier combo across calls.",
            {"key": str}, key_down,
        ),
        PluginTool("key_up", "Releases a key previously pressed with key_down.", {"key": str}, key_up),
    ],
)
