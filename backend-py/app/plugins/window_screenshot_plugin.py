"""windows-window-screenshot -- PrintWindow-based single-window capture.
Windows: in-process via app.plugins._win32 (2026-10-24; was
windowscreenshot.exe), same crop/downscale and burst behavior.

Linux (2026-10-24): the window's own content through the X Composite
extension (app.plugins._x11_capture), covered parts included, like
PrintWindow. Only when the X server has no Composite extension does it fall
back to the old screen crop of the window's rect (mss) -- which captures
whatever lies on top -- and the result says so. get_window_rect's None-rect
(the WSLg sentinel, see _x11_window.py) makes that fallback an error rather
than a bogus region."""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.policies import prefer_cropped_screenshots_instruction

if sys.platform == "win32":
    from app.plugins import _win32 as win
else:
    from app.plugins import _x11_capture as x11cap
    from app.plugins import _x11_window as x11win
    from app.plugins._image_util import crop_and_downscale, to_png
    from app.plugins.screenshot_plugin import _take_screenshot_linux

_SCREEN_CROP_NOTE = (
    " (screen crop of the window's area: this X server has no Composite extension, so a part of the "
    "window covered by another window shows that window instead)"
)


def _crop_arg(args: dict[str, Any]) -> tuple[int, int, int, int] | None:
    if all(args.get(k) is not None for k in ("x", "y", "width", "height")):
        return int(args["x"]), int(args["y"]), int(args["width"]), int(args["height"])
    return None


async def _capture_window_linux(args: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    max_width = int(args["maxWidth"]) if args.get("maxWidth") is not None else None
    try:
        image = await asyncio.to_thread(x11cap.capture_window, args["hwnd"], args.get("settlePollMs"), args.get("settleMaxMs"))
    except x11cap.CompositeUnavailable:
        result = await _capture_window_screen_crop(args)
        if not result.get("is_error"):
            result["text"] += _SCREEN_CROP_NOTE
        return result
    except RuntimeError as exc:
        return {"text": str(exc), "is_error": True}
    image = crop_and_downscale(image, _crop_arg(args), max_width)
    png_bytes = to_png(image)
    save_path = args.get("savePath")
    if save_path:
        Path(save_path).write_bytes(png_bytes)
    return {
        "text": f"Captured {image.width}x{image.height}" + (f" and saved to {save_path}" if save_path else ""),
        "image_base64": base64.b64encode(png_bytes).decode("ascii"),
        "mime_type": "image/png",
    }


async def _capture_window_screen_crop(args: dict[str, Any]) -> dict[str, Any]:
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
    import asyncio

    hwnd = win.parse_hwnd(args["hwnd"])
    crop = None
    if all(args.get(k) is not None for k in ("x", "y", "width", "height")):
        crop = (int(args["x"]), int(args["y"]), int(args["width"]), int(args["height"]))
    max_width = int(args["maxWidth"]) if args.get("maxWidth") is not None else None

    def grab() -> tuple[bytes, int, int]:
        image = win.capture_window(hwnd, crop, max_width)
        return win.to_png(image), image.width, image.height

    image_bytes, width, height = await asyncio.to_thread(grab)
    save_path = args.get("savePath")
    if save_path:
        Path(save_path).write_bytes(image_bytes)
    return {
        "text": f"Captured {width}x{height}" + (f" and saved to {save_path}" if save_path else ""),
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
            result = await _capture_window_linux({  # same Composite capture, same fallback
                "hwnd": hwnd, "settlePollMs": args.get("settlePollMs"), "settleMaxMs": args.get("settleMaxMs"),
            })
            if result.get("is_error"):
                return result
            frame_path = Path(save_path) / f"frame_{i + 1:04d}.png"
            frame_path.write_bytes(base64.b64decode(result["image_base64"]))
            if i < count - 1:
                await asyncio.sleep(interval_ms / 1000)
        return {"text": f"Captured {count} frame(s) to {save_path}"}
    import asyncio
    import json
    import time

    count, interval_ms = int(args["count"]), int(args["intervalMs"])
    handle = win.parse_hwnd(hwnd)

    def burst() -> dict[str, Any]:
        digits = max(4, len(str(count)))
        frames = []
        start = time.monotonic()
        for i in range(count):
            frame_start = time.monotonic()
            file_path = Path(save_path) / f"frame_{str(i + 1).zfill(digits)}.png"
            win.capture_window_raw(handle).save(file_path, format="PNG")
            frames.append({"index": i + 1, "file": str(file_path), "elapsedMs": round((frame_start - start) * 1000)})
            if i < count - 1:
                remaining = interval_ms / 1000 - (time.monotonic() - frame_start)
                if remaining > 0:
                    time.sleep(remaining)
        return {"count": count, "intervalMs": interval_ms, "savePath": save_path, "frames": frames}

    summary = await asyncio.to_thread(burst)
    return {"text": json.dumps(summary)}


PLUGIN = Plugin(
    name="windows-window-screenshot",
    usage_instructions=prefer_cropped_screenshots_instruction(),
    tools=[
        PluginTool(
            "capture_window",
            "Captures a single window's current content as a PNG, by handle -- works even if the window is not "
            "the foreground/active window or is partially covered by other windows, since it reads the "
            "window's own content (PrintWindow on Windows, the X Composite extension on Linux) rather than "
            "cropping a screen capture. Does not work on a minimized window. Optional x/y/width/height crop a sub-rectangle out of the captured bitmap, and "
            "maxWidth downscales proportionally if the result is wider than that."
            " Linux only: the first capture of a window on a desktop without a compositor waits for the window to "
            "repaint its covered parts -- a capture every settlePollMs (default 50) until two in a row match, at "
            "most settleMaxMs (default 1000); raise settleMaxMs for a slow-to-repaint app, 0 to skip the wait.",
            {
                "hwnd": str, "savePath": str | None,
                "x": int | None, "y": int | None, "width": int | None, "height": int | None, "maxWidth": int | None,
                "settlePollMs": int | None, "settleMaxMs": int | None,
            },
            capture_window,
        ),
        PluginTool(
            "capture_window_burst",
            "Captures `count` screenshots of a window at `intervalMs` millisecond spacing, all in one call. "
            "Frames are written to disk under `savePath` as frame_0001.png, frame_0002.png, ... and NOT "
            "returned inline -- read a specific frame back afterward if you need to look at it."
            " Linux only: the first capture of a window on a desktop without a compositor waits for the window to "
            "repaint its covered parts -- a capture every settlePollMs (default 50) until two in a row match, at "
            "most settleMaxMs (default 1000); raise settleMaxMs for a slow-to-repaint app, 0 to skip the wait.",
            {"hwnd": str, "count": int, "intervalMs": int, "savePath": str,
             "settlePollMs": int | None, "settleMaxMs": int | None},
            capture_window_burst,
        ),
    ],
)
