"""turn.reply.expected -- a microagent that decides, from one piece of
outbound text Caroline (the Camerlengo-backed AI assistant) just sent on a
given channel, whether a reply is actually expected.

Per explicit instruction (2026-10-12): after Caroline sends ANY message
(email or messenger), the caller (Caroline's own chat_session.py) asks this
agent whether that message calls for a reply at all -- a question, a
request, anything that leaves the ball in the other side's court -- versus
a pure FYI/notification/acknowledgement that nobody is expected to answer.
When a reply is expected, the caller schedules its own tapering follow-up
checks of that channel; this agent has no opinion on scheduling, only on
whether watching for a reply is worth doing in the first place.

Same shape as PermissionGuard.py (turn.permission.guard) and
RefusalGuard.py (turn.refusal.guard) -- registered into the same shared
Runtime from Api2ReplyCommands.py, its own command (`reply:replyExpected`),
its own billing label."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

EXPECTED = "turn.reply.expected"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


# Evolution-engine genome (2026-10-17): see PermissionGuard.py's own
# GUARD_RULES_TEXT comment for the split's rationale.
EXPECTED_RULES_TEXT = (
    "An AI assistant just sent the message below, on the channel given, on its user's behalf. Decide "
    "whether a REPLY is actually expected from the other side -- not just technically possible, but "
    "something a reasonable person would feel is now awaited.\n\n"
    "A reply IS expected when the message asks a question, makes a request, proposes something needing "
    "a yes/no/choice, or otherwise leaves something for the other side to answer or act on.\n\n"
    "A reply is NOT expected when the message is a pure notification, acknowledgement, FYI, confirmation "
    "of something already done, a one-way update, or otherwise plainly closes the exchange rather than "
    "opening it.\n\n"
    "Judge from the message's own content and the task it serves -- never assume a reply is expected by "
    "default just because a message was sent to a person.\n\n"
)


def _data_section(input: Any) -> str:
    return (
        f"CHANNEL: {input['channel'] or '(not given)'}\n\n"
        f"TASK THIS MESSAGE SERVES: {input['task'] or '(not given)'}\n\n"
        f"MESSAGE SENT:\n{input['sent_message'] or '(empty)'}"
    )


def prompt_expected(input: Any, context: Any) -> str:
    return EXPECTED_RULES_TEXT + _data_section(input)


def verify_expected(input: Any, output: Any) -> Optional[str]:
    if output.get("needs_reply") and not (output.get("reason") or "").strip():
        return '"reason" must be a non-empty string when "needs_reply" is true'
    return None


def llm_agents() -> List[M.Agent]:
    return [
        M.Agent(
            EXPECTED, "1",
            "Decides, from one outbound message and the channel it went out on, whether a reply is actually "
            "expected -- a question/request left open versus a pure FYI nobody need answer.",
            input_schema=_object({"task": _STRING, "channel": _STRING, "sent_message": _STRING}, required=["task", "channel", "sent_message"]),
            output_schema=_object({"needs_reply": {"type": "boolean"}, "reason": _STRING}, required=["needs_reply", "reason"]),
            build_prompt=prompt_expected, verify=verify_expected,
        ),
    ]
