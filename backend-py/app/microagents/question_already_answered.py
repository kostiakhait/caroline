"""turn.question.already_answered -- per explicit instruction (2026-10-20),
after a real incident: Caroline asked the user "which Facebook account --
yours or mine?", the user answered ("for mine"), and three turns later,
asked to actually go ahead, she asked the EXACT SAME question again from
scratch -- the earlier answer sat right there in the dialogue the whole
time, unused. This agent checks a clarifying question about to be sent to
the user against the recent dialogue: has the user already answered this
same question (even if worded differently, even several turns back)? If
so, the caller suppresses the repeat question entirely and hands the
model the answer instead of showing the user a question they already
answered.

Client-side only ("Camerlengo is a universal server" -- see memory note of
the same name): a new, Caroline-specific agent, never deployed to
Camerlengo, run via app/microagent_client.py's local Runtime, same as
delivery_completeness.py/permission_identify.py.

Called from chat_session_mixins/dialogue_transcript.py, the same per-block
pipeline that already runs review_reply -> translate_text ->
_check_permission_guard() -> _check_delivery_completeness() on EVERY
outbound text block. Unlike delivery_completeness (which never blocks the
send, only corrects afterward), a positive result HERE suppresses the
message itself -- the user must never see a question they already
answered, not even once more before the correction lands."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

ALREADY_ANSWERED = "turn.question.already_answered"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "You are checking one message an AI assistant is about to send to its user, for exactly one thing: is "
    "this message asking the user a CLARIFYING QUESTION (which option, which account, yes/no, which one of "
    "several choices, any question the assistant needs an answer to before it can proceed) that the user has "
    "ALREADY ANSWERED earlier in the same dialogue -- even if that earlier answer used completely different "
    "words, even if it was several turns back, even if other topics came up in between?\n\n"
    "This is a real, confirmed failure this assistant has made before: it asked which of two accounts to "
    "use, the user answered plainly, and several turns later -- asked to actually proceed -- it asked the "
    "exact same question again from scratch, as if the answer had never been given.\n\n"
    "Only count it as already answered when the dialogue shows the user actually gave a real, substantive "
    "answer to THIS SPECIFIC question (or decision point) -- not just discussed the topic in general, not "
    "just acknowledged the question was asked, and not answered a DIFFERENT question that happens to look "
    "similar. If the user answered but then the assistant actually acted on it and the situation has "
    "genuinely moved on (the decision is no longer the live, pending one), that is NOT a match either -- "
    "this is about a stale repeat of a still-open question, not every question that superficially resembles "
    "an old one.\n\n"
    "A request for APPROVAL -- \"send it like this?\", \"shall I send / pay / publish / delete?\", a draft shown "
    "with \"reply yes\" -- is NOT answered by the user having made the underlying decision or supplied the "
    "content (\"tell her X\", \"I will take the child\"). It is answered only by the user explicitly approving that "
    "action or text AFTER it was shown to them (\"send it\", \"yes\", \"да\", \"отсылай\" in reply to that very "
    "draft) and only while it has not been acted on yet. Without such an approval, set already_answered=false.\n\n"
    "If the message is not really a question needing the user's input at all (a status update, a finished "
    "report, small talk), or it is a genuinely new question never answered before in this dialogue, set "
    "already_answered=false and leave answer empty.\n\n"
    "If it matches, set already_answered=true and write `answer` as one short, plain sentence stating "
    "exactly what the user already decided/said -- in your own words is fine, but it must accurately reflect "
    "what they actually said, never an inference or a guess beyond it.\n\n"
    "Decide using ONLY what is actually said in the dialogue below -- never guess or assume.\n\n"
    "RECENT DIALOGUE (oldest first, last line is the newest):\n{recent_dialogue}\n\n"
    "MESSAGE ABOUT TO BE SENT:\n---\n{final_text}\n---\n\n"
    "Reply now."
)


def prompt_already_answered(input: Any, context: Any) -> str:
    return _PROMPT.format(recent_dialogue=input.get("recent_dialogue") or "(empty)", final_text=input["final_text"])


def verify_already_answered(input: Any, output: Any) -> Optional[str]:
    if not isinstance(output.get("already_answered"), bool):
        return '"already_answered" must be a boolean'
    if output["already_answered"] and not (output.get("answer") or "").strip():
        return '"answer" must be a non-empty string when already_answered is true'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            ALREADY_ANSWERED, "1",
            "Checks whether a clarifying question about to be sent to the user was already answered earlier "
            "in the dialogue, even if worded differently -- so the caller can suppress the repeat and hand "
            "the model the answer instead of asking again.",
            input_schema=_object({"recent_dialogue": _STRING, "final_text": _STRING}, required=["final_text"]),
            output_schema=_object(
                {"already_answered": {"type": "boolean"}, "answer": _STRING}, required=["already_answered", "answer"],
            ),
            build_prompt=prompt_already_answered, verify=verify_already_answered,
        ),
    ]
