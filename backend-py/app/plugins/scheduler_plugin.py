"""scheduler -- ports backend/src/scheduler.ts's schedule_reminder/
list_reminders/cancel_reminder tools. Storage is workspace/schedule.json,
survives both a session restart and a full app restart.

NOT yet ported: the actual proactive-firing mechanism (startDueCheckLoop/
nextOccurrence/ensureRecurringBackup in the original) -- a background poll
loop that injects a due reminder as a new message into a live chat session
(hasLiveDialog-aware background/priority distinction). That's genuine
session/engine-level infrastructure (proactive-turn injection), Phase 3
scope, not a plugin concern -- reminders can be scheduled/listed/cancelled
today via these tools, but won't fire on their own until that's wired up.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.plugins.loader import Plugin, PluginTool
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
