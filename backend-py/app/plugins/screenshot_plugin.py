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


async def take_screenshot(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
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
