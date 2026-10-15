"""Per-real-tab invisible "pseudo-tab" background sessions.

Per explicit instruction (2026-10-15), replacing the single shared
BACKGROUND_TAB_ID session main.py used to run (see that constant's own
pre-2026-10-15 comment, now removed): "для каждой вкладки отдельную невидимую
'псевдовкладку' для фоновых процессов с собственной опорной моделью... Основная
модель вообще не должна заниматься фоновыми задачами, только знать об их
существовании." Every real, visible tab gets its OWN lazily-created, invisible
headless ChatSession (id f"{real_tab_id}-bg") that carries ALL of that tab's own
background chores -- fired reminders (see main.py's _on_reminder_due), and
reacting to its own sub-agents' reports (see chat_session.py's
_track_background_tasks) -- never the real tab's own live session.

Deliberately NOT inserted into main.py's `sessions` dict (same precedent as
get_or_create_ratatosk_session's own RATATOSK_TAB_ID session -- though that one
inconsistently DOES end up in `sessions` too; this module does not repeat that):
a pseudo-tab is not a conversation surface the phone companion app
(_active_tab_ids/_companion_tab_status) or /api/status's "tabs" list should ever
see. Diagnosability instead goes through logs and agent_registry.py's own
per-tab files (agents-<id>-bg.json, running-agents-<id>-bg.txt), which already
work for any arbitrary tab_id string with zero changes needed there.

Module-level state lives HERE, not in main.py, because both main.py (reminder
routing) and chat_session.py (sub-agent reactivation) need to read/write it, and
chat_session.py cannot import main.py (main.py already imports ChatSession from
chat_session.py -- that would be circular)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from app.durability import load_chat_mode, save_chat_mode
from app.logging_setup import log_event
from app.workspace_dir import WORKSPACE_DIR

if TYPE_CHECKING:
    from app.chat_session import ChatSession

_PSEUDO_SUFFIX = "-bg"


def pseudo_tab_id(real_tab_id: str) -> str:
    return f"{real_tab_id}{_PSEUDO_SUFFIX}"


def real_tab_id_from_pseudo(pseudo_id: str) -> str:
    return pseudo_id[: -len(_PSEUDO_SUFFIX)] if pseudo_id.endswith(_PSEUDO_SUFFIX) else pseudo_id


def is_pseudo_tab_id(tab_id: str) -> bool:
    return tab_id.endswith(_PSEUDO_SUFFIX)


def sync_pseudo_tab_engine(workspace_dir: str, real_tab_id: str) -> None:
    """Per explicit instruction (2026-10-15): a pseudo-session must always run
    on the SAME engine/chat_mode as its own real tab, never its own separate
    default -- mirrored into the pseudo tab's own chat-mode file right before
    anything runs on it. load_chat_mode is read fresh every turn (not cached
    on ChatSession), so this stays correct even if the real tab's own mode
    changes between triggers (a reminder firing, or a sub-agent reactivation)
    -- the pseudo-session's NEXT turn picks up whatever the real tab is on at
    that moment. Called unconditionally (cheap: two small file reads/writes),
    not just once at pseudo-session creation."""
    save_chat_mode(workspace_dir, pseudo_tab_id(real_tab_id), load_chat_mode(workspace_dir, real_tab_id))


# Per real tab_id -> the task/note its CURRENT pseudo-session turn is about (a
# reminder's own note, or a sub-agent's own description) -- set right before
# whatever inject_proactive() call starts that turn, read-and-cleared once the
# turn's own result relays out (see _make_pseudo_session_send below), so
# check_background_relay_worth can judge the eventual result AGAINST what was
# actually asked, not in isolation.
_pseudo_turn_task: dict[str, str | None] = {}


def set_pseudo_turn_task(real_tab_id: str, task: str | None) -> None:
    _pseudo_turn_task[real_tab_id] = task


def pop_pseudo_turn_task(real_tab_id: str) -> str | None:
    return _pseudo_turn_task.pop(real_tab_id, None)


_pseudo_sessions: "dict[str, ChatSession]" = {}
_pseudo_sessions_starting: set[str] = set()
# Per real tab_id -> the in-progress pseudo-turn's own latest real visible text
# -- overwritten by each new one, never accumulated (same "merge only at the
# level of output, not a running commentary" shape the old shared background
# session already used).
_pseudo_turn_last_visible_text: "dict[str, str | None]" = {}

GetTargetSession = Callable[[str], "ChatSession | None"]


def _make_pseudo_session_send(real_tab_id: str, get_target_session: GetTargetSession) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """`send` for one real tab's pseudo-session -- there is no chat window to
    render this conversation in (same shape as the old _background_session_send/
    _ratatosk_session_send). Tracks the turn's own latest real visible text as
    it streams in; once a genuine `result` event arrives, relays whatever was
    last said into the OWNING real tab's own visible chat via
    ChatSession.deliver_relayed_reply, carrying the task this pseudo-turn was
    about. A turn that never said anything visible (every reply was
    [[NO_UPDATE]] -- the normal, expected outcome for most routine chores)
    relays nothing at all."""

    async def _send(event: dict[str, Any]) -> None:
        if event.get("type") != "sdk_message":
            return
        message = event.get("message") or {}
        if message.get("type") == "assistant":
            blocks = (message.get("message") or {}).get("content") or []
            text = "\n".join(
                b.get("text", "") for b in blocks
                if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
            ).strip()
            if text:
                _pseudo_turn_last_visible_text[real_tab_id] = text
        elif message.get("type") == "result":
            text = _pseudo_turn_last_visible_text.pop(real_tab_id, None)
            task = pop_pseudo_turn_task(real_tab_id)
            if not text:
                return
            target = get_target_session(real_tab_id)
            if target is None:
                log_event("engine", "pseudo_reply_dropped_no_real_tab", real_tab_id=real_tab_id, text_len=len(text))
                return
            asyncio.create_task(target.deliver_relayed_reply(text, task=task, is_voice=bool(event.get("isVoice"))))

    return _send


def get_or_create_pseudo_session_sync(real_tab_id: str, get_target_session: GetTargetSession) -> "ChatSession | None":
    """Lazily creates `real_tab_id`'s own pseudo-session -- synchronous and
    non-blocking, because its callers (main.py's _on_reminder_due) are
    themselves synchronous callbacks that must return immediately
    (scheduler_plugin.start_due_check_loop's contract). Returns None (caller
    reports "not yet delivered", same as before) while the session is still
    starting up -- the scheduler simply retries on its own next tick (~20s).
    Never torn down once created: a pseudo-session must keep firing reminders
    and reacting to its own sub-agents' reports even while the real tab is
    closed -- that is the whole point of moving this work off the real tab's
    own lifecycle."""
    from app.chat_session import ChatSession  # local: avoids a module-load cycle (chat_session.py imports this module)

    sync_pseudo_tab_engine(WORKSPACE_DIR, real_tab_id)
    existing = _pseudo_sessions.get(real_tab_id)
    if existing is not None:
        return existing
    if real_tab_id not in _pseudo_sessions_starting:
        _pseudo_sessions_starting.add(real_tab_id)

        async def _create() -> None:
            session = ChatSession(
                tab_id=pseudo_tab_id(real_tab_id), workspace_dir=WORKSPACE_DIR,
                send=_make_pseudo_session_send(real_tab_id, get_target_session),
                headless=True, approval_bridge_tab_id=real_tab_id,
            )
            await session.start()
            _pseudo_sessions[real_tab_id] = session
            _pseudo_sessions_starting.discard(real_tab_id)

        asyncio.create_task(_create())
    return None
