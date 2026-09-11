"""Per explicit instruction (2026-09-10): confirmed live that a background
loop (ChatSession._watchdog_loop) can die completely silently -- an
exception its own narrow `except asyncio.CancelledError` doesn't catch
propagates out of the `while` loop, ends the task, and nothing ever logs
it or notices. That one tab then permanently lost hang-detection, progress
narration, the silent-user-wait nudge, and the idle-task-drift check for
the rest of the process's lifetime -- undetected for 20+ minutes until a
human noticed the downstream symptoms (no narrator comments, a lamp that
never blinks) and asked why. No exception was ever printed anywhere.

Every "run forever" background loop in this backend should be started
through supervise() instead of a bare asyncio.create_task(loop()) --
one place enforces two guarantees every such loop needs:
  1. If the loop body raises anything other than CancelledError, that
     exception is logged in full (not swallowed, not silently fatal).
  2. The loop is then RESTARTED (per explicit instruction: "вочдог должен
     перезапускаться если упал") after a brief pause, so a single crash
     never permanently disables whatever that loop was doing -- it just
     costs one missed tick, loudly, on the record.

This is a supervisor of LAST resort, not a substitute for a loop
protecting its own tick body with its own try/except where that's already
natural to do (several loops in this codebase already do, e.g.
companion_api.py's inbox loop, ratatosk_channel.py's two loops) -- those
stay as they are; this just adds the outer safety net so a gap in that
per-tick protection (or a bug introduced later) can't go unnoticed and
unrecovered the way tonight's incident did.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from app.logging_setup import log_event

# Own choice, not a specified value -- long enough that a crash loop
# (the same bug re-raising every restart) doesn't spin hot and flood the
# log, short enough that real recovery is still effectively immediate.
RESTART_DELAY_S = 2.0


def supervise(name: str, body: Callable[[], Awaitable[None]], tab_id: str | None = None) -> "asyncio.Task[None]":
    """Runs body() -- typically an `async def _loop(): while True: ...`
    closure -- restarting it if it ever raises. `name` identifies the loop
    in logs (e.g. "watchdog", "due_check", "ratatosk_presence"); `tab_id`
    is attached to every log line when the loop belongs to one specific
    tab (None for process-wide loops)."""

    async def _run() -> None:
        while True:
            try:
                await body()
                # A loop that returns normally (its own "while not
                # self.ended" finished, e.g.) is done on purpose -- don't
                # restart something that intentionally ended.
                log_event("engine", "supervised_loop_ended", loop=name, tab_id=tab_id)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- must log, never let this die silently
                log_event(
                    "engine", "supervised_loop_crashed", loop=name, tab_id=tab_id,
                    error=str(exc), error_type=type(exc).__name__,
                )
                await asyncio.sleep(RESTART_DELAY_S)
                log_event("engine", "supervised_loop_restarting", loop=name, tab_id=tab_id)

    return asyncio.create_task(_run())
