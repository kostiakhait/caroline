"""Per-turn session context, threaded via contextvars so plugin handlers
can reach the calling ChatSession's own WS push (`ChatSession.send`)
without every plugin author needing bespoke callback plumbing -- mirrors
backend/src/*.ts's `sendToFrontend` closures (each TS tool-factory function
took its own copy of that parameter), generalized here: one ChatSession
sets this once, and every plugin handler invoked during that session's
lifetime can reach it via get_send().

contextvars.ContextVar propagates correctly to any asyncio Task spawned
(directly or indirectly) from the point where it was set -- exactly the
call chain a ChatSession's own client/pump task and everything the SDK
calls back into (including in-process MCP tool handlers) descends from.
"""

from __future__ import annotations

import contextvars
from typing import Any, Awaitable, Callable

SendFn = Callable[[dict[str, Any]], Awaitable[None]]

_current_send: contextvars.ContextVar[SendFn | None] = contextvars.ContextVar("current_send", default=None)
_current_tab_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_tab_id", default=None)


def get_send() -> SendFn:
    """Raises if called outside a live ChatSession turn (e.g. a throwaway
    test script driving a plugin handler directly) -- callers that
    genuinely need to push a WS event have no sane fallback, so failing
    loudly here is more useful than silently doing nothing."""
    send = _current_send.get()
    if send is None:
        raise RuntimeError("No active session send() context -- this tool must be called from within a live ChatSession turn.")
    return send


def set_send(send: SendFn | None) -> contextvars.Token[SendFn | None]:
    return _current_send.set(send)


def get_tab_id() -> str | None:
    """Per explicit instruction (2026-09-10): lets app/operations.py's
    dispatch() tag every Operation with the tab that started it, so
    ChatSession.stop() can cancel exactly this tab's own in-flight
    background operations (OperationRegistry is process-wide, shared by
    every tab) without touching another tab's still-running work. None
    outside a live ChatSession turn -- same non-fatal shape as a plugin
    handler invoked directly by a throwaway script, unlike get_send()
    there's no reason to raise here, callers just skip tagging."""
    return _current_tab_id.get()


def set_tab_id(tab_id: str | None) -> contextvars.Token[str | None]:
    return _current_tab_id.set(tab_id)
