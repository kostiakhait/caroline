"""The request context for the evolution judge (2026-10-24, explicit order: "Нам надо
дать судье контекст запроса"; the user chose: the user's request, the role's full rules
and the last 5 lines of the dialogue).

current_request_context() -- {"userRequest", "recentDialogue"} of the session the
calling code runs in (the tab of session_context), or {} when there is none (a
background drain, no session yet). Sent with memory: commands (the server hands it to
evolution.selector through Api2Evolution.with_request_context) and put into the judge's
input by app/evolution_client.run_reply_agent for the client's own tournaments. Never
raises: the judge's context is a help, never a reason for a call to fail."""

from __future__ import annotations

from typing import Any

from app.logging_setup import log_event
from app.session_context import get_sessions_getter, get_tab_id

DIALOGUE_LINES = 5   # the user's number (2026-10-24): the last 5 lines of the dialogue


def current_request_context(tab_id: str | None = None) -> dict[str, Any]:
    tab_id = tab_id or get_tab_id()
    if not tab_id:
        return {}
    try:
        from app import pseudo_tab   # late: pseudo_tab imports chat_session lazily
        session = pseudo_tab._pseudo_sessions.get(tab_id) if pseudo_tab.is_pseudo_tab_id(tab_id) else None
        if session is None:
            getter = get_sessions_getter()
            session = getter(tab_id) if getter else None
        if session is None:
            return {}
        return session.evolution_request_context(DIALOGUE_LINES)
    except Exception as exc:  # noqa: BLE001 -- logged; the call goes on without the context
        log_event("engine", "evolution_request_context_failed", tab_id=tab_id, error=f"{type(exc).__name__}: {exc}"[:300])
        return {}
