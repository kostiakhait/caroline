"""The user's request, formalized before an sw turn starts.

Per explicit instruction (2026-10-22): a separate microagent turns the request
into a task -- who asks, what for, what exactly, by when -- and the cascade's
models and the verifier work from that; the user's own words go along as
additional data for clarification. Seen on the test box the same day: asked
to answer a sender "that the invoice is received and will be paid by Friday",
the strongest tier drafted "we plan to pay on [date]" -- "I don't have a date"
-- and the verifier approved it.

The "report" field describes what to report, never the report itself (2026-11-01,
live akinakes chat secretary): asked to set a project's objective, nano wrote
"What to tell the user: Confirmed: Set the objective ..." before anything was
done. The model posted "Objective confirmed" without setting it, and
sw.content.grounding and the verifier, which check against this text as the
task, took the false claim for wording the user asked for.

Split into three microagents (2026-11-02, explicit instruction: "раздели
формализатор на 2-3 и каждому поставь более четкое задание, ответы
объединяй"), see app/microagents/sw_request_spec_parts.py. One nano call with
ten fields filled them in: "set the objective" became "identify the fields,
select sensors, create a formal document", plus an outgoing "your objective has
been captured verbatim". Now each part has a narrow task, every action,
condition and outgoing message must quote the request's words that ask for it
(checked by code), and the parts run in parallel on the microagent ladder. The
answers are merged into the same task text as before.

Any failure leaves the turn as it was before: a part that fails adds nothing,
and with no part at all the request goes to the models in the user's own words.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.logging_setup import log_event

# The fields, in the order they are shown to the models. The labels are what
# the models and the verifier read.
FIELDS = (
    ("requester", "Requester"),
    ("goal", "Goal"),
    ("actions", "Actions to take"),
    ("targets", "Who or what it is about"),
    ("content", "Content"),
    ("outgoing", "Messages to send (to whom: exactly what it says)"),
    ("report", "What to report to the user"),
    ("dates", "Dates and deadlines"),
    ("deliverable", "What the user should end up with"),
    ("constraints", "Conditions and limits"),
)


# Seen live 2026-11-06 (Ferret editor, SW engine on its cheapest rung): asked "Замени шарик кубиком", the formalizer's
# answer became a task that was its own instruction ("reply with one JSON object ... content, report, deliverable"), and
# the turn answered with that JSON without touching the project. Two guards, both for every platform: the task is
# thrown away when it is that, and a reply of that shape is never a finished answer.
_PROMPT_MARKERS = (
    "a program parses your reply",
    "reply with one json object",
    "below is a request a user gave their ai assistant",
    "conversation before the request",
    "reply with: {",
)
_SPEC_KEYS = ("requester", "goal", "actions", "targets", "content", "outgoing", "report", "dates", "deliverable", "constraints")


def looks_like_formalizer_output(text: str) -> bool:
    """True for the formalizer's own instruction or a JSON object of its fields (content/report/deliverable ...)."""
    import json
    import re

    body = (text or "").strip()
    if not body:
        return False
    low = body.lower()
    if any(marker in low for marker in _PROMPT_MARKERS):
        return True
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", body, re.DOTALL)
    candidate = fenced.group(1) if fenced else body
    if candidate.startswith("{"):
        try:
            data = json.loads(candidate)
        except ValueError:
            data = None
        if isinstance(data, dict) and len(data) >= 2 and set(data) <= set(_SPEC_KEYS):
            return True
        if data is None and sum(f'"{key}"' in candidate for key in _SPEC_KEYS) >= 3:
            return True
    return False


def _stems(text: str) -> set[str]:
    import re

    return {w[:5] for w in re.findall(r"\w+", (text or "").lower()) if len(w) >= 4}


def spec_problem(request: str, text: str) -> str | None:
    """Why the formalized task cannot be used as the task, or None. A task that is the formalizer's own instruction,
    asks for a JSON of its fields, or shares no word with the request has lost the person's request."""
    if looks_like_formalizer_output(text) or any(m in text.lower() for m in _PROMPT_MARKERS) or "deliverable" in text.lower() and "json" in text.lower():
        return "is the formalizer's own instruction or a JSON of its fields"
    wanted = _stems(request)
    if wanted and not (wanted & _stems(text)):
        return "has lost the request's words"
    return None


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


def merge(work: dict[str, Any] | None, outgoing: dict[str, Any] | None, outcome: dict[str, Any] | None) -> dict[str, Any]:
    """The three parts' answers as the one spec the fields above describe. A part that failed (None) adds nothing."""
    spec: dict[str, Any] = {}
    if work:
        spec.update({k: work.get(k) for k in ("requester", "goal", "targets", "dates")})
        spec["actions"] = [a.get("do") for a in work.get("actions") or []]
        spec["constraints"] = [c.get("condition") for c in work.get("constraints") or []]
    if outgoing:
        spec["outgoing"] = [f'{m.get("to")}: {m.get("says")}' for m in outgoing.get("messages") or []]
    if outcome:
        spec.update({k: outcome.get(k) for k in ("content", "report", "deliverable")})
    return spec


def _run_part(name: str, given: dict[str, Any], tab_id: str | None) -> dict[str, Any] | None:
    """One part on the local microagent ladder (blocking; run in a thread). None when it could not be done."""
    from app.microagent_client import build_local_runtime, new_budget
    from app.microagents import sw_request_spec_parts

    runtime = build_local_runtime()
    if runtime is None:
        return None
    for agent in sw_request_spec_parts.llm_agents():
        try:
            runtime.register(agent)
        except ValueError:
            pass
    result = runtime.run(name, given, runtime.new_context(budget=new_budget()))
    if (result or {}).get("status") != "ok":
        log_event("engine", "sw_request_spec_part_failed", tab_id=tab_id, part=name, error=(result or {}).get("error"))
        return None
    return (result or {}).get("output") or {}


async def formalize(request: str, recent_dialogue_lines: list[str], tab_id: str | None = None) -> str | None:
    """The formalized task as text for the models, or None (nothing to add, or a failure)."""
    from app.microagents import sw_request_spec_parts as parts

    started = time.monotonic()
    given = {"request": request, "dialogue": "\n".join(recent_dialogue_lines[-8:])}
    names = (parts.REQUEST_WORK, parts.REQUEST_OUTGOING, parts.REQUEST_OUTCOME)
    try:
        answers = await asyncio.gather(*(asyncio.to_thread(_run_part, name, given, tab_id) for name in names))
    except Exception as exc:  # noqa: BLE001 -- the turn goes on with the user's words alone
        log_event("engine", "sw_request_spec_failed", tab_id=tab_id, error=str(exc)[:200], error_type=type(exc).__name__,
                  ms=int((time.monotonic() - started) * 1000))
        return None
    text = _render(merge(*answers))
    problem = spec_problem(request, text) if text else None
    if problem:
        log_event("engine", "sw_request_spec_thrown_away", tab_id=tab_id, problem=problem, spec=text[:600])
        return None
    log_event("engine", "sw_request_spec", tab_id=tab_id, ok=bool(text), ms=int((time.monotonic() - started) * 1000),
              parts_failed=[n for n, a in zip(names, answers) if a is None], spec=text[:1500])
    return text or None
