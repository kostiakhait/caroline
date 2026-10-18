"""turn.action.summary -- a microagent that translates one about-to-run tool
call (name + arguments) into a single short, plain sentence a human (or
another microagent) can read, without needing to know what the tool actually
does.

Per explicit instruction (2026-10-15), after a real incident: Caroline sent a
Telegram message AS the user (account="owner") to a business contact with no
approval for that specific identity choice -- turn.permission.guard's own
instructions already covered this exactly ("a reply sent as if it came from
the user still needs their specific go-ahead"), but that agent reads ONLY the
recent dialogue text, never a tool call's actual arguments, and the model's
own visible text before the three sends ("Отправляю брошюру всем троим") said
nothing about which account each one would use. The guard had nothing to see.

Per explicit instruction, giving permissionGuard itself direct access to raw
tool schemas/arguments was rejected as too heavy and expensive for what is a
cheap, narrow judgment call today. This agent is the deliberately separate,
cheap translation step instead: it sees exactly one tool call and says, in
one sentence, what is about to happen in plain words -- "Caroline is about to
send a Telegram message from Konstantin's own account to chat 507712606: ...".
That sentence is then spliced into the dialogue permissionGuard already reads
(see chat_session.py's own _check_permission_guard(extra_context=...)) --
permissionGuard's own prompt/judgment is completely unchanged; it simply now
gets to see, in the one form it already understands, a fact it was
structurally blind to before.

This agent does no judgment of its own -- it never decides whether approval
is needed, only describes what the call is. Keep it that way: a translation
step that also tries to judge risk would duplicate (and could disagree with)
permissionGuard's own single source of truth for that question."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

SUMMARY = "turn.action.summary"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


# Evolution-engine genome (2026-10-17): see PermissionGuard.py's own
# GUARD_RULES_TEXT comment for the split's rationale.
SUMMARY_RULES_TEXT = (
    "An AI assistant named Caroline is about to call one tool. Describe, in ONE short plain sentence, what "
    "this specific call will actually do in the real world -- who or what it will reach, and under which "
    "identity/account if the arguments name one. Write it as a plain statement of fact about to happen "
    "('Caroline is about to ...'), not a question, not a judgment of whether it's okay.\n\n"
    "Pay special attention to, and always name explicitly when present in the arguments: (1) which "
    "ACCOUNT or IDENTITY the action goes out under, especially a value like \"owner\"/\"self\" or a named "
    "person -- say in plain words whose name/account this is (e.g. \"from the user's own account\", "
    "\"under Caroline's own identity\"), never just repeat the raw field value; (2) WHO or WHAT it reaches "
    "(a recipient, a contact, a chat, an address, a URL); (3) any money amount, currency or payment "
    "destination; (4) the real substance of what is being said or done, briefly, not just the action verb.\n\n"
    "If the call is not a communication or a payment at all (reading, searching, listing, checking status), "
    "still describe it factually in one sentence the same way -- do not add weight or caution it doesn't "
    "need.\n\n"
    "Reply with exactly that one sentence and nothing else: no preamble, no quotes around it, no comment on "
    "whether it should proceed.\n\n"
)


def _data_section(input: Any) -> str:
    return f"TOOL NAME: {input['tool_name']}\n\nARGUMENTS (as given to the tool, verbatim):\n{input['args_json']}"


def prompt_summary(input: Any, context: Any) -> str:
    return SUMMARY_RULES_TEXT + _data_section(input)


def verify_summary(input: Any, output: Any) -> Optional[str]:
    text = (output.get("summary") or "").strip()
    if not text:
        return '"summary" must be a non-empty string'
    if len(text) > 500:
        return '"summary" must be one short sentence (under 500 characters)'
    return None


def llm_agents() -> List[M.Agent]:
    return [
        M.Agent(
            SUMMARY, "1",
            "Translates one tool call (name + arguments) into a single plain-language sentence describing what "
            "it will actually do -- for turn.permission.guard, which never sees raw tool calls itself.",
            input_schema=_object({"tool_name": _STRING, "args_json": _STRING}, required=["tool_name", "args_json"]),
            output_schema=_object({"summary": _STRING}, required=["summary"]),
            build_prompt=prompt_summary, verify=verify_summary, ladder=M.LADDER,
        ),
    ]
