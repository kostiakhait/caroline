"""Shared subprocess-spawning helper for every native-exe-backed plugin
(mouse/keyboard/inspect/chain/window-*/screenshot) -- ports the identical
pattern each of today's TS wrappers (mcp-servers-src/*/src/index.ts)
repeats: spawn the SAME pre-built C#/.NET exe, exit 0 = stdout is the
result, non-zero = stderr is the error. Per the migration plan, the native
executables themselves are reused as-is; only this thin spawning layer is
re-implemented per language.

CAROLINE_NATIVE_TOOLS_DIR lets deployment point at wherever the native
exes actually ship (today's build already produces them under each
mcp-servers-src/<name>/dist/); defaults to that same dev-tree location so
this runs unmodified against the current repo checkout during the Phase 2
migration itself.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

NATIVE_TOOLS_DIR = Path(
    os.environ.get("CAROLINE_NATIVE_TOOLS_DIR", r"D:\REPO\caroline\backend\mcp-servers-src")
)

# We run under pythonw.exe (no console of its own) -- without this, Windows
# auto-allocates a fresh console window for every one of these
# console-subsystem native exe calls (confirmed live, 2026-09-09: visible
# flashing windows on every mouse/keyboard/screenshot/etc. tool call).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def exe_path(server_dir: str, exe_name: str) -> Path:
    return NATIVE_TOOLS_DIR / server_dir / "dist" / exe_name


async def run_exe(exe: Path, args: list[str]) -> str:
    proc = await asyncio.create_subprocess_exec(
        str(exe), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace").strip() or f"{exe.name} exited with code {proc.returncode}")
    return stdout.decode(errors="replace").strip()


def parse_position(output: str) -> dict[str, int]:
    x_str, y_str = output.strip().split(",")
    return {"x": int(x_str), "y": int(y_str)}


# --- key-name -> Windows virtual-key-code table ----------------------------
# Ported verbatim from mcp-servers-src/keyboard/src/keys.ts (also reused by
# window-keyboard and chain's own key-name resolution) -- shared here since
# all three need the identical mapping.
_NAMED_KEYS: dict[str, int] = {
    "enter": 0x0D, "return": 0x0D, "escape": 0x1B, "esc": 0x1B, "tab": 0x09,
    "backspace": 0x08, "space": 0x20, "spacebar": 0x20, "capslock": 0x14,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "printscreen": 0x2C, "scrolllock": 0x91, "pause": 0x13, "numlock": 0x90,
    "ctrl": 0x11, "control": 0x11, "lctrl": 0xA2, "rctrl": 0xA3,
    "shift": 0x10, "lshift": 0xA0, "rshift": 0xA1,
    "alt": 0x12, "menu": 0x12, "lalt": 0xA4, "ralt": 0xA5,
    "win": 0x5B, "windows": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62, "numpad3": 0x63, "numpad4": 0x64,
    "numpad5": 0x65, "numpad6": 0x66, "numpad7": 0x67, "numpad8": 0x68, "numpad9": 0x69,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "semicolon": 0xBA, "equals": 0xBB, "comma": 0xBC, "minus": 0xBD,
    "period": 0xBE, "slash": 0xBF, "backtick": 0xC0, "grave": 0xC0,
    "openbracket": 0xDB, "backslash": 0xDC, "closebracket": 0xDD, "quote": 0xDE,
}
for _i in range(1, 25):
    _NAMED_KEYS[f"f{_i}"] = 0x6F + _i


def resolve_vk(key: str) -> int:
    normalized = key.strip().lower()
    if normalized in _NAMED_KEYS:
        return _NAMED_KEYS[normalized]
    if len(normalized) == 1:
        if "a" <= normalized <= "z":
            return ord(normalized.upper())
        if "0" <= normalized <= "9":
            return ord(normalized)
    raise ValueError(f'Unknown key name: "{key}"')
