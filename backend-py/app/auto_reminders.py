"""Automatic reminders from the owner's dialogue and from the correspondence (2026-11-20, owner's order): significant
events are never left to chance. A reminder is set at once (no question), and the owner gets one line about it.

Flow: the text (a message of the owner; a letter or messenger note Caroline read) -> reminder.extract judges what is
significant -> code schedules each through the scheduler (whose reminder.advisor refuses a duplicate of an existing
reminder) -> one notice line to the tab's window. A text is looked at once: its hash is remembered
(<workspace>/auto-reminders-seen.json) once reminder.extract has answered for it (see MAX_EXTRACT_FAILURES). Fail-open and silent on any error: a missed auto-reminder is a missed
convenience, never a broken turn."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.logging_setup import log_event

SEEN_KEEP = 800
MIN_TEXT_CHARS = 25
# Text that is machinery, not something the owner or a correspondent said.
_SKIP_PREFIXES = ("⏰ Reminder due", "[Internal", "[Your context", "State check", "Check whether you actually finished")


def _seen_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "auto-reminders-seen.json"


def _digest(text: str) -> str:
    return hashlib.sha1(" ".join(text.split())[:3000].encode("utf-8")).hexdigest()


def _seen(workspace_dir: str) -> list[str]:
    try:
        seen = json.loads(_seen_path(workspace_dir).read_text(encoding="utf-8"))
        return seen if isinstance(seen, list) else []
    except (FileNotFoundError, ValueError):
        return []


def already_seen(workspace_dir: str, text: str) -> bool:
    return _digest(text) in _seen(workspace_dir)


def remember(workspace_dir: str, text: str) -> None:
    digest, seen = _digest(text), _seen(workspace_dir)
    if digest in seen:
        return
    try:
        _seen_path(workspace_dir).write_text(json.dumps((seen + [digest])[-SEEN_KEEP:]), encoding="utf-8")
    except OSError:
        pass


def first_time(workspace_dir: str, text: str) -> bool:
    """True once per text; remembers it."""
    if already_seen(workspace_dir, text):
        return False
    remember(workspace_dir, text)
    return True


# A text whose extraction did not answer (timeout, no runtime, the ladder exhausted) is looked at again the next time it
# comes up, up to MAX_EXTRACT_FAILURES times (2026-11-23): its hash used to be remembered BEFORE the extraction, so one
# failed call dropped the letter's reminders for good. In-memory: a restart gives a text its tries again.
MAX_EXTRACT_FAILURES = 3
_failures: dict[str, int] = {}
_in_flight: set[str] = set()


def worth_looking_at(text: str) -> bool:
    text = (text or "").strip()
    return len(text) >= MIN_TEXT_CHARS and not text.startswith(_SKIP_PREFIXES)


async def _extract(text: str, source: str) -> list[dict[str, Any]] | None:
    """The reminders the agent found ([] for none); None when the agent did not answer."""
    from app.microagents.reminder_extract import REMINDER_EXTRACT
    from app.plugins.voice_api import _run_local_microagent

    result = await _run_local_microagent(REMINDER_EXTRACT, "app.microagents.reminder_extract",
                                         {"text": text, "source": source, "now": datetime.now().astimezone().isoformat(timespec="minutes")})
    output = (result or {}).get("output") if (result or {}).get("status") == "ok" else None
    if not isinstance(output, dict):
        return None
    items = output.get("reminders")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


async def _notice(tab_id: str, lines: list[str]) -> None:
    """One short line per set reminder, in the owner's language, into the tab's window."""
    from app.chat_session_shared import current_language_name, get_owner_profile, get_persona_gender
    from app.plugins.voice_api import translate_text
    from app.session_context import get_send_for_tab
    from app.workspace_dir import WORKSPACE_DIR

    send = get_send_for_tab(tab_id)
    if send is None or not lines:
        return
    text = "\n".join(lines)
    try:
        text = await translate_text(text, current_language_name(tab_id), gender=get_persona_gender(WORKSPACE_DIR),
                                    addressee_gender=get_owner_profile(WORKSPACE_DIR).gender) or text
    except Exception:  # noqa: BLE001 -- the English line is still better than none
        pass
    wire = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    await send({"type": "sdk_message", "message": wire, "isVoice": False, "isDeliveryReport": True})


async def consider(text: str, source: str, tab_id: str | None, workspace_dir: str | None = None) -> int:
    """Looks at one text; returns how many reminders were set. Never raises."""
    try:
        from app.plugins.scheduler_plugin import schedule_reminder
        from app.workspace_dir import WORKSPACE_DIR

        ws = workspace_dir or WORKSPACE_DIR
        digest = _digest(text)
        if not worth_looking_at(text) or digest in _in_flight or already_seen(ws, text):
            return 0
        _in_flight.add(digest)
        try:
            items = await _extract(text, source)
        finally:
            _in_flight.discard(digest)
        if items is None:
            _failures[digest] = _failures.get(digest, 0) + 1
            log_event("engine", "auto_reminder_extract_unanswered", tab_id=tab_id, source=source[:60], tries=_failures[digest])
            if _failures[digest] >= MAX_EXTRACT_FAILURES:
                remember(ws, text)
            return 0
        _failures.pop(digest, None)
        remember(ws, text)
        notices = []
        for item in items:
            reply = await schedule_reminder({"due_at_iso": item["remind_at_iso"], "note": item["note"], "event_type": "user",
                                             "priority": "priority"}, None)
            ok = not reply.get("is_error") and str(reply.get("text", "")).startswith("Scheduled")
            log_event("engine", "auto_reminder", tab_id=tab_id, source=source[:60], ok=ok, when=item["remind_at_iso"],
                      note=item["note"][:200], result=str(reply.get("text"))[:160])
            if ok:
                notices.append(f"⏰ Reminder set: {item['note']} ({item['event']}).")
        if notices and tab_id:
            await _notice(tab_id, notices)
        return len(notices)
    except Exception as exc:  # noqa: BLE001
        log_event("engine", "auto_reminder_failed", tab_id=tab_id, error=str(exc)[:200], error_type=type(exc).__name__)
        return 0


def consider_in_background(text: str, source: str, tab_id: str | None) -> None:
    """Fire and forget, from any plugin or session code."""
    try:
        asyncio.get_running_loop().create_task(consider(text, source, tab_id))
    except RuntimeError:
        pass
