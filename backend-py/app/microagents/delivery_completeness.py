"""turn.delivery.completeness -- per explicit instruction (2026-10-18),
after a real incident: Caroline narrated having shown the user a Chinese
email draft for approval, when the actual outbound message never contained
it -- the model's own belief about what it delivered diverged from what
was actually sent. This agent checks the ACTUAL final text about to reach
the user against what it itself claims or implies it is delivering (a
draft, specific wording, a figure, an attachment) and flags anything
claimed but not actually present.

Client-side only (2026-10-18, "Camerlengo is a universal server" -- see
memory note of the same name): a new, Caroline-specific agent, never
deployed to Camerlengo, run entirely via app/microagent_client.py's local
Runtime + the generic microagent:call primitive, same as
permission_identify.py/action_sanction.py.

Called from chat_session_mixins/dialogue_transcript.py, the same per-block
pipeline that already runs review_reply -> translate_text ->
_check_permission_guard() on EVERY outbound text block, right after. Never
blocks the send itself -- on an incomplete delivery, the caller
inject_proactive()s a corrective note describing what's missing, same
non-blocking pattern _on_permission_block_checked already uses for a
permission block."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

COMPLETENESS = "turn.delivery.completeness"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "You are checking one message an AI assistant is about to send to its user, for exactly one failure "
    "mode: the message CLAIMS or IMPLIES it is delivering something specific -- a draft's actual wording, a "
    "document, a quoted figure, an attachment, a concrete result -- that the text does not actually contain. "
    "This is a real, confirmed failure this assistant has made before: it believed it had shown the user a "
    "draft for approval, and never actually included it.\n\n"
    "Look for phrases like \"here is the draft\", \"I've written it as follows\", \"see above\", \"as shown "
    "below\", \"I'm sending this\", \"the email says\" -- anywhere the message points at content it is "
    "supposedly presenting RIGHT NOW. If the actual content it is pointing at is genuinely present in the "
    "same message, this is complete. If the message merely DESCRIBES having done something (\"I've prepared "
    "the email\", \"I sent it\") without claiming to show its exact content here, that is also complete -- "
    "this check is ONLY about a claimed-to-be-shown-here content that is actually missing, never about "
    "whether a description is detailed enough.\n\n"
    "A message that states a STATUS or asks the user for APPROVAL or a decision (\"the email is not sent yet, it is "
    "waiting for your yes\", \"tell me when to send it\") does not claim to show any content right now: it is "
    "complete, even when a draft is being discussed in the dialogue. Flag only a message that points at content "
    "it is presenting in itself (\"below\", \"here is\", \"the text:\" followed by nothing) and does not contain it.\n\n"
    "Use RECENT DIALOGUE only for context (what was asked for, what the assistant said earlier it would "
    "show) -- judge completeness against the MESSAGE itself, not against the dialogue's own earlier promises "
    "in isolation.\n\n"
    "If everything the message claims to deliver is actually present, set complete=true and leave missing "
    "empty. If something claimed is genuinely missing, set complete=false and describe in one short, plain "
    "sentence what is missing (e.g. \"the message says the draft is shown below but no draft text follows\") "
    "-- this will be used to tell the assistant to actually include it.\n\n"
    "RECENT DIALOGUE (oldest first, last line is the newest):\n{recent_dialogue}\n\n"
    "MESSAGE ABOUT TO BE SENT:\n---\n{final_text}\n---\n\n"
    "Reply now."
)


def prompt_completeness(input: Any, context: Any) -> str:
    return _PROMPT.format(recent_dialogue=input.get("recent_dialogue") or "(empty)", final_text=input["final_text"])


def verify_completeness(input: Any, output: Any) -> Optional[str]:
    if not isinstance(output.get("complete"), bool):
        return '"complete" must be a boolean'
    if not output["complete"] and not (output.get("missing") or "").strip():
        return '"missing" must be a non-empty string when complete is false'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            COMPLETENESS, "1",
            "Checks whether a message about to be sent actually contains the specific content it claims or "
            "implies it is delivering right now (a draft's wording, a figure, an attachment) -- never whether "
            "a mere description is detailed enough.",
            input_schema=_object({"recent_dialogue": _STRING, "final_text": _STRING}, required=["final_text"]),
            output_schema=_object(
                {"complete": {"type": "boolean"}, "missing": _STRING}, required=["complete", "missing"],
            ),
            build_prompt=prompt_completeness, verify=verify_completeness,
        ),
    ]
