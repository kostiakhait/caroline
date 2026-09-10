"""Monkey-patches anyio.open_process to default creationflags to
CREATE_NO_WINDOW on Windows whenever the caller doesn't specify one --
without this, claude_agent_sdk's own claude.exe spawn (vendored
third-party code, not ours to edit directly -- see
_internal/transport/subprocess_cli.py's own `anyio.open_process(cmd,
stdin=PIPE, stdout=PIPE, stderr=stderr_dest, cwd=..., env=..., user=...)`
call, which never passes creationflags) flashes/holds open a visible
console window every time a fresh CLI subprocess is spawned. This whole
backend runs under pythonw.exe (no console of its own), so Windows
auto-allocates one for any console-subsystem child unless told not to.
Confirmed live (2026-09-09) as the actual remaining source of console-
window flashing after the 4 in-house subprocess-spawn call sites
(subscription_mode.py, native_exe.py, screenshot_plugin.py,
local_tts_launcher.py) were already fixed directly -- a fresh claude.exe
gets spawned on every ChatSession._run_loop() restart, and dehydration
forces one after EVERY turn across every open tab, so this was the
dominant source, not a minor contributor as first assumed.

anyio.open_process is looked up as a plain module attribute at call time
(`import anyio; anyio.open_process(...)`), so reassigning it here before
anything spawns a query() is enough to transparently intercept every
caller, SDK included -- no vendored file edited, survives an SDK upgrade
as long as it keeps calling anyio.open_process the same way. Must be
applied before app.chat_session (or anything else that imports
claude_agent_sdk) ever actually spawns a subprocess -- call apply() once,
as early as possible in main.py/run_server.py.
"""

from __future__ import annotations

import subprocess
from typing import Any

import anyio

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_real_open_process = anyio.open_process
_applied = False


async def _patched_open_process(*args: Any, **kwargs: Any) -> Any:
    if _NO_WINDOW and "creationflags" not in kwargs:
        kwargs["creationflags"] = _NO_WINDOW
    return await _real_open_process(*args, **kwargs)


def apply() -> None:
    global _applied
    if _applied:
        return
    anyio.open_process = _patched_open_process
    _applied = True
