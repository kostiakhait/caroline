"""Reports what blocks the backend's event loop, and for how long.

Found 2026-10-10: the loop regularly stood still for 10-15 s at a time, and
every network call made from it (memory, Notes, the SW balance) collected
several such stalls -- 30 to 90 s for a request the server answers in under
one. The log showed the gaps but not their cause. This watchdog runs in a
thread of its own: it pings the loop every PING_INTERVAL_S, and when a ping
has gone unanswered for longer than STALL_THRESHOLD_S it logs the stack of
the loop's thread -- the code that holds it -- and again every
RESAMPLE_EVERY_S while the stall lasts; when the loop answers again it logs
how long the stall was.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import traceback

from app.logging_setup import log_event

PING_INTERVAL_S = 0.5
STALL_THRESHOLD_S = 2.0
RESAMPLE_EVERY_S = 5.0
MAX_STACK_CHARS = 6000

_started = False


def start(loop: asyncio.AbstractEventLoop) -> None:
    """Starts the watchdog for `loop`; call from inside the loop's thread."""
    global _started
    if _started:
        return
    _started = True
    loop_thread_id = threading.get_ident()
    threading.Thread(target=_watch, args=(loop, loop_thread_id), name="loop-watchdog", daemon=True).start()
    log_event("engine", "loop_watchdog_started", threshold_s=STALL_THRESHOLD_S)


def _stack_of(thread_id: int) -> str:
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return "(no frame)"
    text = "".join(traceback.format_stack(frame))
    return text[-MAX_STACK_CHARS:]


def _watch(loop: asyncio.AbstractEventLoop, loop_thread_id: int) -> None:
    answered = [time.monotonic()]

    def pong() -> None:
        answered[0] = time.monotonic()

    stalled_since: float | None = None
    last_sample = 0.0
    while not loop.is_closed():
        try:
            loop.call_soon_threadsafe(pong)
        except RuntimeError:
            return  # the loop is closed
        time.sleep(PING_INTERVAL_S)
        now = time.monotonic()
        silent = now - answered[0]
        if silent > STALL_THRESHOLD_S:
            if stalled_since is None:
                stalled_since = answered[0]
                last_sample = 0.0
            if now - last_sample >= RESAMPLE_EVERY_S:
                last_sample = now
                try:
                    log_event("engine", "event_loop_blocked", blocked_s=round(silent, 1), stack=_stack_of(loop_thread_id))
                except Exception:  # noqa: BLE001 -- a diagnostic must never take anything down
                    pass
        elif stalled_since is not None:
            try:
                log_event("engine", "event_loop_unblocked", blocked_s=round(answered[0] - stalled_since, 1))
            except Exception:  # noqa: BLE001
                pass
            stalled_since = None
