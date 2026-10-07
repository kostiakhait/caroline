"""Durable event memory -- a record of things Caroline herself arranged
(appointments, meetings, anything she scheduled a reminder for), kept
separate from working_memory.py's "events" category. That category is a
small, auto-evicting short-term cache (8-entry cap, LFU+LRU eviction,
usage instructions that never actually mention this use case) -- the wrong
shape for "did I actually agree to this, and when" lookups, which is
exactly the failure this module fixes (2026-10-07 incident: Caroline, asked
about a meeting she herself had scheduled, had no durable record of it and
resorted to 483 notes_get calls trying to reconstruct it from old notes).

Per explicit instruction (2026-10-07):
- Written to AUTOMATICALLY, in code, every time schedule_reminder creates a
  reminder (see scheduler_plugin.py) -- not left to Caroline's own
  discretion to remember to log it a second time.
- Retention is by the event's OWN due date, not a count or time-window cap
  like working_memory.py's eviction -- an entry stays until some time after
  its own due_at_iso has passed (EXPIRY_GRACE_S below), then is pruned.

Same local-JSON-in-workspace-dir pattern as working_memory.py/persona.py.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

# How long an event stays recallable AFTER its own due time has passed,
# before it's pruned as no longer relevant -- generous enough to answer
# "what was that meeting about" days later, bounded so the file doesn't
# grow forever. Entries with no due_at_iso never expire this way.
EXPIRY_GRACE_S = 14 * 24 * 3600.0


@dataclass
class EventRecord:
    id: str
    text: str
    due_at_iso: str | None
    created_at: float
    source: str
    reminder_id: str | None = None


def _event_memory_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "event_memory.json"


def _load_all(workspace_dir: str) -> list[EventRecord]:
    try:
        raw = json.loads(_event_memory_path(workspace_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    events: list[EventRecord] = []
    for entry in raw:
        try:
            events.append(EventRecord(**entry))
        except TypeError:
            continue  # a malformed/old-shape entry -- drop rather than crash the whole load
    return events


def _save_all(workspace_dir: str, events: list[EventRecord]) -> None:
    raw = [asdict(e) for e in events]
    _event_memory_path(workspace_dir).write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _is_expired(event: EventRecord, now: float) -> bool:
    if not event.due_at_iso:
        return False
    try:
        due = datetime.fromisoformat(event.due_at_iso)
    except ValueError:
        return False
    if due.tzinfo is None:
        due = due.astimezone()
    return (now - due.timestamp()) > EXPIRY_GRACE_S


def _prune(events: list[EventRecord]) -> list[EventRecord]:
    now = time.time()
    return [e for e in events if not _is_expired(e, now)]


def remember_event(workspace_dir: str, text: str, due_at_iso: str | None = None, source: str = "manual", reminder_id: str | None = None) -> EventRecord:
    events = _prune(_load_all(workspace_dir))
    event = EventRecord(id=uuid.uuid4().hex, text=text, due_at_iso=due_at_iso, created_at=time.time(), source=source, reminder_id=reminder_id)
    events.append(event)
    _save_all(workspace_dir, events)
    return event


def forget_event(workspace_dir: str, event_id: str) -> bool:
    events = _load_all(workspace_dir)
    before = len(events)
    events = [e for e in events if e.id != event_id]
    if len(events) == before:
        return False
    _save_all(workspace_dir, events)
    return True


def forget_event_by_reminder_id(workspace_dir: str, reminder_id: str) -> bool:
    """Called from scheduler_plugin.py's cancel_reminder -- a cancelled
    reminder didn't actually happen, so its event-memory record shouldn't
    linger either."""
    events = _load_all(workspace_dir)
    before = len(events)
    events = [e for e in events if e.reminder_id != reminder_id]
    if len(events) == before:
        return False
    _save_all(workspace_dir, events)
    return True


def list_events(workspace_dir: str) -> list[EventRecord]:
    events = _load_all(workspace_dir)
    pruned = _prune(events)
    if len(pruned) != len(events):
        _save_all(workspace_dir, pruned)
    return pruned


def search_events(workspace_dir: str, query: str = "") -> list[EventRecord]:
    events = list_events(workspace_dir)
    q = query.strip().lower()
    if not q:
        return events
    return [e for e in events if q in e.text.lower()]
