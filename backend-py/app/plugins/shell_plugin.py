"""shell -- generic command/Python execution (2026-09-13), added alongside
files_plugin.py's read_file/write_file for the same reason: the small-model
primary path (small_model_engine.py) only ever sees tools collected via
discover_plugins(), and unlike the full Claude Agent SDK path it has no
built-in Bash of its own. Without this, any task needing to run a script or
shell command (editing a .pptx with python-pptx, checking something with a
one-off command, etc.) was silently impossible on that path rather than
escalating to the full SDK -- confirmed live as a real capability gap
(2026-09-13).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.plugins.loader import Plugin, PluginTool

# See screenshot_plugin.py's identical constant/comment -- this backend runs
# under pythonw.exe (no console of its own), so without this every spawned
# child flashes/holds open an auto-allocated console window.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_TIMEOUT_S = 120
MAX_OUTPUT_CHARS = 50_000


def _format_output(stdout: bytes, stderr: bytes, returncode: int) -> str:
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    parts = [f"exit code: {returncode}"]
    if out:
        parts.append(f"stdout:\n{out}")
    if err:
        parts.append(f"stderr:\n{err}")
    text = "\n\n".join(parts)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n\n[... truncated, {len(text)} characters total ...]"
    return text


async def _run(proc_coro: Any, timeout_s: float) -> str:
    proc = await proc_coro
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise TimeoutError(f"Command timed out after {timeout_s}s and was killed.") from None
    return _format_output(stdout, stderr, proc.returncode)


async def run_command(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    command = args["command"]
    cwd = args.get("cwd") or None
    timeout_s = args.get("timeout_s") or DEFAULT_TIMEOUT_S
    proc_coro = asyncio.create_subprocess_shell(
        command, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    text = await _run(proc_coro, timeout_s)
    return {"text": text}


async def run_python(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    code = args["code"]
    cwd = args.get("cwd") or None
    timeout_s = args.get("timeout_s") or DEFAULT_TIMEOUT_S
    # Written to a temp file rather than passed via `python -c` -- multi-line
    # scripts with embedded quotes are common (e.g. python-pptx edits) and
    # shell-escaping those through -c is exactly the kind of thing that goes
    # subtly wrong; a real file sidesteps it entirely.
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        script_path = f.name
    try:
        proc_coro = asyncio.create_subprocess_exec(
            sys.executable, script_path, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
        text = await _run(proc_coro, timeout_s)
    finally:
        Path(script_path).unlink(missing_ok=True)
    return {"text": text}


PLUGIN = Plugin(
    name="shell",
    tools=[
        PluginTool(
            "run_command",
            "Run a Windows shell command and return its exit code, stdout, and stderr. Use this for anything a "
            "human would run in a terminal -- inspecting/moving files, calling a CLI tool, etc. Defaults to a "
            f"{DEFAULT_TIMEOUT_S}s timeout (override with timeout_s); the process is killed if it exceeds it.",
            {"command": str, "cwd": str | None, "timeout_s": int | None}, run_command,
        ),
        PluginTool(
            "run_python",
            "Run a Python script (arbitrary multi-line code, using this machine's own Python environment -- "
            "including packages like python-pptx, python-docx, etc.) and return its exit code, stdout, and "
            "stderr. Use this for anything that needs real logic rather than a one-line shell command -- editing "
            f"an Office document, processing data, and so on. Defaults to a {DEFAULT_TIMEOUT_S}s timeout "
            "(override with timeout_s).",
            {"code": str, "cwd": str | None, "timeout_s": int | None}, run_python,
        ),
    ],
)
