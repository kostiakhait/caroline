"""take_screenshot -- thin Python subprocess wrapper around the SAME native
C#/.NET capture.exe the current Node backend already uses (backend/mcp-
servers-src/screenshot/native/, Capture.csproj), per the migration plan:
native OS-automation executables are reused as-is, only the spawning
wrapper gets re-implemented per language. Protocol (from the current
TypeScript wrapper, src/index.ts): `capture.exe --out <path> [--monitor N]
[--cropX/Y/Width/Height N] [--maxWidth N]`, exit 0 with stdout = the
resolution string ("1920x1080"), non-zero exit + stderr = failure.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.policies import prefer_cropped_screenshots_instruction

CAPTURE_EXE = Path(
    os.environ.get(
        "CAROLINE_CAPTURE_EXE",
        r"D:\REPO\caroline\backend\mcp-servers-src\screenshot\dist\capture.exe",
    )
)

# See native_exe.py's identical constant/comment -- we run under pythonw.exe
# (no console of its own), so this suppresses the auto-allocated console
# window every capture.exe call would otherwise flash open.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _downscale_rgb(data: bytes, width: int, height: int, max_width: int) -> tuple[bytes, int, int]:
    """Pure-Python nearest-neighbor downscale on raw RGB bytes (3 bytes/
    pixel, what mss.tools.to_png expects) -- avoids pulling in Pillow/
    numpy as a new dependency just for the rare maxWidth case. Screenshot-
    sized images, called occasionally (not a hot loop), so the plain
    Python loop cost is fine."""
    if width <= max_width:
        return data, width, height
    new_width = max_width
    new_height = max(1, round(height * new_width / width))
    out = bytearray(new_width * new_height * 3)
    for ny in range(new_height):
        sy = min(height - 1, ny * height // new_height)
        src_row = sy * width * 3
        dst_row = ny * new_width * 3
        for nx in range(new_width):
            sx = min(width - 1, nx * width // new_width)
            out[dst_row + nx * 3 : dst_row + nx * 3 + 3] = data[src_row + sx * 3 : src_row + sx * 3 + 3]
    return bytes(out), new_width, new_height


async def _take_screenshot_linux(args: dict[str, Any]) -> dict[str, Any]:
    """Linux port (2026-10-04): mss grabs the screen directly via X11
    (XGetImage) -- no native exe, no subprocess. mss.tools.to_png needs no
    Pillow (pure zlib), matching this project's no-new-heavy-deps posture."""
    import asyncio

    import mss
    import mss.tools

    def _grab() -> tuple[bytes, int, int]:
        with mss.mss() as sct:
            if args.get("monitor") is not None:
                # mss.monitors[0] is the combined virtual screen; individual
                # monitors start at index 1 -- same "monitor 0 = first real
                # monitor" convention the Windows capture.exe tool uses.
                region = dict(sct.monitors[int(args["monitor"]) + 1])
            else:
                region = dict(sct.monitors[0])
            if all(args.get(k) is not None for k in ("x", "y", "width", "height")):
                region = {
                    "left": region["left"] + int(args["x"]), "top": region["top"] + int(args["y"]),
                    "width": int(args["width"]), "height": int(args["height"]),
                }
            shot = sct.grab(region)
            return bytes(shot.rgb), shot.width, shot.height

    data, width, height = await asyncio.to_thread(_grab)
    max_width = args.get("maxWidth")
    if max_width is not None and width > int(max_width):
        data, width, height = await asyncio.to_thread(_downscale_rgb, data, width, height, int(max_width))
    png_bytes = mss.tools.to_png(data, (width, height), output=None)
    save_path = args.get("savePath")
    if save_path:
        Path(save_path).write_bytes(png_bytes)
    return {
        "text": f"Captured {width}x{height}" + (f" and saved to {save_path}" if save_path else ""),
        "image_base64": base64.b64encode(png_bytes).decode("ascii"),
        "mime_type": "image/png",
    }


async def take_screenshot(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    if sys.platform != "win32":
        return await _take_screenshot_linux(args)

    import asyncio

    with tempfile.TemporaryDirectory(prefix="caroline-screenshot-") as tmp_dir:
        out_path = Path(tmp_dir) / "screenshot.png"
        cli_args = ["--out", str(out_path)]
        # .get(k) is not None, not "k in args" -- an optional field the model
        # doesn't care about can arrive as an explicit {"field": null} instead
        # of simply being omitted, depending on how the SDK renders `T | None`
        # into JSON schema; treat both the same.
        if args.get("monitor") is not None:
            cli_args += ["--monitor", str(args["monitor"])]
        if all(args.get(k) is not None for k in ("x", "y", "width", "height")):
            cli_args += [
                "--cropX", str(args["x"]), "--cropY", str(args["y"]),
                "--cropWidth", str(args["width"]), "--cropHeight", str(args["height"]),
            ]
        if args.get("maxWidth") is not None:
            cli_args += ["--maxWidth", str(args["maxWidth"])]

        proc = await asyncio.create_subprocess_exec(
            str(CAPTURE_EXE), *cli_args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace").strip() or f"capture.exe exited with code {proc.returncode}")

        resolution = stdout.decode(errors="replace").strip()
        image_bytes = out_path.read_bytes()
        save_path = args.get("savePath")
        if save_path:
            Path(save_path).write_bytes(image_bytes)

        return {
            "text": f"Captured {resolution}" + (f" and saved to {save_path}" if save_path else ""),
            "image_base64": base64.b64encode(image_bytes).decode("ascii"),
            "mime_type": "image/png",
        }


from app.plugins.loader import Plugin, PluginTool  # noqa: E402 -- avoid a circular import at module load

PLUGIN = Plugin(
    name="windows-screenshot",
    usage_instructions=prefer_cropped_screenshots_instruction(),
    tools=[
        PluginTool(
            name="take_screenshot",
            description=(
                "Captures the Windows screen and returns it as a PNG image. By default captures the full "
                "virtual screen (all monitors combined); pass 'monitor' to capture a single monitor by its "
                "zero-based index. Optional x/y/width/height crop a sub-rectangle out of the captured bitmap, "
                "and maxWidth downscales proportionally if the result is wider than that."
            ),
            input_schema={
                "monitor": int | None, "savePath": str | None,
                "x": int | None, "y": int | None, "width": int | None, "height": int | None, "maxWidth": int | None,
            },
            handler=take_screenshot,
        )
    ],
)
