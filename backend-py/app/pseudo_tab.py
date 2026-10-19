"""Per-background-activity invisible "pseudo-tab" sessions.

Per explicit instruction (2026-10-15, reshaped 2026-10-19 after a confirmed
live incident -- see below): "для каждой вкладки отдельную невидимую
'псевдовкладку' для фоновых процессов с собственной опорной моделью...
Основная модель вообще не должна заниматься фоновыми задачами, только знать
об их существовании." Every background CHORE (a fired reminder, see
main.py's _on_reminder_due; a sub-agent's own asynchronous completion
report, see chat_session_mixins/watchdog.py's _track_background_tasks) gets
its OWN freshly-created, invisible, headless ChatSession, used for exactly
one turn and then disposed -- never the real tab's own live session, and
never shared with any OTHER background chore either.

Reshaped (2026-10-19) from "one shared `{tab}-bg` session per real tab,
reused forever across every chore" to "one `{tab}-bg-{activity}` session per
CHORE INSTANCE, created on demand and disposed once its one turn's result
relays out" -- per explicit instruction ("каждая фоновая активность должна
быть ОТДЕЛЬНОЙ ниткой рассуждений... никогда не должны поступать одной и
той же модели"), after a live incident where a real tab's own model called
the Claude Agent SDK's own NATIVE ScheduleWakeup tool (see
chat_session_shared.py's own note on the SDK's native `/loop` mechanism,
which bypasses this whole module entirely) to self-reschedule an ongoing
background check directly in its own live session, repeatedly re-waking
that same session and at one point causing it to misread which message was
"the previous message" when the real user asked it to look back and
answer. ScheduleWakeup and friends are now blocked outright (see
chat_session_mixins/engine_lifecycle.py's own disallowed_tools) -- this
module's own job is the other half: make sure that even a LEGITIMATE
background chore (a reminder, an async sub-agent report) never ends up
sharing one session/one conversation history with a different chore, or
with the real tab's own live dialogue.

Deliberately NOT inserted into main.py's `sessions` dict (same precedent as
get_or_create_ratatosk_session's own RATATOSK_TAB_ID session -- though that
one inconsistently DOES end up in `sessions` too; this module does not
repeat that): a pseudo-tab is not a conversation surface the phone companion
app (_active_tab_ids/_companion_tab_status) or /api/status's "tabs" list
should ever see. Diagnosability instead goes through logs and
agent_registry.py's own per-tab files (agents-<id>.json,
running-agents-<id>.txt), which already work for any arbitrary tab_id
string with zero changes needed there.

Module-level state lives HERE, not in main.py, because both main.py
(reminder routing) and chat_session_mixins/watchdog.py (sub-agent
reactivation) need to read/write it, and chat_session.py cannot import
main.py (main.py already imports ChatSession from chat_session.py -- that
would be circular)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from app.durability import load_chat_mode, save_chat_mode
from app.logging_setup import log_event
from app.workspace_dir import WORKSPACE_DIR

if TYPE_CHECKING:
    from app.chat_session import ChatSession

# Separates the real tab id from the activity id inside a pseudo tab_id
# string (f"{real_tab_id}{_PSEUDO_MARKER}{activity_id}") -- a real tab_id
# never naturally contains this exact substring, so real_tab_id_from_pseudo
# can split on its first occurrence unambiguously.
_PSEUDO_MARKER = "-bg-"


def pseudo_tab_id(real_tab_id: str, activity_id: str) -> str:
    return f"{real_tab_id}{_PSEUDO_MARKER}{activity_id}"


def real_tab_id_from_pseudo(pseudo_id: str) -> str:
    idx = pseudo_id.find(_PSEUDO_MARKER)
    return pseudo_id[:idx] if idx != -1 else pseudo_id


def is_pseudo_tab_id(tab_id: str) -> bool:
    return _PSEUDO_MARKER in tab_id


def sync_pseudo_tab_engine(workspace_dir: str, real_tab_id: str, pseudo_id: str) -> None:
    """Per explicit instruction (2026-10-15): a pseudo-session must always run
    on the SAME engine/chat_mode as its own real tab, never its own separate
    default -- mirrored into the pseudo tab's own chat-mode file right before
    anything runs on it. load_chat_mode is read fresh every turn (not cached
    on ChatSession), so a freshly-created per-activity session always picks
    up whatever the real tab is on at the moment it's created."""
    save_chat_mode(workspace_dir, pseudo_id, load_chat_mode(workspace_dir, real_tab_id))


# Per pseudo tab_id (one per activity INSTANCE, never shared) -- the task/
# note that one activity's own turn is about (a reminder's own note, or a
# sub-agent's own description), set right before the inject_proactive()
# call that starts that turn, read-and-cleared once the turn's own result
# relays out (see _make_pseudo_session_send below), so
# check_background_relay_worth can judge the eventual result AGAINST what
# was actually asked, not in isolation. Keyed by the full pseudo_id (unique
# per activity instance) rather than real_tab_id -- with one session per
# activity instance, two chores can never collide on this slot even in
# principle, since they're never the same key.
_pseudo_turn_task: dict[str, str | None] = {}


def set_pseudo_turn_task(pseudo_id: str, task: str | None) -> None:
    _pseudo_turn_task[pseudo_id] = task


def pop_pseudo_turn_task(pseudo_id: str) -> str | None:
    return _pseudo_turn_task.pop(pseudo_id, None)


_pseudo_sessions: "dict[str, ChatSession]" = {}
_pseudo_sessions_starting: set[str] = set()
# Per pseudo tab_id -- the in-progress pseudo-turn's own latest real visible
# text, overwritten by each new assistant block, popped once the turn's
# result arrives. See _pseudo_turn_task's own comment for why keying by the
# full pseudo_id (not real_tab_id) makes cross-activity collision
# structurally impossible now, not just unlikely.
_pseudo_turn_last_visible_text: "dict[str, str | None]" = {}

GetTargetSession = Callable[[str], "ChatSession | None"]


def _make_pseudo_session_send(real_tab_id: str, pseudo_id: str, get_target_session: GetTargetSession) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """`send` for one background activity's own, single-use pseudo-session --
    there is no chat window to render this conversation in (same shape as
    the old _background_session_send/_ratatosk_session_send). Tracks the
    turn's own latest real visible text as it streams in; once a genuine
    `result` event arrives, disposes this activity's session (its one job is
    done) and relays whatever was last said into the OWNING real tab's own
    visible chat via ChatSession.deliver_relayed_reply, carrying the task
    this pseudo-turn was about. A turn that never said anything visible
    (every reply was [[NO_UPDATE]] -- the normal, expected outcome for most
    routine chores) relays nothing at all, but the session is disposed
    either way: its one turn is already over."""

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
                _pseudo_turn_last_visible_text[pseudo_id] = text
        elif message.get("type") == "result":
            text = _pseudo_turn_last_visible_text.pop(pseudo_id, None)
            task = pop_pseudo_turn_task(pseudo_id)
            _dispose_pseudo_session(pseudo_id)
            if not text:
                return
            target = get_target_session(real_tab_id)
            if target is None:
                log_event("engine", "pseudo_reply_dropped_no_real_tab", real_tab_id=real_tab_id, pseudo_id=pseudo_id, text_len=len(text))
                return
            asyncio.create_task(target.deliver_relayed_reply(text, task=task, is_voice=bool(event.get("isVoice"))))

    return _send


def _dispose_pseudo_session(pseudo_id: str) -> None:
    """A per-activity session's own one turn is over -- tear it down now
    rather than leaving it idle forever (today's `inject_proactive` on an
    already-ended session just silently no-ops, see its own `self.ended`
    check, so nothing re-uses a disposed session by accident). The SAME
    activity_id's next occurrence (a recurring reminder's next firing, a
    fresh sub-agent spawn) lazily creates a brand-new session again, same
    as the very first time."""
    session = _pseudo_sessions.pop(pseudo_id, None)
    _pseudo_sessions_starting.discard(pseudo_id)
    if session is not None:
        session.dispose()


async def _start_pseudo_session(real_tab_id: str, pseudo_id: str, get_target_session: GetTargetSession) -> "ChatSession":
    from app.chat_session import ChatSession  # local: avoids a module-load cycle (chat_session.py imports this module)

    session = ChatSession(
        tab_id=pseudo_id, workspace_dir=WORKSPACE_DIR,
        send=_make_pseudo_session_send(real_tab_id, pseudo_id, get_target_session),
        headless=True, approval_bridge_tab_id=real_tab_id,
    )
    await session.start()
    return session


def get_or_create_pseudo_session_sync(real_tab_id: str, activity_id: str, get_target_session: GetTargetSession) -> "ChatSession | None":
    """Lazily creates one background activity's own pseudo-session --
    synchronous and non-blocking, because this caller (main.py's
    _on_reminder_due, scheduler_plugin.start_due_check_loop's own sync
    callback contract) must return immediately. Returns None (caller
    reports "not yet delivered", same as before) while the session is still
    starting up -- the scheduler simply retries on its own next tick
    (~20s), which is fine for a recurring reminder (there will always be a
    next tick) but would silently lose a true one-shot notification with no
    retry loop of its own -- see get_or_create_pseudo_session below for
    that case.

    `activity_id` identifies THIS specific chore instance (a reminder's own
    id, a sub-agent's own task_id, a fixed label like "shutdown-backup") --
    a DIFFERENT activity_id for the same real_tab_id always gets its own,
    completely separate session, never sharing turn-state or conversation
    history with another activity. Disposed automatically once its one
    turn's result relays out (see _dispose_pseudo_session) -- the same
    activity_id's next occurrence creates a fresh session again."""
    pseudo_id = pseudo_tab_id(real_tab_id, activity_id)
    sync_pseudo_tab_engine(WORKSPACE_DIR, real_tab_id, pseudo_id)
    existing = _pseudo_sessions.get(pseudo_id)
    if existing is not None:
        return existing
    if pseudo_id not in _pseudo_sessions_starting:
        _pseudo_sessions_starting.add(pseudo_id)

        async def _create() -> None:
            session = await _start_pseudo_session(real_tab_id, pseudo_id, get_target_session)
            _pseudo_sessions[pseudo_id] = session
            _pseudo_sessions_starting.discard(pseudo_id)

        asyncio.create_task(_create())
    return None


async def get_or_create_pseudo_session(real_tab_id: str, activity_id: str, get_target_session: GetTargetSession) -> "ChatSession":
    """Async counterpart of get_or_create_pseudo_session_sync, for a caller
    that's already inside an async context and can simply await creation
    instead of polling (2026-10-19, watchdog.py's own async sub-agent-
    completion reroute -- unlike the reminder scheduler's sync callback,
    there's no external retry loop behind a one-shot sub-agent notification,
    so returning None while "still starting" would silently lose it with
    nothing to ever retry it). Waits out a concurrent creation already in
    progress for the exact same activity rather than starting a second one
    (defensive -- in practice a given activity_id, e.g. a sub-agent's own
    unique task_id, is never notified twice concurrently)."""
    pseudo_id = pseudo_tab_id(real_tab_id, activity_id)
    sync_pseudo_tab_engine(WORKSPACE_DIR, real_tab_id, pseudo_id)
    existing = _pseudo_sessions.get(pseudo_id)
    if existing is not None:
        return existing
    if pseudo_id in _pseudo_sessions_starting:
        while pseudo_id in _pseudo_sessions_starting:
            await asyncio.sleep(0.05)
        existing = _pseudo_sessions.get(pseudo_id)
        if existing is not None:
            return existing
    _pseudo_sessions_starting.add(pseudo_id)
    try:
        session = await _start_pseudo_session(real_tab_id, pseudo_id, get_target_session)
        _pseudo_sessions[pseudo_id] = session
        return session
    finally:
        _pseudo_sessions_starting.discard(pseudo_id)
