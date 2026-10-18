"""turn.refusal.guard -- a microagent that decides whether the assistant's
own just-sent reply, in a real user turn, represents the assistant
VOLUNTARILY DECLINING to do what the user actually asked -- as opposed to
genuinely doing the task, asking a real clarifying question, reporting a
genuine completion, or hitting an infrastructure error.

Per explicit instruction (2026-10-12): Caroline's underlying model
sometimes refuses a legitimate user task on its own judgment (e.g.
declining to modify/deploy its own permission-control code unsupervised --
a real incident the same day). This is NOT something turn.permission.guard
covers or should cover -- that agent is scoped exclusively to missing
APPROVAL for a communication/financial action (see PermissionGuard.py's own
docstring); this agent is a separate, independent concern: did the
assistant just say no to the task itself. Kept as its OWN agent rather than
folded into permission.guard so neither agent's scope drifts into the
other's.

The caller (Caroline's own chat_session.py) runs this once, right after a
real user turn finishes with visible text, and on a confirmed decline
automatically retries the SAME task through a different engine (the SW-
subscription small-model path) -- a one-off hand-off for that task only,
not a persisted mode change. See that call site for the retry mechanics;
this agent only judges, it never acts."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

GUARD = "turn.refusal.guard"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


# Evolution-engine genome (2026-10-17): see PermissionGuard.py's own
# GUARD_RULES_TEXT comment for the split's rationale -- same mechanical,
# behavior-preserving pattern, applied here.
GUARD_RULES_TEXT = (
    "You are watching one specific reply an AI assistant just gave its user, checking for exactly one "
    "thing: did the assistant just DECLINE to do what the user actually asked, on its own judgment?\n\n"
    "Count it as a decline when the reply says, in substance, that the assistant won't do the requested "
    "task -- refusing, declining, holding a position against doing it, or explaining why it won't comply -- "
    "regardless of how politely or at how much length it's phrased, and regardless of whether it offers an "
    "alternative (e.g. 'I won't do X myself, but I can show you how' is still a decline of X).\n\n"
    "Do NOT count any of these as a decline:\n"
    "- The assistant actually doing the task, in whole or in part.\n"
    "- A genuine clarifying question needed before it CAN do the task.\n"
    "- Reporting that the task is already done, or that there's nothing new to do.\n"
    "- An infrastructure/API error, a content-safety block from the underlying provider, a timeout, or "
    "any other failure that isn't the assistant's own choice.\n"
    "- The assistant correctly declining because it is still waiting on the user's own missing approval "
    "for a communication-on-their-behalf or financial action (a different, already-handled case) -- only "
    "count it here if the decline is the assistant's OWN judgment call about the task itself, not a "
    "pending-approval gate.\n\n"
    "Decide using ONLY the task and the reply given below -- no other context. If it's a decline, give "
    "one short, plain-English sentence summarizing what was declined and why, for a log, not for the user.\n\n"
)


def _data_section(input: Any) -> str:
    return (
        f"TASK (what the user actually asked for): {input['task'] or '(not given)'}\n\n"
        f"ASSISTANT'S REPLY TO THAT TASK:\n{input['assistant_reply'] or '(empty)'}"
    )


def prompt_guard(input: Any, context: Any) -> str:
    return GUARD_RULES_TEXT + _data_section(input)


def verify_guard(input: Any, output: Any) -> Optional[str]:
    if output.get("declined") and not (output.get("reason") or "").strip():
        return '"reason" must be a non-empty string when "declined" is true'
    return None


def llm_agents() -> List[M.Agent]:
    return [
        M.Agent(
            GUARD, "1",
            "Decides whether the assistant's reply to a real user task is the assistant voluntarily "
            "declining to do that task, as opposed to doing it, asking a real clarifying question, or "
            "hitting an infra error.",
            input_schema=_object({"task": _STRING, "assistant_reply": _STRING}, required=["task", "assistant_reply"]),
            output_schema=_object({"declined": {"type": "boolean"}, "reason": _STRING}, required=["declined", "reason"]),
            build_prompt=prompt_guard, verify=verify_guard,
        ),
    ]
