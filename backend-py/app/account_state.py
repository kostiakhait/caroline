"""A small, general cache primitive for slow-to-compute, rarely-changing
account state (is the Claude subscription logged in, what's the
SquirrelWisdom balance) -- so nothing that merely WANTS that state (the
Settings panel, a tab starting its session) has to compute it from scratch,
and wait, every time it asks.

Per explicit instruction (2026-09-20): Settings sat on "Checking..." for
subscription state Caroline could long since have determined. Confirmed
live why: every Settings open ran three separate `claude auth status`
subprocesses (auth_status, mode_get and chat_mode_get each spawned their
own) plus a network round trip for the SW balance (measured >= 9 s), all
from scratch, nothing remembered between opens -- and every tab start ran
yet another `claude auth status` on top of that. Under load (a restart, a
tab compacting a huge session) "a few seconds each" became "never".

Nothing here knows what it caches -- a CachedAsyncValue is just (name, an
async fetch function). Four properties, each one a deliberate choice:

- SINGLE-FLIGHT: however many callers ask at once (four tabs starting
  together, thirteen Settings requests at once), exactly one fetch runs and
  they all share its result.
- STALE-WHILE-REVALIDATE: a caller that can tolerate slightly old data gets
  the last known value INSTANTLY and a refresh runs in the background, so
  the next ask is fresh. Only a caller with no value at all ever waits.
- STALE-ON-ERROR: a failed or timed-out refresh keeps the last good value
  rather than replacing "known" with "unknown" (a slow `claude auth status`
  under load must never look like "logged out" -- that would pop the login
  window). After a failure the next attempt waits a flat
  FAILURE_RETRY_PAUSE_S (never exponential -- standing rule).
- EXPLICIT INVALIDATION for the moments the underlying truth changes on
  purpose (login/logout/top-up), so those never show old data.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Generic, TypeVar

from app.logging_setup import log_event

T = TypeVar("T")

# Flat pause (not growing) before retrying a fetch that just failed -- keeps
# a persistently-failing source (offline, a wedged CLI) from being hammered
# by every single caller, while still recovering by itself once it can.
FAILURE_RETRY_PAUSE_S = 30.0


class CachedAsyncValue(Generic[T]):
    def __init__(self, name: str, fetch: Callable[[], Awaitable[T]]) -> None:
        self.name = name
        self._fetch = fetch
        self._value: T | None = None
        self._has_value = False
        self._fetched_at = 0.0  # time.monotonic() of the last successful fetch
        self._last_failure_at: float | None = None
        self._inflight: "asyncio.Task[T] | None" = None

    def peek(self) -> T | None:
        """Whatever is cached right now, without ever fetching or waiting."""
        return self._value if self._has_value else None

    def age_s(self) -> float | None:
        return (time.monotonic() - self._fetched_at) if self._has_value else None

    async def get(self, *, max_age_s: float, serve_stale: bool) -> T:
        """A value no older than max_age_s if there is one. Otherwise:
        serve_stale=True returns the old value immediately and refreshes in
        the background; serve_stale=False (or no value yet) waits for a
        fresh one."""
        age = self.age_s()
        if age is not None and age <= max_age_s:
            return self._value  # type: ignore[return-value]
        if age is not None and serve_stale:
            if not self._in_failure_pause():
                self._start_refresh()
            return self._value  # type: ignore[return-value]
        if age is None and self._in_failure_pause() and self._inflight is None:
            # Nothing to serve AND the source failed moments ago: say so
            # rather than immediately re-running the same failing fetch for
            # every caller.
            raise RuntimeError(f"{self.name} is unavailable right now (last attempt failed {FAILURE_RETRY_PAUSE_S:.0f}s window)")
        return await asyncio.shield(self._start_refresh())

    async def refresh(self) -> T:
        """Force a refresh now (single-flight) and wait for it."""
        return await asyncio.shield(self._start_refresh())

    def invalidate(self, *, hard: bool = False) -> None:
        """Mark the value stale. soft: the next stale-tolerant get still
        returns the old value instantly (and refreshes behind it). hard:
        forget it entirely, so the next get WAITS for a fresh one -- for a
        change the user just made on purpose, where showing the old value
        even briefly would be wrong."""
        self._fetched_at = 0.0
        if hard:
            self._has_value = False
            self._value = None
            self._last_failure_at = None

    def _in_failure_pause(self) -> bool:
        return self._last_failure_at is not None and (time.monotonic() - self._last_failure_at) < FAILURE_RETRY_PAUSE_S

    def _start_refresh(self) -> "asyncio.Task[T]":
        if self._inflight is None or self._inflight.done():
            task = asyncio.get_running_loop().create_task(self._do_fetch())
            # A background (nobody-awaiting) refresh can still fail with no
            # previous value to fall back on -- retrieve the exception so it
            # never surfaces as an "exception was never retrieved" warning.
            task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
            self._inflight = task
        return self._inflight

    async def _do_fetch(self) -> T:
        started = time.monotonic()
        try:
            value = await self._fetch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- see stale-on-error above
            self._last_failure_at = time.monotonic()
            log_event("engine", "account_state_refresh_failed", name=self.name, error=str(exc), kept_previous=self._has_value)
            if self._has_value:
                return self._value  # type: ignore[return-value]
            raise
        self._value = value
        self._has_value = True
        self._fetched_at = time.monotonic()
        self._last_failure_at = None
        log_event("engine", "account_state_refreshed", name=self.name, ms=round((self._fetched_at - started) * 1000))
        return value
