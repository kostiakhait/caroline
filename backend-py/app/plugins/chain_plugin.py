"""windows-chain -- ports mcp-servers-src/chain/src/index.ts's linear
GUI-automation step interpreter (chain.exe), unchanged native binary.
Unlike the other native-exe plugins, this one writes its args to a temp
JSON file (chain.exe's own protocol: `--stepsFile <path> --out <path>`)
rather than passing them on the command line -- ported as-is.

Per-step schema validation (zod's discriminatedUnion in the TS original) is
NOT reproduced here for this first port -- steps are passed through mostly
as-is (only "key" steps get their key name resolved to a VK code, and
"pid" fields get stringified, matching toWireStep()'s own two
transformations), trusting chain.exe's own validation to report a
malformed step. Worth reconsidering once the plugin API's schema story
(pydantic models vs. plain dicts) is settled more broadly.
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.plugins.native_exe import exe_path, resolve_vk, run_exe

EXE = exe_path("chain", "chain.exe")


def _to_wire_step(step: dict[str, Any]) -> dict[str, Any]:
    if step.get("op") == "key":
        wire = {k: v for k, v in step.items() if k not in ("key", "modifiers")}
        wire["vk"] = resolve_vk(step["key"])
        wire["modifierVks"] = [resolve_vk(m) for m in step.get("modifiers") or []]
        return wire
    if step.get("op") in ("kill", "restart", "wait_window") and step.get("pid") is not None:
        return {**step, "pid": str(step["pid"])}
    return dict(step)


async def run_chain(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    steps = args["steps"]
    with tempfile.TemporaryDirectory(prefix="caroline-chain-") as tmp_dir:
        steps_file = Path(tmp_dir) / "steps.json"
        out_file = Path(tmp_dir) / "checkpoint.png"
        steps_file.write_text(json.dumps([_to_wire_step(s) for s in steps]), encoding="utf-8")

        # chain.exe's own exit-code convention: 0 whenever it ran to
        # completion at all, even if the chain itself reports status
        # "failed"/"paused" in its JSON -- non-zero is reserved for "the exe
        # couldn't run at all" (bad steps file, crash).
        stdout = await run_exe(EXE, ["--stepsFile", str(steps_file), "--out", str(out_file)])
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"chain.exe produced non-JSON output: {stdout}")

        response: dict[str, Any] = {"text": json.dumps({**result, "screenshot": "(attached below)" if result.get("screenshot") else None}, indent=2)}
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
            "the exact shape of each op.",
            {"steps": list},
            run_chain,
        ),
    ],
)
