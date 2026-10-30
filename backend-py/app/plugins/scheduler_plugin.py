"""scheduler -- Caroline's reminders and recurring tasks.

A reminder is one record in workspace/schedule.json. A one-off reminder has no
`cron` field and is marked fired once delivered. A recurring one carries a
`cron` expression (6 fields: second minute hour day-of-month month day-of-week,
local time, Sunday = 0) and keeps the same record: after each delivery its
dueAtIso moves to the next matching time. Nothing is copied or re-created.

If the app was closed when a recurring time passed, it fires once on the next
check and then resumes at the next future match, without catching up.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.event_memory import forget_event_by_reminder_id, remember_event
from app.logging_setup import log_event
from app.plugins.loader import Plugin, PluginTool
from app.policies import follow_explicit_parameters_instruction
from app.session_context import get_tab_id
from app.task_supervisor import supervise
from app.workspace_dir import WORKSPACE_DIR

BACKUP_KIND = "vault-backup-hourly"
# Per explicit instruction (2026-10-15), part of the per-tab pseudo-session
# rework (see app/pseudo_tab.py): every reminder now carries the real tab_id
# of whichever session scheduled it, so it fires back into THAT tab's own
# pseudo-session (main.py's _on_reminder_due), not a single shared one. A
# reminder predating this field, or one scheduled by code that calls this
# tool outside any real session's own turn, falls back to the primary tab --
# the same tab the old shared background session always bridged to.
_DEFAULT_TAB_ID = "1"

# Per explicit instruction (2026-10-07), after a real incident: a reminder-
# driven turn used to always both (a) get live progress-narrator chatter
# while it ran and (b) be told, unconditionally, to "tell the user
# proactively" when done -- correct for something the user actually cares
# about (a mailbox check), completely wrong for Caroline's own routine
# internal upkeep (a memory backup), which produced unwanted "syncing..."
# commentary about internal mechanics nobody asked to hear. This is a
# GENERAL capability of every reminder, not a special case for one of
# them: "service" never narrates and never reports back (silent unless
# something is actually broken); "user" never narrates either, but still
# reports the outcome when done -- see main.py's _on_reminder_due for
# where this actually changes behavior.
EVENT_TYPES = ("service", "user")
_FIELD_RANGES = (("second", 0, 59), ("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 7))


def _schedule_path() -> Path:
    return Path(WORKSPACE_DIR) / "schedule.json"


def _load_reminders() -> list[dict[str, Any]]:
    path = _schedule_path()
    if not path.exists():
        return []
    try:
        reminders = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    # Per explicit instruction (2026-10-15): a reminder scheduled before tabId
    # existed defaults to the primary tab -- same in-memory default every
    # caller would otherwise need its own copy of; never rewrites the file, a
    # read must not silently mutate what's on disk.
    for r in reminders:
        r.setdefault("tabId", _DEFAULT_TAB_ID)
    return reminders


def _save_reminders(reminders: list[dict[str, Any]]) -> None:
    path = _schedule_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reminders, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _parse_field(text: str, lo: int, hi: int, name: str) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise ValueError(f"{name}: step must be at least 1")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = int(base)
            end = hi if step_text else start
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise ValueError(f"{name}: '{part}' is outside {lo}-{hi}")
        values.update(range(start, end + 1, step))
    if name == "weekday":
        values = {0 if v == 7 else v for v in values}
    return values


class _Cron:
    def __init__(self, expression: str) -> None:
        fields = expression.split()
        if len(fields) != 6:
            raise ValueError("cron needs exactly 6 fields: second minute hour day month weekday")
        parsed = [_parse_field(f, lo, hi, name) for f, (name, lo, hi) in zip(fields, _FIELD_RANGES)]
        self.seconds, self.minutes, self.hours, self.days, self.months, self.weekdays = parsed
        # Vixie-cron rule: if either day field is '*', both must match; otherwise either may.
        self.dom_star = fields[3].startswith("*")
        self.dow_star = fields[5].startswith("*")

    def _day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom_ok = d.day in self.days
        dow_ok = (d.isoweekday() % 7) in self.weekdays
        if self.dom_star or self.dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def next_after(self, after: datetime) -> datetime:
        local_after = after.astimezone().replace(tzinfo=None, microsecond=0)
        start = local_after + timedelta(seconds=1)
        for offset in range(366 * 8):
            d = start.date() + timedelta(days=offset)
            if not self._day_matches(d):
                continue
            for h in sorted(self.hours):
                for m in sorted(self.minutes):
                    for s in sorted(self.seconds):
                        candidate = datetime(d.year, d.month, d.day, h, m, s)
                        if candidate >= start:
                            return candidate.astimezone()
        raise ValueError("cron expression never matches")


def _cron_for_legacy(due_at_iso: str, recurring: str | None, every_minutes: float | None) -> str:
    """Maps the old recurring / recurring_every_minutes arguments onto cron."""
    if recurring in ("daily", "weekly"):
        due = datetime.fromisoformat(due_at_iso)
        if due.tzinfo is None:
            due = due.astimezone()
        local = due.astimezone()
        weekday = "*" if recurring == "daily" else str((local.isoweekday() % 7))
        return f"{local.second} {local.minute} {local.hour} * * {weekday}"
    if every_minutes:
        minutes = int(every_minutes)
        if minutes < 60 and 60 % minutes == 0:
            return f"0 */{minutes} * * * *"
        if minutes % 60 == 0 and 24 % (minutes // 60) == 0:
            return f"0 0 */{minutes // 60} * * *"
        raise ValueError(f"an every-{minutes}-minutes repeat cannot be expressed exactly; pass a cron expression instead")
    raise ValueError("recurring needs recurring_every_minutes or daily/weekly")


def _migrate_legacy_recurrence(reminders: list[dict[str, Any]]) -> bool:
    changed = False
    for r in reminders:
        if r.get("fired") or r.get("cron"):
            continue
        if not (r.get("recurringCalendar") or r.get("recurringMs")):
            continue
        cal = r.get("recurringCalendar")
        ms = r.get("recurringMs") or 0
        try:
            if cal in ("daily", "weekly"):
                r["cron"] = _cron_for_legacy(r["dueAtIso"], cal, None)
            else:
                r["cron"] = _cron_for_legacy(r["dueAtIso"], None, ms / 60_000)
        except ValueError as exc:
            log_event("engine", "reminder_migration_failed", reminder_id=r.get("id"), error=str(exc))
            continue
        r.pop("recurringCalendar", None)
        r.pop("recurringMs", None)
        r["dueAtIso"] = _Cron(r["cron"]).next_after(datetime.now().astimezone()).isoformat()
        changed = True
        log_event("engine", "reminder_migrated_to_cron", reminder_id=r.get("id"), cron=r["cron"])
    return changed


def _schedule_text(cron_text: str | None, due_iso: str | None) -> str:
    return f"cron '{cron_text}'" if cron_text else f"once at {due_iso}"


async def _advise(note: str, schedule: str, pending: list[dict[str, Any]]) -> dict[str, Any] | None:
    """reminder.advisor (app/microagents/reminder_advisor.py) on a new reminder against
    the pending ones; None when there are none or on any failure -- then the reminder
    is simply created, as before."""
    if not pending:
        return None
    from app.plugins.voice_api import _run_local_microagent  # local: voice_api imports the plugin loader
    from app.microagents.reminder_advisor import REMINDER_ADVISOR

    existing = [{"id": r["id"], "note": r.get("note") or "", "schedule": _schedule_text(r.get("cron"), r.get("dueAtIso"))} for r in pending]
    try:
        result = await _run_local_microagent(REMINDER_ADVISOR, "app.microagents.reminder_advisor", {"new": {"note": note, "schedule": schedule}, "existing": existing})
    except Exception as exc:  # noqa: BLE001 -- a failed check must not lose the user's reminder
        log_event("engine", "reminder_advisor_failed", error=str(exc)[:200], error_type=type(exc).__name__)
        return None
    if not result or result.get("status") != "ok":
        log_event("engine", "reminder_advisor_failed", error=(result or {}).get("error"))
        return None
    output = result.get("output") or {}
    log_event("engine", "reminder_advisor_done", decision=output.get("decision"), id=output.get("id"), why=output.get("why"))
    return output


async def schedule_reminder(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    reminders = _load_reminders()
    cron_text = args.get("cron")
    try:
        if not cron_text and (args.get("recurring") or args.get("recurring_every_minutes")):
            cron_text = _cron_for_legacy(args.get("due_at_iso") or datetime.now().astimezone().isoformat(), args.get("recurring"), args.get("recurring_every_minutes"))
        if cron_text:
            cron = _Cron(cron_text)
            due = args.get("due_at_iso")
            first = datetime.fromisoformat(due) if due else cron.next_after(datetime.now().astimezone())
            if first.tzinfo is None:
                first = first.astimezone()
            if due and first <= datetime.now().astimezone():
                first = cron.next_after(first)
            due_iso = first.isoformat()
        else:
            cron = None
            # Issue #5 (2026-10-24): checked before it is saved -- an unparseable
            # date used to be stored as given and then broke every due check.
            due_iso = datetime.fromisoformat(str(args["due_at_iso"])).isoformat()
    except (ValueError, TypeError) as exc:
        return {"text": f"Invalid schedule: {exc}", "is_error": True}
    event_type = args.get("event_type") or "user"
    if event_type not in EVENT_TYPES:
        return {"text": f'Invalid event_type "{event_type}" -- must be one of {", ".join(EVENT_TYPES)}.', "is_error": True}
    # Per explicit instruction (2026-10-29): an existing reminder on the same topic is
    # refined, not duplicated. The advisor's merge is handed back to the model, which
    # applies it with update_reminder (or corrects it); `separate` is for a reminder the
    # user really wants next to a similar one.
    if not args.get("separate"):
        pending = [r for r in reminders if not r.get("fired") and r.get("kind") != BACKUP_KIND]
        advice = await _advise(args["note"], _schedule_text(cron_text if cron else None, due_iso), pending)
        if advice and advice.get("decision") == "update":
            existing = next(r for r in pending if r["id"] == advice["id"])
            suggested_cron = (advice.get("cron") or "").strip() or None
            return {"text": (
                f"Not scheduled: reminder {existing['id']} already covers this topic "
                f"({_schedule_text(existing.get('cron'), existing.get('dueAtIso'))}): {existing.get('note')}\n\n"
                f"Refine it instead -- call update_reminder with id {existing['id']}. Suggested merged reminder"
                f"{f' (cron {suggested_cron!r})' if suggested_cron else ''}: {advice['note']}\n\n"
                "Check the suggestion before you apply it: fix anything it dropped or invented, and anything that "
                "contradicts the existing reminder's own rules (those win). Only if the user really wants a second, "
                "separate reminder on this topic, call schedule_reminder again with separate=true."
            )}
    reminder = {
        "id": uuid.uuid4().hex,
        "dueAtIso": due_iso,
        "note": args["note"],
        "createdAtIso": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fired": False,
        "priority": args.get("priority") or "priority",
        "cron": cron_text if cron else None,
        "eventType": event_type,
        "tabId": get_tab_id() or _DEFAULT_TAB_ID,
    }
    reminders.append(reminder)
    _save_reminders(reminders)
    # Per explicit instruction (2026-10-07): every scheduled reminder is also
    # a durable event-memory record, written automatically here -- not left
    # to Caroline's own discretion to log it a second time. See
    # app/event_memory.py's own doc comment for why this is separate from
    # working_memory.py's "events" category.
    remember_event(WORKSPACE_DIR, text=args["note"], due_at_iso=due_iso, source="schedule_reminder", reminder_id=reminder["id"])
    rec_text = f", repeats on cron '{cron_text}'" if cron_text else ""
    return {"text": f"Scheduled (id {reminder['id']}) for {due_iso} [{reminder['priority']}{rec_text}]: {args['note']}"}


async def update_reminder(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    """Refines a pending reminder in place: same id, same lastReport (2026-10-29)."""
    reminders = _load_reminders()
    reminder = next((r for r in reminders if r.get("id") == args["id"] and not r.get("fired")), None)
    if reminder is None:
        return {"text": f"No pending reminder with id {args['id']}.", "is_error": True}
    try:
        if args.get("cron"):
            cron = _Cron(args["cron"])
            reminder["cron"] = args["cron"]
            reminder["dueAtIso"] = cron.next_after(datetime.now().astimezone()).isoformat()
        if args.get("due_at_iso"):
            due = datetime.fromisoformat(str(args["due_at_iso"]))
            if due.tzinfo is None:
                due = due.astimezone()
            if not args.get("cron"):
                reminder["cron"] = None
            reminder["dueAtIso"] = due.isoformat()
    except (ValueError, TypeError) as exc:
        return {"text": f"Invalid schedule: {exc}", "is_error": True}
    if args.get("event_type"):
        if args["event_type"] not in EVENT_TYPES:
            return {"text": f'Invalid event_type "{args["event_type"]}" -- must be one of {", ".join(EVENT_TYPES)}.', "is_error": True}
        reminder["eventType"] = args["event_type"]
    if args.get("priority"):
        reminder["priority"] = args["priority"]
    if args.get("note"):
        reminder["note"] = args["note"]
    reminder["updatedAtIso"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _save_reminders(reminders)
    forget_event_by_reminder_id(WORKSPACE_DIR, reminder["id"])
    remember_event(WORKSPACE_DIR, text=reminder["note"], due_at_iso=reminder["dueAtIso"], source="update_reminder", reminder_id=reminder["id"])
    log_event("engine", "reminder_updated", reminder_id=reminder["id"], fields=sorted(k for k in ("note", "cron", "due_at_iso", "priority", "event_type") if args.get(k)))
    rec_text = f", repeats on cron '{reminder['cron']}'" if reminder.get("cron") else ""
    return {"text": f"Updated {reminder['id']}: next {reminder['dueAtIso']} [{reminder['priority']}{rec_text}]: {reminder['note']}"}


async def list_reminders(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    pending = [r for r in _load_reminders() if not r.get("fired")]
    return {"text": json.dumps(pending, indent=2, ensure_ascii=False) if pending else "No pending reminders."}


async def cancel_reminder(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    before = _load_reminders()
    after = [r for r in before if r.get("id") != args["id"]]
    _save_reminders(after)
    removed = len(after) < len(before)
    if removed:
        forget_event_by_reminder_id(WORKSPACE_DIR, args["id"])  # it never happened -- don't keep a record of it
    return {"text": f"Cancelled {args['id']}." if removed else f"No pending reminder with id {args['id']}."}


# Reminders already resumed in this process (see set_reminder_awaiting_retry).
_resumed_this_process: set[str] = set()


def set_reminder_awaiting_retry(reminder_id: str, waiting: bool) -> None:
    """Marks a reminder whose turn is waiting out a usage cap in its own
    session (app/pseudo_tab.py keeps that session alive and lets its 90s
    retry run), and unmarks it once a real reply got through. The mark is in
    schedule.json so that it survives a restart: the waiting session does
    not, and without the mark the firing would be lost -- its time has
    already moved on. After a restart the due-check loop fires a marked
    reminder once more. An activity that is not a reminder (a sub-agent's
    own pseudo-session) has no record here and is left alone."""
    reminders = _load_reminders()
    for r in reminders:
        if r.get("id") != reminder_id:
            continue
        if bool(r.get("awaitingRetry")) == waiting:
            return
        if waiting:
            r["awaitingRetry"] = datetime.now().astimezone().isoformat()
        else:
            r.pop("awaitingRetry", None)
        _save_reminders(reminders)
        log_event("engine", "reminder_awaiting_retry_set", reminder_id=reminder_id, waiting=waiting)
        return


LAST_REPORT_MAX_CHARS = 2000


def set_reminder_last_report(reminder_id: str, text: str) -> None:
    """Keeps what a reminder's firing last reported to the user (2026-10-29): every
    firing now starts a fresh session (main.py's _on_reminder_due), and this is all
    of the earlier firings it is given -- enough to tell what is new since then. An
    activity that is not a reminder has no record here and is left alone."""
    reminders = _load_reminders()
    for r in reminders:
        if r.get("id") != reminder_id:
            continue
        r["lastReport"] = {"atIso": datetime.now().astimezone().isoformat(timespec="seconds"), "text": text[:LAST_REPORT_MAX_CHARS]}
        _save_reminders(reminders)
        return


def start_due_check_loop(on_due: Callable[[dict[str, Any]], bool], interval_s: float = 20.0) -> asyncio.Task[None]:
    """Polls workspace/schedule.json and delivers every pending reminder whose
    time has passed. on_due returns whether it was actually delivered; an
    undelivered reminder stays pending and is retried on the next check.

    A reminder marked awaitingRetry (see set_reminder_awaiting_retry) is
    delivered once more, whatever its time, the first time this process sees
    it: after a restart its waiting session is gone. In a process where that
    session is still alive the delivery coalesces into it (main.py's
    _on_reminder_due) and nothing new runs."""

    def _check() -> None:
        reminders = _load_reminders()
        changed = _migrate_legacy_recurrence(reminders)
        now = datetime.now().astimezone()
        for r in reminders:
            resume = bool(r.get("awaitingRetry")) and r.get("id") not in _resumed_this_process
            if r.get("fired") and not resume:
                continue
            try:
                due = datetime.fromisoformat(str(r["dueAtIso"]))
                if due.tzinfo is None:
                    due = due.astimezone()
            except (KeyError, ValueError, TypeError, OverflowError) as exc:
                # Issue #5 (2026-10-24): one broken entry (an old one, saved before
                # dates were checked) is set aside and the others still fire --
                # it used to abort the whole check, every tick, forever.
                if not r.get("invalidDue"):
                    r["invalidDue"] = f"{type(exc).__name__}: {exc}"
                    changed = True
                    log_event("engine", "reminder_due_unparseable", reminder_id=r.get("id"), due=r.get("dueAtIso"), error=r["invalidDue"])
                continue
            due_by_time = due <= now and not r.get("fired")
            if not due_by_time and not resume:
                continue
            try:
                delivered = on_due(r)
            except Exception as exc:
                log_event("engine", "due_check_on_due_failed", reminder_id=r.get("id"), error=str(exc))
                delivered = False
            if not delivered:
                continue
            if resume:
                _resumed_this_process.add(r["id"])
                log_event("engine", "reminder_resumed_awaiting_retry", reminder_id=r.get("id"), awaiting_since=r.get("awaitingRetry"))
            if not due_by_time:
                continue
            changed = True
            if r.get("cron"):
                try:
                    r["dueAtIso"] = _Cron(r["cron"]).next_after(now).isoformat()
                except (ValueError, TypeError, OverflowError) as exc:
                    # Delivered already; a broken cron must not lose that (the save
                    # below) nor deliver it again every tick.
                    r["fired"] = True
                    r["invalidDue"] = f"{type(exc).__name__}: {exc}"
                    log_event("engine", "reminder_cron_unusable", reminder_id=r.get("id"), cron=r.get("cron"), error=r["invalidDue"])
            else:
                r["fired"] = True
        if changed:
            _save_reminders(reminders)

    def _check_safe() -> None:
        try:
            _check()
        except Exception as exc:  # noqa: BLE001 -- a bad entry must not stop future checks
            log_event("engine", "due_check_tick_failed", error=str(exc), error_type=type(exc).__name__)

    async def _loop() -> None:
        _check_safe()
        while True:
            await asyncio.sleep(interval_s)
            _check_safe()

    log_event("engine", "due_check_loop_starting", interval_s=interval_s)
    return supervise("due_check", _loop)


def _usage_instructions() -> str:
    return "\n\n".join((
        "For ANY periodic/recurring task (checking something on a schedule, a daily/weekly routine, a birthday or "
        "anniversary) pass a `cron` expression to schedule_reminder. It is ONE reminder that the backend keeps "
        "repeating on its own -- never write 'reschedule this for later' into the note and rely on yourself to do "
        "it, because that silently stops the first time a turn fails or gets interrupted. This is the ONLY "
        "correct way to check something again later, including a one-off 'wait N seconds/minutes and check back' "
        "-- you have no tool that lets you schedule your own wake-up or re-check yourself from inside your own "
        "current turn; schedule_reminder is what exists for exactly that.\n"
        "cron has 6 fields, local time: second minute hour day-of-month month day-of-week (Sunday = 0). "
        "Examples: '0 0 9 * * *' every day at 09:00; '0 30 17 * * 6' every Saturday at 17:30; '0 0 */2 * * *' every 2 hours.",
        "One reminder per topic. When the user adds to or changes something a reminder already does (another "
        "mailbox for the same report, a new point to watch, a different time), refine that reminder with "
        "update_reminder -- never add a second one next to it. schedule_reminder checks this itself: if an existing "
        "reminder covers the topic it schedules nothing and hands you a suggested merge for update_reminder.",
        follow_explicit_parameters_instruction(),
    ))


PLUGIN = Plugin(
    name="scheduler",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "schedule_reminder",
            "Schedule a reminder/task for yourself (Caroline) to act on, surviving app restarts. When it comes due "
            "you will be prompted with the note text automatically, without the user saying anything. Use due_at_iso "
            "for a one-off time. For a repeating task pass cron (6 fields: second minute hour day month weekday, local "
            "time, Sunday=0) instead -- the one reminder keeps repeating. priority controls what happens if it comes "
            "due mid-conversation: 'priority' (default) fires regardless; 'background' waits until there is no live "
            "back-and-forth -- use it for routine chores. event_type controls how it's delivered: 'user' (default) "
            "is for anything whose OUTCOME the user cares about (a mailbox check, a scheduled follow-up) -- no "
            "live progress chatter while it runs, but you still report the result when done. 'service' is for your "
            "own routine upkeep that doesn't concern the user at all (e.g. a memory/notes backup) -- also no "
            "progress chatter, AND stays completely silent afterward (reply [[NO_UPDATE]]) unless something is "
            "actually broken. If a pending reminder already covers the same topic, nothing is scheduled: you get "
            "its id and a suggested merge to apply with update_reminder. separate=true schedules it anyway -- only "
            "when the user really wants a second reminder on that topic.",
            {
                "due_at_iso": str | None, "note": str, "priority": str | None, "cron": str | None,
                "recurring": str | None, "recurring_every_minutes": float | None, "event_type": str | None,
                "separate": bool | None,
            }, schedule_reminder,
        ),
        PluginTool(
            "update_reminder",
            "Refine a pending reminder in place (see list_reminders for ids): a new note (give the WHOLE note, not "
            "just the change), a new cron or due_at_iso, priority, event_type. Only the fields you pass change; the "
            "reminder keeps its id and what it reported last time.",
            {
                "id": str, "note": str | None, "cron": str | None, "due_at_iso": str | None,
                "priority": str | None, "event_type": str | None,
            }, update_reminder,
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
