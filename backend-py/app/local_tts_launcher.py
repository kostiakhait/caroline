"""Ports backend/src/localTtsServer.ts's launchLocalTtsServer -- launches
(once, kept running for this backend process's whole lifetime) the local
edge-tts HTTP server (python-scripts/local_tts_server.py) that
voice_api.py's synthesize_speech calls in preference to Camerlengo's
ai:tts. The point is cutting per-call latency by never paying a fresh
Python interpreter's startup cost per TTS call, not saving money -- the
Camerlengo path stays as the fallback whenever this is unavailable or
fails, same as the original.

A plain (non-detached) child of this process, same reasoning as the
original -- the WPF shell's Kill(entireProcessTree:true) on shutdown
tears it down along with everything else, no separate cleanup needed
here. No restart-on-crash -- if it dies mid-session, synthesize_speech's
own fallback to Camerlengo covers every call until the next full app
restart, so a supervisor isn't worth the complexity yet.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.logging_setup import log_event
from app.plugins.voice_api import LOCAL_TTS_PORT

# Shipped output layout (see the Makefile's packaging step) puts this
# self-contained under backend-py/python-scripts/ -- but a dev tree
# running straight from the repo (no `make build` yet) doesn't have that
# copy, only backend/python-scripts/ (the Node backend's own, unchanged
# script -- reused as-is, not forked, since it's plain dependency-free
# Python with nothing Node-specific in it). Checked in that order so a
# real packaged install never accidentally falls through to a sibling
# repo checkout that might not even exist there.
_SHIPPED_SCRIPT = Path(__file__).resolve().parent.parent / "python-scripts" / "local_tts_server.py"
_DEV_TREE_SCRIPT = Path(__file__).resolve().parent.parent.parent / "backend" / "python-scripts" / "local_tts_server.py"

# We run under pythonw.exe (no console of its own); python_exe below is the
# CONSOLE-subsystem python.exe, so without this flag Windows auto-allocates
# a real, persistently visible console window for this long-running server
# (confirmed live, 2026-09-09).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _resolve_script_path() -> Path | None:
    for candidate in (_SHIPPED_SCRIPT, _DEV_TREE_SCRIPT):
        if candidate.exists():
            return candidate
    return None


def launch_local_tts_server() -> None:
    python_exe = os.environ.get("CAROLINE_PYTHON_PATH") or sys.executable
    if os.path.isabs(python_exe) and not os.path.exists(python_exe):
        log_event("engine", "local_tts_python_not_found", path=python_exe)
        return
    script_path = _resolve_script_path()
    if script_path is None:
        log_event("engine", "local_tts_script_not_found", checked=[str(_SHIPPED_SCRIPT), str(_DEV_TREE_SCRIPT)])
        return
    try:
        proc = subprocess.Popen(
            [python_exe, str(script_path), "--port", str(LOCAL_TTS_PORT)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, creationflags=_NO_WINDOW,
        )
    except Exception as exc:
        log_event("engine", "local_tts_launch_failed", error=str(exc))
        return
    log_event("engine", "local_tts_launched", port=LOCAL_TTS_PORT, pid=proc.pid, script=str(script_path))

    def _pump_output() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            log_event("plugin:local-tts", "output", line=line.rstrip())
        code = proc.wait()
        log_event("engine", "local_tts_exited", code=code)

    import threading
    threading.Thread(target=_pump_output, daemon=True).start()
