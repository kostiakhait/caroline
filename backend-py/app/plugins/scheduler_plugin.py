"""scheduler -- ports backend/src/scheduler.ts's schedule_reminder/
list_reminders/cancel_reminder tools, PLUS (2026-09-09) the proactive-firing
mechanism (startDueCheckLoop/nextOccurrence/ensureRecurringBackup in the
original) -- a background poll loop that injects a due reminder as a new
message into a live chat session (hasLiveDialog-aware background/priority
distinction). This is genuine session/engine-level infrastructure, not a
per-tool concern, so start_due_check_loop/ensure_recurring_backup are called
from app/main.py (which owns primary_session()), not from here -- this
module just exposes them, same file-organization choice scheduler.ts itself
made (tool-creation and the due-check loop coexist in one file there too).
Storage is workspace/schedule.json, survives both a session restart and a
full app restart.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.logging_setup import log_event
from app.plugins.loader import Plugin, PluginTool
from app.task_supervisor import supervise
from app.workspace_dir import WORKSPACE_DIR


def _schedule_path() -> Path:
    return Path(WORKSPACE_DIR) / "schedule.json"


def _load_reminders() -> list[dict[str, Any]]:
    path = _schedule_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_reminders(reminders: list[dict[str, Any]]) -> None:
    path = _schedule_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reminders, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


async def schedule_reminder(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    reminders = _load_reminders()
    recurring = args.get("recurring")
    recurring_every_minutes = args.get("recurring_every_minutes")
    reminder = {
        "id": uuid.uuid4().hex,
        "dueAtIso": args["due_at_iso"],
        "note": args["note"],
        "createdAtIso": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fired": False,
        "priority": args.get("priority") or "priority",
        "recurringCalendar": recurring if recurring and recurring != "none" else None,
        "recurringMs": recurring_every_minutes * 60_000 if recurring_every_minutes else None,
    }
    reminders.append(reminder)
    _save_reminders(reminders)
    rec_text = (
        f", recurring {reminder['recurringCalendar']}" if reminder["recurringCalendar"]
        else f", recurring every {recurring_every_minutes}min" if reminder["recurringMs"]
        else ""
    )
    return {"text": f"Scheduled (id {reminder['id']}) for {args['due_at_iso']} [{reminder['priority']}{rec_text}]: {args['note']}"}


async def list_reminders(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    pending = [r for r in _load_reminders() if not r.get("fired")]
    return {"text": json.dumps(pending, indent=2, ensure_ascii=False) if pending else "No pending reminders."}


async def cancel_reminder(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    before = _load_reminders()
    after = [r for r in before if r.get("id") != args["id"]]
    _save_reminders(after)
    removed = len(after) < len(before)
    return {"text": f"Cancelled {args['id']}." if removed else f"No pending reminder with id {args['id']}."}


def _next_occurrence(reminder: dict[str, Any]) -> datetime:
    """Ported verbatim from scheduler.ts's nextOccurrence: calendar recurrence
    advances by whole calendar days (1 or 7) in LOCAL time so "every day/
    Saturday at 17:30" keeps landing on 17:30 local time across a DST
    transition, instead of drifting the way a fixed 24h/7-day timedelta
    would. Falls back to the plain millisecond interval otherwise. If the
    app was closed long enough to miss one or more occurrences, skips
    straight to the next one still in the future rather than firing a burst
    of catch-up reminders for every missed day/week."""
    now = datetime.now().astimezone()
    calendar = reminder.get("recurringCalendar")
    if calendar:
        step_days = 7 if calendar == "weekly" else 1
        base = datetime.fromisoformat(reminder["dueAtIso"])
        if base.tzinfo is None:
            base = base.astimezone()
        while True:
            base = base + timedelta(days=step_days)
            if base > now:
                return base
    recurring_ms = reminder.get("recurringMs") or 0
    return now + timedelta(milliseconds=recurring_ms)


def start_due_check_loop(on_due: Callable[[dict[str, Any]], bool], interval_s: float = 20.0) -> asyncio.Task[None]:
    """Ported from scheduler.ts's startDueCheckLoop. Polls workspace/
    schedule.json and calls on_due for every reminder whose time has
    passed. on_due returns whether it actually got delivered (e.g. there's
    a live chat session to inject it into) -- a reminder is only marked
    fired when that's true, so one that comes due while the app happens to
    be between sessions (or fully closed) stays pending and fires on the
    very next check instead of being silently dropped."""

    def _check() -> None:
        reminders = _load_reminders()
        now = datetime.now().astimezone()
        changed = False
        new_reminders: list[dict[str, Any]] = []
        for r in reminders:
            if r.get("fired"):
                continue
            due = datetime.fromisoformat(r["dueAtIso"])
            if due.tzinfo is None:
                due = due.astimezone()
            if due > now:
                continue
            try:
                delivered = on_due(r)
            except Exception as exc:
                log_event("engine", "due_check_on_due_failed", reminder_id=r.get("id"), error=str(exc))
                delivered = False
            if delivered:
                r["fired"] = True
                changed = True
                if r.get("recurringCalendar") or r.get("recurringMs"):
                    new_reminders.append({
                        "id": uuid.uuid4().hex,
                        "dueAtIso": _next_occurrence(r).isoformat(),
                        "note": r["note"],
                        "createdAtIso": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "fired": False,
                        "priority": r.get("priority"),
                        "recurringMs": r.get("recurringMs"),
                        "recurringCalendar": r.get("recurringCalendar"),
                        "kind": r.get("kind"),
                    })
        if new_reminders:
            reminders.extend(new_reminders)
            changed = True
        if changed:
            _save_reminders(reminders)

    def _check_safe() -> None:
        # Bug fix (2026-09-10): confirmed live elsewhere (chat_session.py's
        # watchdog loop) that a "while True" background loop with no
        # per-tick guard dies completely silently the moment ANYTHING
        # inside one tick raises -- _load_reminders()/date parsing here
        # aren't otherwise guarded (only on_due() itself was). A single
        # corrupted schedule.json entry must not cost every future
        # reminder check for the rest of the process's lifetime.
        try:
            _check()
        except Exception as exc:  # noqa: BLE001 -- must log, never let this tick die silently
            log_event("engine", "due_check_tick_failed", error=str(exc), error_type=type(exc).__name__)

    async def _loop() -> None:
        _check_safe()  # catch up on anything already overdue right away, don't wait a full interval
        while True:
            await asyncio.sleep(interval_s)
            _check_safe()

    log_event("engine", "due_check_loop_starting", interval_s=interval_s)
    return supervise("due_check", _loop)


def ensure_recurring_backup(note: str, interval_s: float = 60 * 60) -> None:
    """Ported from scheduler.ts's ensureRecurringBackup: seeds an hourly
    recurring "back up your memory" reminder exactly once, detected via the
    `kind` tag, so restarting the app never piles up duplicate recurring
    chains. Safe to call on every startup."""
    kind = "vault-backup-hourly"
    reminders = _load_reminders()
    if any(r.get("kind") == kind for r in reminders):
        log_event("engine", "ensure_recurring_backup_already_seeded")
        return
    log_event("engine", "ensure_recurring_backup_seeding", interval_s=interval_s)
    reminders.append({
        "id": uuid.uuid4().hex,
        "dueAtIso": (datetime.now(timezone.utc) + timedelta(seconds=interval_s)).isoformat().replace("+00:00", "Z"),
        "note": note,
        "createdAtIso": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fired": False,
        "recurringMs": interval_s * 1000,
        "kind": kind,
    })
    _save_reminders(reminders)


def _usage_instructions() -> str:
    return (
        "For ANY periodic/recurring task (checking something on a schedule, a daily/weekly routine) or a task "
        "tied to a recurring real-world date (a birthday, an anniversary), ALWAYS use schedule_reminder's own "
        "`recurring` (calendar-anchored: daily/weekly) or `recurring_every_minutes` (plain interval) "
        'parameter to make it self-sustaining on the backend. NEVER implement recurrence by writing "reschedule '
        'yourself for N from now" into the note text and relying on yourself to actually do that every time it '
        "fires -- confirmed in practice this silently stops forever the first time a turn fails, gets "
        "interrupted, or you simply don't follow through, with nothing to notice or recover it. A backend-"
        "scheduled recurrence cannot be skipped this way."
    )


PLUGIN = Plugin(
    name="scheduler",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "schedule_reminder",
            "Schedule a reminder/task for yourself (Caroline) to act on at a specific future date/time, "
            "surviving app restarts. When it comes due you will be prompted with the note text "
            "automatically, without the user saying anything -- use this for anything the user asks you to "
            "do 'at' or 'in' some time, or any follow-up you decide you should do later, including recurring "
            "periodic tasks (e.g. 'every morning check my email').\n\n"
            "priority controls what happens if this comes due while you're in the middle of something with "
            "the user: 'priority' (default) fires immediately regardless -- use this when the user needs it "
            "to happen at that exact time no matter what. 'background' only fires once there's no live "
            "back-and-forth going on -- deferred and retried later if the user is actively chatting when it "
            "comes due. Use 'background' for routine periodic chores that aren't time-critical.",
            {
                "due_at_iso": str, "note": str, "priority": str | None,
                "recurring": str | None, "recurring_every_minutes": float | None,
            }, schedule_reminder,
        ),
        PluginTool(
            "list_reminders",
            "List reminders that have been scheduled but haven't fired yet.",
            {}, list_reminders,
        ),
        PluginTool(
            "cancel_reminder",
            "Cancel a previously scheduled reminder by its id (see list_reminders).",
            {"id": str}, cancel_reminder,
        ),
    ],
)
