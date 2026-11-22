"""reminder.extract -- does a message or a letter contain something that deserves a reminder of its own? (2026-11-20,
owner's order: significant reminders must be set automatically from the correspondence and from his dialogue with
Caroline, and an event must never be missed -- a session with a therapist at 12:00 was not confirmed and not reminded.)

Judges only what the text says (an appointment, a deadline, a promise with a date, a call, a delivery, a payment due);
scheduling, de-duplication against existing reminders (reminder.advisor) and the one-line notice are code in
app/auto_reminders.py. Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from app.microagent_client import Microagent as M

REMINDER_EXTRACT = "reminder.extract"
MAX_ITEMS = 3

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI secretary reads what her owner wrote to her, and letters and messages that reached him. Decide whether the "
    "text below contains SIGNIFICANT dated things the owner must not miss, and for each give a reminder.\n"
    "Significant: an appointment or session (doctor, therapist, court, school meeting), a meeting or call with a time, "
    "a deadline (a form, a payment, a filing, a visa or biometrics date), a delivery or flight, something the owner "
    "promised to do by a date, something he told the secretary to remind him of. NOT significant: newsletters, "
    "marketing, receipts, security codes, job alerts, general chatter, anything without a concrete date or time, "
    "anything already in the past.\n"
    "For each item give: \"note\" -- what to remind the owner of, in one clear sentence in the language of the text, "
    "with who/where/the link or phone if the text has it; \"remind_at_iso\" -- when to remind him, with the UTC offset "
    "of the owner's time zone: for a timed event 45 minutes before it, for a deadline on a date the morning of the "
    "previous day at 09:00, for a flight 3 hours before; it must be in the future; \"event\" -- the event's own date "
    "and time in plain words.\n"
    "At most " + str(MAX_ITEMS) + " items; an empty list when nothing is significant (the usual case).\n\n"
    "NOW (the owner's local time): {now}\nSOURCE: {source}\n\nTEXT:\n---\n{text}\n---\n\n"
    'Reply with a JSON object: {{"reminders": [{{"note": "...", "remind_at_iso": "...", "event": "..."}}]}}'
)


def prompt_reminder_extract(input: Any, context: Any) -> str:
    return _PROMPT.format(now=input.get("now") or "", source=input.get("source") or "", text=(input.get("text") or "")[:6000])


def verify_reminder_extract(input: Any, output: Any) -> Optional[str]:
    items = output.get("reminders")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        return f'"reminders" must be a list of at most {MAX_ITEMS} items'
    now = None
    try:
        now = datetime.fromisoformat(str(input.get("now")))
    except ValueError:
        pass
    for item in items:
        if not isinstance(item, dict) or not str(item.get("note") or "").strip() or not str(item.get("event") or "").strip():
            return 'every reminder needs "note" and "event"'
        try:
            when = datetime.fromisoformat(str(item.get("remind_at_iso")))
        except ValueError:
            return '"remind_at_iso" must be an ISO 8601 time with a UTC offset'
        if when.tzinfo is None:
            return '"remind_at_iso" must carry a UTC offset'
        if now is not None and now.tzinfo is not None and when <= now:
            return '"remind_at_iso" must be in the future'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            REMINDER_EXTRACT, "1",
            "Finds significant dated events and deadlines in a message or letter and proposes reminders for them.",
            input_schema=_object({"text": _STRING, "source": _STRING, "now": _STRING}, required=["text"]),
            output_schema=_object({"reminders": {"type": "array", "items": {"type": "object"}}}),
            build_prompt=prompt_reminder_extract, verify=verify_reminder_extract,
        ),
    ]
