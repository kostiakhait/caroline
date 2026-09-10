"""windows-window-screenshot -- ports mcp-servers-src/window-screenshot/
src/index.ts's PrintWindow-based single-window capture
(windowscreenshot.exe), unchanged native binary."""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.plugins.native_exe import exe_path, run_exe
from app.policies import prefer_cropped_screenshots_instruction

EXE = exe_path("window-screenshot", "windowscreenshot.exe")


async def capture_window(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
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
