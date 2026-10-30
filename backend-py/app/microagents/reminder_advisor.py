"""reminder.advisor -- before a new reminder is scheduled, decides whether an
existing one already covers its topic (2026-10-29, explicit instruction: the
model must refine existing reminders when it can and create a new one only
when there is none on the same topic -- a second 2-hourly mail check had
appeared next to the first, doubling the work).

Client-side only: run through the local microagent ladder. Called by
scheduler_plugin.schedule_reminder; any failure there means "create"."""

from __future__ import annotations

import json
from typing import Any, List, Optional

from app.microagent_client import Microagent as M

REMINDER_ADVISOR = "reminder.advisor"

DECISIONS = ("create", "update", "separate")

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An assistant keeps reminders -- tasks it does for its user on a schedule (check a mailbox every 2 hours, "
    "remind about a birthday, follow up on a letter). It is about to add the NEW reminder below. Decide whether "
    "one of the EXISTING reminders already covers the same topic, so that the existing one should be refined "
    "instead of adding a second one.\n\n"
    "Same topic means the same job: the same mailbox or account to watch, the same report, the same person, case "
    "or deadline -- even if the wording, the extra details or the schedule differ. Different jobs are NOT the same "
    "topic just because they look alike: a different mailbox, a different person, a one-off \"remind me tomorrow\" "
    "next to a regular report.\n\n"
    "Answer with one decision:\n"
    "- \"create\": no existing reminder covers this topic.\n"
    "- \"update\": the existing reminder with this id covers it. Give the merged note: the existing note as it "
    "is, plus only what the new one genuinely adds (another mailbox, a new point to watch), with no duplicated "
    "lines. Where the two contradict each other (when to stay silent, how to report, which window to check), the "
    "existing note's rules win -- the user set them explicitly; drop the new one's conflicting part. Give the cron "
    "the merged reminder runs on: the existing reminder's cron, unchanged, unless the new one explicitly asks for "
    "a different time (6 fields: second minute hour day month weekday; empty for a one-off).\n"
    "- \"separate\": an existing reminder is close, but this one is a genuinely different job and should exist "
    "next to it; name that close reminder's id.\n\n"
    "NEW reminder:\n{new}\n\n"
    "EXISTING reminders:\n{existing}\n\n"
    'Reply with a JSON object: {{"decision": "create" | "update" | "separate", "id": "<existing id or empty>", '
    '"note": "<merged note, for update; else empty>", "cron": "<cron for update, or empty>", "why": "<one sentence>"}}'
)


def prompt_reminder_advisor(input: Any, context: Any) -> str:
    return _PROMPT.format(
        new=json.dumps(input.get("new") or {}, ensure_ascii=False, indent=1),
        existing=json.dumps(input.get("existing") or [], ensure_ascii=False, indent=1),
    )


def verify_reminder_advisor(input: Any, output: Any) -> Optional[str]:
    decision = output.get("decision")
    if decision not in DECISIONS:
        return f'"decision" must be one of {", ".join(DECISIONS)}'
    if decision == "create":
        return None
    ids = {r.get("id") for r in input.get("existing") or []}
    if output.get("id") not in ids:
        return f'"id" must be the id of one of the EXISTING reminders for "{decision}"'
    if decision == "update" and not str(output.get("note") or "").strip():
        return '"note" must be the merged note for "update"'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            REMINDER_ADVISOR, "1",
            "Decides whether a new reminder should refine an existing one on the same topic instead of being added.",
            input_schema=_object({
                "new": _object({"note": _STRING, "schedule": _STRING}),
                "existing": {"type": "array", "items": _object({"id": _STRING, "note": _STRING, "schedule": _STRING})},
            }),
            output_schema=_object(
                {"decision": _STRING, "id": _STRING, "note": _STRING, "cron": _STRING, "why": _STRING},
                required=["decision", "why"],
            ),
            build_prompt=prompt_reminder_advisor, verify=verify_reminder_advisor,
        ),
    ]
