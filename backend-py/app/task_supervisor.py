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

Heartbeat watchdog (2026-10-11), per explicit instruction after a real
incident: Telegram's self-account background loop got stuck forever
inside `await client.connect()` -- no exception, no return, so the
guarantees above never kicked in; `supervise()` just awaited a coroutine
that would never resolve, for 55+ minutes, until a human noticed and
forced a fresh login (which didn't even fix it, since the stuck task
was a completely separate coroutine from the one a fresh login touches).

A GENERIC stall detector can't just clock how long body() has been
running -- most of these loops (an open Telegram/WhatsApp/Signal
connection, `client.run_until_disconnected()`) are SUPPOSED to run for
hours, that's correct operation, not a hang. The only honest way to tell
"stuck" from "healthily running forever" from the OUTSIDE is to have the
loop itself report progress: `heartbeat_timeout_s`, when given, changes
`body`'s own call signature from `body()` to `body(touch)` -- `touch()`
is a zero-arg callback the loop calls whenever it reaches a point that
proves it's not stuck (a successful connect, then periodically for as
long as it considers itself healthily connected). If `touch()` hasn't
been called within `heartbeat_timeout_s`, the body task is cancelled and
restarted, the same way a raised exception already triggers a restart --
cancellation reaches body's own `finally` blocks normally, so existing
cleanup (closing a client, clearing `_live_clients`, etc.) still runs.
Opt-in and backwards compatible: a caller that never passes
`heartbeat_timeout_s` keeps body's original zero-arg signature and
exactly today's behavior.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from app.logging_setup import log_event

# Own choice, not a specified value -- long enough that a crash loop
# (the same bug re-raising every restart) doesn't spin hot and flood the
# log, short enough that real recovery is still effectively immediate.
RESTART_DELAY_S = 2.0

# How often the heartbeat watchdog checks for staleness, independent of
# heartbeat_timeout_s itself -- just the polling grain, kept well below
# any sane timeout so detection latency is never the bottleneck.
_HEARTBEAT_POLL_S = 2.0


def supervise(
    name: str,
    body: Callable[..., Awaitable[None]],
    tab_id: str | None = None,
    *,
    heartbeat_timeout_s: float | None = None,
) -> "asyncio.Task[None]":
    """Runs body() -- typically an `async def _loop(): while True: ...`
    closure -- restarting it if it ever raises. `name` identifies the loop
    in logs (e.g. "watchdog", "due_check", "ratatosk_presence"); `tab_id`
    is attached to every log line when the loop belongs to one specific
    tab (None for process-wide loops).

    `heartbeat_timeout_s`: opt-in stall watchdog (see module docstring).
    When given, `body` must accept one argument, `touch: Callable[[],
    None]`, and call it whenever it has just proven it isn't stuck. A
    body that goes longer than `heartbeat_timeout_s` without calling
    `touch()` is cancelled and restarted."""

    async def _run_plain() -> None:
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

    async def _run_with_heartbeat(timeout_s: float) -> None:
        while True:
            last_touch = time.monotonic()

            def touch() -> None:
                nonlocal last_touch
                last_touch = time.monotonic()

            body_task: "asyncio.Task[None]" = asyncio.ensure_future(body(touch))
            stalled = False
            try:
                while not body_task.done():
                    await asyncio.sleep(min(_HEARTBEAT_POLL_S, timeout_s))
                    if body_task.done():
                        break
                    idle_s = time.monotonic() - last_touch
                    if idle_s >= timeout_s:
                        stalled = True
                        log_event(
                            "engine", "supervised_loop_stalled", loop=name, tab_id=tab_id,
                            idle_s=round(idle_s, 1), timeout_s=timeout_s,
                        )
                        body_task.cancel()
                        break
            except asyncio.CancelledError:
                # supervise() itself is being torn down (process shutdown) --
                # cancel the body and propagate, not a stall-triggered restart.
                body_task.cancel()
                raise

            try:
                await body_task
            except asyncio.CancelledError:
                if not stalled:
                    raise
                # Stall-triggered cancellation: body's own finally blocks
                # already ran by the time await body_task returns here.
                await asyncio.sleep(RESTART_DELAY_S)
                log_event("engine", "supervised_loop_restarting", loop=name, tab_id=tab_id, reason="stalled")
                continue
            except Exception as exc:  # noqa: BLE001 -- must log, never let this die silently
                log_event(
                    "engine", "supervised_loop_crashed", loop=name, tab_id=tab_id,
                    error=str(exc), error_type=type(exc).__name__,
                )
                await asyncio.sleep(RESTART_DELAY_S)
                log_event("engine", "supervised_loop_restarting", loop=name, tab_id=tab_id)
                continue
            # body_task finished on its own, normally -- same "done on
            # purpose" reasoning as the plain path above.
            log_event("engine", "supervised_loop_ended", loop=name, tab_id=tab_id)
            return

    if heartbeat_timeout_s is None:
        return asyncio.create_task(_run_plain())
    return asyncio.create_task(_run_with_heartbeat(heartbeat_timeout_s))
