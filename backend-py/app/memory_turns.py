"""memory_turns -- what a "turn" is for the memory service's money rules.

The server lets a turn that started while the user's wallet had money run to
its end even if the wallet runs dry in the middle of it (see
docs/MICROAGENTS_PLAN.md, section 4). For that it needs to know which memory
requests belong to one turn: every request carries a turn id.

A turn starts with a real message from the user and lasts for as long as
Caroline keeps working on it, automatic continuations included. A turn nobody
asked for in so many words -- a reminder firing -- starts a turn of its own.

The same record keeps what the turn started from, so that a turn stopped
because the memory service's provider ran out of money can be started again
from the user's message once the money is back.

Keyed by tab, in memory only: after a restart the next request simply starts
a fresh turn.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

UpstreamNoFundsHandler = Callable[[str], None]

_turns: dict[str, dict[str, Any]] = {}
_handlers: dict[str, UpstreamNoFundsHandler] = {}


def start_turn(tab_id: str, text: str, attachments: list[Any] | None = None, is_voice: bool = False) -> str:
    """A real message from the user begins a turn."""
    turn_id = uuid.uuid4().hex
    _turns[tab_id] = {"id": turn_id, "text": text, "attachments": list(attachments or []), "is_voice": is_voice}
    return turn_id


def start_service_turn(tab_id: str) -> str:
    """A turn with no user message behind it (a reminder): nothing to replay."""
    turn_id = uuid.uuid4().hex
    _turns[tab_id] = {"id": turn_id, "text": None, "attachments": [], "is_voice": False}
    return turn_id


def current_turn(tab_id: str | None) -> dict[str, Any] | None:
    return _turns.get(tab_id) if tab_id else None


def current_turn_id(tab_id: str | None) -> str | None:
    """The id to send with a memory request; a tab that has not had a turn
    yet gets one now."""
    if not tab_id:
        return None
    turn = _turns.get(tab_id)
    return turn["id"] if turn else start_service_turn(tab_id)


def set_upstream_no_funds_handler(tab_id: str, handler: UpstreamNoFundsHandler | None) -> None:
    if handler is None:
        _handlers.pop(tab_id, None)
    else:
        _handlers[tab_id] = handler


def notify_upstream_no_funds(tab_id: str | None, reason: str) -> bool:
    """Tells the tab's session that the memory service's provider is out of
    money. True when a session took it."""
    handler = _handlers.get(tab_id) if tab_id else None
    if handler is None:
        return False
    handler(reason)
    return True
