"""windows-chain -- a fixed, linear sequence of GUI-automation steps run in
one call. Windows: app.plugins._win32_chain (2026-10-24; was chain.exe,
which was never built, so this tool could not run on Windows). Linux:
app.plugins._linux_chain. Both take the steps exactly as the model sends
them (key names, pid as a number or "$name") and return the same result:
status ok/paused/failed, completedSteps, failedAt, elapsedMs and, on a pause
or failure, a screenshot.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.plugins.loader import Plugin, PluginTool

if sys.platform == "win32":
    from app.plugins._win32_chain import run_chain_windows as _run_steps
else:
    from app.plugins._linux_chain import run_chain_linux as _run_steps


async def run_chain(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    import asyncio

    steps = args["steps"]
    with tempfile.TemporaryDirectory(prefix="caroline-chain-") as tmp_dir:
        out_file = str(Path(tmp_dir) / "checkpoint.png")
        result = await asyncio.to_thread(_run_steps, steps, out_file)
        response: dict[str, Any] = {
            "text": json.dumps({**result, "screenshot": "(attached below)" if result.get("screenshot") else None}, indent=2)
        }
        screenshot_path = result.get("screenshot")
        if screenshot_path and Path(screenshot_path).exists():
            response["image_base64"] = base64.b64encode(Path(screenshot_path).read_bytes()).decode("ascii")
            response["mime_type"] = "image/png"
        if result.get("status") == "failed":
            response["is_error"] = True
        return response


PLUGIN = Plugin(
    name="windows-chain",
    tools=[
        PluginTool(
            "run_chain",
            "Executes a fixed, linear (no branching, no loops) sequence of GUI-automation steps natively in one "
            "call, instead of one LLM round-trip per step. Mouse/keyboard steps run window-scoped (posted "
            "messages, no focus theft) when a step's hwnd is given, or screen-absolute (real SendInput/"
            "SetCursorPos, focus-stealing) when omitted. A launch/wait_window step's `as` name can be "
            'referenced later as "$name" in an hwnd or pid field. Use a `checkpoint` step to pause '
            "deliberately before a risky/irreversible action -- it returns the steps completed so far plus a "
            "screenshot; resume with a fresh run_chain call. Stops at the first step that fails (after its own "
            "retries, if any) and reports exactly which step and why. Each step is a dict with an \"op\" field "
            '(move/click/mouse_down/mouse_up/drag/key/type/scroll/sleep/wait_window/wait_pixel/wait_idle/'
            "launch/kill/restart/checkpoint) plus that op's own fields -- see windows-chain's own examples for "
            "the exact shape of each op. Linux only: wait_pixel/wait_idle/checkpoint with an hwnd read the "
            "window's own content (covered parts included); its first capture on a desktop without a compositor "
            "waits for the window to repaint -- optional step fields settlePollMs (default 50) and settleMaxMs "
            "(default 1000) set that wait.",
            {"steps": list},
            run_chain,
        ),
    ],
)
