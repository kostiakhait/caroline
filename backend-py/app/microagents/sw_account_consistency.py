"""sw.account.consistency -- which identity did the work use so far, and which
does this call use? (2026-10-23, explicit choice after the Ratatosk test M2.)
Seen on the sw test box: asked to read the latest incoming message and answer
it, nano opened the chat and read it as Caroline's own account ("self"), then
sent the answer as the user's account ("owner") -- one conversation, two
identities.

Asked before every changing call that follows other calls of the turn -- for
every messenger and for mail alike, with no list of tools or parameter names
(an identity is an `account`, a mailbox address, a `from`, a login: the agent
reads the calls). The agent only NAMES two things: the identity the earlier
calls acted as and the identity this call acts as. Whether they match is
decided by the code (small_model_engine._account_consistent); a mismatch is
excused only if sw.task.on_behalf says the task asks for the user's own name.
In the first version one agent also decided, and for Ratatosk and Telegram
wrote the right rule in its reason while answering "consistent" about a call
that switched from "self" to "owner"; in the second it also answered whether the
task asks for the other identity, and said yes three times in four.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

ACCOUNT_CONSISTENCY = "sw.account.consistency"

NOT_STATED = "not stated"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI assistant works in messengers, mailboxes and other services where it can act as different "
    "identities: its OWN account and the USER's account. How a call names the identity differs by service -- "
    "an `account` parameter, a mailbox address (with its password and servers), a `from` address, a login. "
    "Below: the task, the calls already made in this turn, and the call about to be made. Answer two "
    "things, each taken only from the text below, nothing guessed:\n"
    "1. earlier_identity -- the identity the earlier calls acted as in the SAME conversation, mailbox or "
    "piece of work as the new call, written exactly as the calls name it (for example self, owner, "
    "support@x.net). If the earlier calls used different identities, the one used by the call that opened or "
    f"read the conversation. If no earlier call is about the same work, or none names an identity: \"{NOT_STATED}\".\n"
    "2. this_identity -- the identity the NEW call acts as, written exactly as the call names it. If the call "
    f"names none: \"{NOT_STATED}\".\n\n"
    "TASK:\n{task}\n\n"
    "CALLS MADE SO FAR IN THIS TURN:\n{calls}\n\n"
    "THE CALL ABOUT TO BE MADE:\n{tool}({args})\n\n"
    'Reply with a JSON object: {{"earlier_identity": "...", "this_identity": "..."}}'
)


def prompt_account_consistency(input: Any, context: Any) -> str:
    return _PROMPT.format(
        task=input.get("task") or "(not given)",
        calls="\n".join(input.get("calls") or []) or "(none)",
        tool=input.get("tool") or "",
        args=input.get("args") or "",
    )


def verify_account_consistency(input: Any, output: Any) -> Optional[str]:
    for key in ("earlier_identity", "this_identity"):
        if not isinstance(output.get(key), str) or not output[key].strip():
            return f'"{key}" must be a non-empty string (or "{NOT_STATED}")'
    return None


def switched(output: dict[str, Any], args: str = "") -> bool:
    """True when both identities are stated and they differ. The new call's identity must
    literally occur in the call's arguments: on the W1 test the agent answered with the tool
    name ("run_python") for a call that names no identity, and a correct call was refused."""
    earlier = str(output["earlier_identity"]).strip().lower()
    this = str(output["this_identity"]).strip().lower()
    if this not in str(args).lower():
        return False
    return NOT_STATED not in (earlier, this) and earlier != this


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            ACCOUNT_CONSISTENCY, "3",
            "Names the identity (own or the user's account, mailbox, from) the earlier calls of a piece of work "
            "acted as and the identity a new call acts as.",
            input_schema=_object(
                {"task": _STRING, "tool": _STRING, "args": _STRING, "calls": {"type": "array", "items": _STRING}},
                required=["task", "tool", "args"],
            ),
            output_schema=_object({"earlier_identity": _STRING, "this_identity": _STRING}),
            build_prompt=prompt_account_consistency, verify=verify_account_consistency,
        ),
    ]
