"""The user's request, formalized before an sw turn starts.

Per explicit instruction (2026-10-22): a separate microagent turns the request
into a task -- who asks, what for, what exactly, by when -- and the cascade's
models and the verifier work from that; the user's own words go along as
additional data for clarification. Seen on the test box the same day: asked
to answer a sender "that the invoice is received and will be paid by Friday",
the strongest tier drafted "we plan to pay on [date]" -- "I don't have a date"
-- and the verifier approved it.

On nano (explicit choice). Any failure leaves the turn as it was before: the
request goes to the models in the user's own words only.
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.logging_setup import log_event

REQUEST_SPEC_MODEL = "openai/gpt-5-nano"
REQUEST_SPEC_REASONING = {"effort": "low"}

# The fields, in the order they are shown to the models. The labels are what
# the models and the verifier read.
FIELDS = (
    ("requester", "Requester"),
    ("goal", "Goal"),
    ("actions", "Actions to take"),
    ("targets", "Who or what it is about"),
    ("content", "Content"),
    ("outgoing", "Messages to send (to whom: exactly what it says)"),
    ("report", "What to tell the user"),
    ("dates", "Dates and deadlines"),
    ("deliverable", "What the user should end up with"),
    ("constraints", "Conditions and limits"),
)

_PROMPT = (
    "CRITICAL: a program parses your reply. Reply with one JSON object and nothing else.\n\n"
    "Below is a request a user gave their AI assistant, with the conversation before it. Turn the request "
    "into a precise task for the assistant. Fields (leave out a field the request says nothing about):\n"
    '- "requester": who asks (the user, or someone the user speaks for)\n'
    '- "goal": what the request is for, in one sentence\n'
    '- "actions": the concrete things to do, in order (list of short strings)\n'
    '- "targets": people, mailboxes, files, chats, items it concerns (list)\n'
    '- "content": what has to be written or produced -- the substance, with every value the user gave\n'
    '- "outgoing": each message to be sent to someone other than the user, as "<recipient>: <what that '
    'message says>" (list) -- only what is meant for that recipient\n'
    '- "report": what the assistant tells the user at the end (a summary, a finding, a confirmation) -- '
    "this goes to the user, never into a message to someone else\n"
    '- "dates": dates, deadlines, times the user named, exactly as named ("до пятницы", "15:00")\n'
    '- "deliverable": what the user should have when it is done\n'
    '- "constraints": conditions the user set (list)\n'
    "Rules: take everything from the request and the conversation, invent nothing. Copy names, numbers, "
    "dates, quoted wording and any value the user gave EXACTLY, in their original language. Resolve "
    "\"him\"/\"that mailbox\"/\"the same\" from the conversation when it says who or what is meant. Write the "
    "rest in the request's language.\n\n"
    "CONVERSATION BEFORE THE REQUEST:\n---\n{dialogue}\n---\n\n"
    "REQUEST:\n---\n{request}\n---\n\n"
    "Reply now."
)


def _render(spec: dict[str, Any]) -> str:
    lines = []
    for key, label in FIELDS:
        value = spec.get(key)
        if isinstance(value, list):
            value = "; ".join(str(v).strip() for v in value if str(v).strip())
        value = str(value or "").strip()
        if value:
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)


async def formalize(request: str, recent_dialogue_lines: list[str], tab_id: str | None = None) -> str | None:
    """The formalized task as text for the models, or None (nothing to add, or a failure)."""
    from app.small_model_engine import resolve_narration_prompt

    started = time.monotonic()
    prompt = _PROMPT.format(dialogue="\n".join(recent_dialogue_lines[-8:]) or "(nothing yet)", request=request)
    try:
        raw = (await resolve_narration_prompt(prompt, model=REQUEST_SPEC_MODEL, reasoning=REQUEST_SPEC_REASONING) or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        spec = json.loads(raw[start:end + 1]) if start >= 0 and end > start else None
    except Exception as exc:  # noqa: BLE001 -- the turn goes on with the user's words alone
        log_event("engine", "sw_request_spec_failed", tab_id=tab_id, error=str(exc)[:200], error_type=type(exc).__name__,
                  ms=int((time.monotonic() - started) * 1000))
        return None
    text = _render(spec) if isinstance(spec, dict) else ""
    log_event("engine", "sw_request_spec", tab_id=tab_id, ok=bool(text), ms=int((time.monotonic() - started) * 1000), spec=text[:1500])
    return text or None
