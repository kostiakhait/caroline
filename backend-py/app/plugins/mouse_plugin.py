"""windows-mouse -- ports mcp-servers-src/mouse/src/index.ts's real
SendInput/SetCursorPos mouse control (mouse.exe), unchanged native binary.

Linux port (2026-10-04): no native exe on this platform at all -- in-process
XTest calls via app.plugins._x11_input instead (see that module's own
docstring for how it was verified against a real X server). Each X11 call
is synchronous (python-xlib, no asyncio of its own), so it's run via
asyncio.to_thread to avoid blocking the event loop during its XTest round
trip -- same non-blocking contract run_exe's subprocess await already gave
every caller on Windows."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.policies import prefer_window_targeted_input_instruction

if sys.platform == "win32":
    from app.plugins.native_exe import exe_path, parse_position, run_exe

    EXE = exe_path("mouse", "mouse.exe")
else:
    from app.plugins import _x11_input as x11


async def get_mouse_position(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    if sys.platform != "win32":
        pos = await asyncio.to_thread(x11.get_mouse_position)
        return {"text": f"{pos['x']},{pos['y']}"}
    out = await run_exe(EXE, ["--action", "Position"])
    pos = parse_position(out)
    return {"text": f"{pos['x']},{pos['y']}"}


async def move_mouse(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    x, y = args["x"], args["y"]
    if sys.platform != "win32":
        await asyncio.to_thread(x11.move_mouse, x, y)
        return {"text": f"Moved to {x},{y}"}
    out = await run_exe(EXE, ["--action", "Move", "--x", str(x), "--y", str(y)])
    pos = parse_position(out)
    return {"text": f"Moved to {pos['x']},{pos['y']}"}


async def click_mouse(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    button = args.get("button") or "Left"
    clicks = args.get("clicks") or 1
    x, y = args.get("x"), args.get("y")
    if sys.platform != "win32":
        for _ in range(clicks):
            if x is not None and y is not None:
                await asyncio.to_thread(x11.click_mouse, x, y, button)
            else:
                pos = await asyncio.to_thread(x11.get_mouse_position)
                await asyncio.to_thread(x11.click_mouse, pos["x"], pos["y"], button)
        pos = await asyncio.to_thread(x11.get_mouse_position)
        return {"text": f"Clicked {button} at {pos['x']},{pos['y']}"}
    cli = ["--action", "Click", "--button", button, "--clicks", str(clicks)]
    if x is not None and y is not None:
        cli += ["--x", str(x), "--y", str(y)]
    out = await run_exe(EXE, cli)
    pos = parse_position(out)
    return {"text": f"Clicked {button} at {pos['x']},{pos['y']}"}


async def mouse_button(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    action = args["action"]
    button = args.get("button") or "Left"
    x, y = args.get("x"), args.get("y")
    if sys.platform != "win32":
        if x is not None and y is not None:
            await asyncio.to_thread(x11.move_mouse, x, y)
        await asyncio.to_thread(x11.mouse_button, action, button)
        pos = await asyncio.to_thread(x11.get_mouse_position)
        verb = "Pressed" if action == "down" else "Released"
        return {"text": f"{verb} {button} at {pos['x']},{pos['y']}"}
    cli = ["--action", "Down" if action == "down" else "Up", "--button", button]
    if x is not None and y is not None:
        cli += ["--x", str(x), "--y", str(y)]
    out = await run_exe(EXE, cli)
    pos = parse_position(out)
    verb = "Pressed" if action == "down" else "Released"
    return {"text": f"{verb} {button} at {pos['x']},{pos['y']}"}


async def scroll_mouse(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    delta = args["delta"]
    if sys.platform != "win32":
        await asyncio.to_thread(x11.scroll_mouse, delta)
        pos = await asyncio.to_thread(x11.get_mouse_position)
        return {"text": f"Scrolled {delta} notch(es) at {pos['x']},{pos['y']}"}
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
