"""windows-mouse -- ports mcp-servers-src/mouse/src/index.ts's real
SendInput/SetCursorPos mouse control (mouse.exe), unchanged native binary."""

from __future__ import annotations

from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.plugins.native_exe import exe_path, parse_position, run_exe
from app.policies import prefer_window_targeted_input_instruction

EXE = exe_path("mouse", "mouse.exe")


async def get_mouse_position(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    out = await run_exe(EXE, ["--action", "Position"])
    pos = parse_position(out)
    return {"text": f"{pos['x']},{pos['y']}"}


async def move_mouse(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    out = await run_exe(EXE, ["--action", "Move", "--x", str(args["x"]), "--y", str(args["y"])])
    pos = parse_position(out)
    return {"text": f"Moved to {pos['x']},{pos['y']}"}


async def click_mouse(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    button = args.get("button") or "Left"
    cli = ["--action", "Click", "--button", button, "--clicks", str(args.get("clicks") or 1)]
    if args.get("x") is not None and args.get("y") is not None:
        cli += ["--x", str(args["x"]), "--y", str(args["y"])]
    out = await run_exe(EXE, cli)
    pos = parse_position(out)
    return {"text": f"Clicked {button} at {pos['x']},{pos['y']}"}


async def mouse_button(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    action = args["action"]
    button = args.get("button") or "Left"
    cli = ["--action", "Down" if action == "down" else "Up", "--button", button]
    if args.get("x") is not None and args.get("y") is not None:
        cli += ["--x", str(args["x"]), "--y", str(args["y"])]
    out = await run_exe(EXE, cli)
    pos = parse_position(out)
    verb = "Pressed" if action == "down" else "Released"
    return {"text": f"{verb} {button} at {pos['x']},{pos['y']}"}


async def scroll_mouse(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    delta = args["delta"]
    out = await run_exe(EXE, ["--action", "Scroll", "--delta", str(delta)])
    pos = parse_position(out)
    return {"text": f"Scrolled {delta} notch(es) at {pos['x']},{pos['y']}"}


PLUGIN = Plugin(
    name="windows-mouse",
    usage_instructions=prefer_window_targeted_input_instruction(),
    tools=[
        PluginTool("get_mouse_position", "Returns the current cursor position in screen coordinates.", {}, get_mouse_position),
        PluginTool(
            "move_mouse", "Moves the cursor to an absolute screen position.",
            {"x": int, "y": int}, move_mouse,
        ),
        PluginTool(
            "click_mouse",
            "Clicks a mouse button, optionally after moving to a position first. Use clicks:2 for a double-click.",
            {"button": str | None, "x": int | None, "y": int | None, "clicks": int | None},
            click_mouse,
        ),
        PluginTool(
            "mouse_button",
            "Presses or releases a mouse button without releasing/pressing it again. Pair a 'down' with a later 'up' to drag.",
            {"action": str, "button": str | None, "x": int | None, "y": int | None},
            mouse_button,
        ),
        PluginTool(
            "scroll_mouse",
            "Scrolls the mouse wheel. Positive delta scrolls up/forward, negative scrolls down/backward, in wheel-notch units.",
            {"delta": int}, scroll_mouse,
        ),
    ],
)
