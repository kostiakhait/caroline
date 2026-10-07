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
from app.task_supervisor import supervise
from app.workspace_dir import WORKSPACE_DIR

BACKUP_KIND = "vault-backup-hourly"
_FIELD_RANGES = (("second", 0, 59), ("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 7))


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


async def schedule_reminder(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    reminders = _load_reminders()
    cron_text = args.get("cron")
    if not cron_text and (args.get("recurring") or args.get("recurring_every_minutes")):
        cron_text = _cron_for_legacy(args.get("due_at_iso") or datetime.now().astimezone().isoformat(), args.get("recurring"), args.get("recurring_every_minutes"))
    try:
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
            due_iso = args["due_at_iso"]
    except ValueError as exc:
        return {"text": f"Invalid schedule: {exc}", "is_error": True}
    reminder = {
        "id": uuid.uuid4().hex,
        "dueAtIso": due_iso,
        "note": args["note"],
        "createdAtIso": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fired": False,
        "priority": args.get("priority") or "priority",
        "cron": cron_text if cron else None,
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


def start_due_check_loop(on_due: Callable[[dict[str, Any]], bool], interval_s: float = 20.0) -> asyncio.Task[None]:
    """Polls workspace/schedule.json and delivers every pending reminder whose
    time has passed. on_due returns whether it was actually delivered; an
    undelivered reminder stays pending and is retried on the next check."""

    def _check() -> None:
        reminders = _load_reminders()
        changed = _migrate_legacy_recurrence(reminders)
        now = datetime.now().astimezone()
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
            if not delivered:
                continue
            changed = True
            if r.get("cron"):
                r["dueAtIso"] = _Cron(r["cron"]).next_after(now).isoformat()
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


def ensure_recurring_backup(note: str) -> None:
    """Seeds the hourly memory-backup reminder once; detected by its kind tag."""
    reminders = _load_reminders()
    if any(r.get("kind") == BACKUP_KIND for r in reminders):
        log_event("engine", "ensure_recurring_backup_already_seeded")
        return
    cron = "0 0 * * * *"
    log_event("engine", "ensure_recurring_backup_seeding", cron=cron)
    reminders.append({
        "id": uuid.uuid4().hex,
        "dueAtIso": _Cron(cron).next_after(datetime.now().astimezone()).isoformat(),
        "note": note,
        "createdAtIso": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fired": False,
        "cron": cron,
        "kind": BACKUP_KIND,
    })
    _save_reminders(reminders)


def _usage_instructions() -> str:
    return "\n\n".join((
        "For ANY periodic/recurring task (checking something on a schedule, a daily/weekly routine, a birthday or "
        "anniversary) pass a `cron` expression to schedule_reminder. It is ONE reminder that the backend keeps "
        "repeating on its own -- never write 'reschedule this for later' into the note and rely on yourself to do "
        "it, because that silently stops the first time a turn fails or gets interrupted.\n"
        "cron has 6 fields, local time: second minute hour day-of-month month day-of-week (Sunday = 0). "
        "Examples: '0 0 9 * * *' every day at 09:00; '0 30 17 * * 6' every Saturday at 17:30; '0 0 */2 * * *' every 2 hours.",
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
            "back-and-forth -- use it for routine chores.",
            {
                "due_at_iso": str | None, "note": str, "priority": str | None, "cron": str | None,
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
