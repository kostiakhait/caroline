"""windows-window-screenshot -- ports mcp-servers-src/window-screenshot/
src/index.ts's PrintWindow-based single-window capture
(windowscreenshot.exe), unchanged native binary.

Linux port (2026-10-04): no PrintWindow equivalent exists in core X11 --
this just resolves the window's own screen rect (_x11_window.get_window_rect)
and screen-crops that region via mss, same mechanism screenshot_plugin.py's
own Linux branch already uses and already verified live. This is NOT
occlusion-safe (unlike PrintWindow, which renders the window's own content
even when covered): a window partly behind another window captures
whatever is actually on top on Linux. That's a real fidelity gap, not an
oversight -- flagged the same way this project already flags the
Windows-side "GPU-rendered controls may not respond to posted input"
fidelity gaps elsewhere, rather than silently pretending parity. Also
inherits get_window_rect's defensive None-rect fallback (see
_x11_window.py's docstring) for the one confirmed-live WSLg geometry
quirk -- a None rect here raises rather than capturing a bogus region."""

from __future__ import annotations

import base64
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.policies import prefer_cropped_screenshots_instruction

if sys.platform == "win32":
    from app.plugins.native_exe import exe_path, run_exe

    EXE = exe_path("window-screenshot", "windowscreenshot.exe")
else:
    from app.plugins import _x11_window as x11win
    from app.plugins.screenshot_plugin import _take_screenshot_linux


async def _capture_window_linux(args: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    rect = await asyncio.to_thread(x11win.get_window_rect, args["hwnd"])
    if rect["x"] is None:
        return {"text": f"Could not resolve window {args['hwnd']}'s screen position.", "is_error": True}
    shot_args: dict[str, Any] = {
        "x": rect["x"], "y": rect["y"], "width": rect["width"], "height": rect["height"],
        "maxWidth": args.get("maxWidth"), "savePath": args.get("savePath"),
    }
    if all(args.get(k) is not None for k in ("x", "y", "width", "height")):
        shot_args["x"] = rect["x"] + int(args["x"])
        shot_args["y"] = rect["y"] + int(args["y"])
        shot_args["width"] = int(args["width"])
        shot_args["height"] = int(args["height"])
    return await _take_screenshot_linux(shot_args)


async def capture_window(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    if sys.platform != "win32":
        return await _capture_window_linux(args)
    hwnd = args["hwnd"]
    with tempfile.TemporaryDirectory(prefix="caroline-window-screenshot-") as tmp_dir:
        out_path = Path(tmp_dir) / "window.png"
        cli = ["--action", "capture", "--hwnd", hwnd, "--out", str(out_path)]
        if all(args.get(k) is not None for k in ("x", "y", "width", "height")):
            cli += [
                "--cropX", str(args["x"]), "--cropY", str(args["y"]),
                "--cropWidth", str(args["width"]), "--cropHeight", str(args["height"]),
            ]
        if args.get("maxWidth") is not None:
            cli += ["--maxWidth", str(args["maxWidth"])]

        resolution = await run_exe(EXE, cli)
        image_bytes = out_path.read_bytes()
        save_path = args.get("savePath")
        if save_path:
            Path(save_path).write_bytes(image_bytes)

        return {
            "text": f"Captured {resolution}" + (f" and saved to {save_path}" if save_path else ""),
            "image_base64": base64.b64encode(image_bytes).decode("ascii"),
            "mime_type": "image/png",
        }


async def capture_window_burst(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    hwnd, save_path = args["hwnd"], args["savePath"]
    Path(save_path).mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        import asyncio

        count, interval_ms = int(args["count"]), int(args["intervalMs"])
        for i in range(count):
            result = await _capture_window_linux({"hwnd": hwnd})
            if result.get("is_error"):
                return result
            frame_path = Path(save_path) / f"frame_{i + 1:04d}.png"
            frame_path.write_bytes(base64.b64decode(result["image_base64"]))
            if i < count - 1:
                await asyncio.sleep(interval_ms / 1000)
        return {"text": f"Captured {count} frame(s) to {save_path}"}
    summary = await run_exe(EXE, [
        "--action", "burst", "--hwnd", hwnd,
        "--count", str(args["count"]), "--intervalMs", str(args["intervalMs"]), "--outDir", save_path,
    ])
    return {"text": summary}


PLUGIN = Plugin(
    name="windows-window-screenshot",
    usage_instructions=prefer_cropped_screenshots_instruction(),
    tools=[
        PluginTool(
            "capture_window",
            "Captures a single window's current content as a PNG, by handle -- works even if the window is not "
            "the foreground/active window or is partially covered by other windows, since it renders the "
            "window's own content (PrintWindow) rather than cropping a screen capture. Does not work on a "
            "minimized window. Optional x/y/width/height crop a sub-rectangle out of the captured bitmap, and "
            "maxWidth downscales proportionally if the result is wider than that.",
            {
                "hwnd": str, "savePath": str | None,
                "x": int | None, "y": int | None, "width": int | None, "height": int | None, "maxWidth": int | None,
            },
            capture_window,
        ),
        PluginTool(
            "capture_window_burst",
            "Captures `count` screenshots of a window at `intervalMs` millisecond spacing, all in one call. "
            "Frames are written to disk under `savePath` as frame_0001.png, frame_0002.png, ... and NOT "
            "returned inline -- read a specific frame back afterward if you need to look at it.",
            {"hwnd": str, "count": int, "intervalMs": int, "savePath": str},
            capture_window_burst,
        ),
    ],
)
