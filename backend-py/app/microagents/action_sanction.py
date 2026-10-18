"""action.sanction -- step 3 of the action-boundary tracking system (per
explicit instruction, 2026-10-18): after EVERY real user message, decide
whether that message grants or revokes sanction for any of the tab's
currently open actions (app/action_registry.py). Pure judgment over one
message against a short, explicit list -- deliberately NOT given the whole
dialogue (every open action's own `summary` already carries whatever
context matters; re-reading the full dialogue here would just re-litigate
what turn.permission.identify already settled when each action was opened).

Client-side only (2026-10-18, "Camerlengo is a universal server" -- see
memory note of the same name): this is a new, Caroline-specific agent, so
it is never deployed to Camerlengo at all, run entirely via
app/microagent_client.py's local Runtime + the generic microagent:call
primitive, same as permission_identify.py.

Called from chat_session_mixins/permission_guard.py's own
_check_action_sanction (submit()'s real-user-message path, same site
_check_permission_guard() already runs from), and SKIPPED ENTIRELY when
the tab's own ActionRegistry is empty -- no cost when nothing is pending,
matching the fail-cheap philosophy already used throughout this codebase's
other microagent checks."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

SANCTION = "action.sanction"

_STRING = {"type": "string"}
_DECISIONS = ("approved", "revoked", "unchanged")


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "You are watching one real message a user just sent to their AI assistant, checking whether it changes "
    "the approval status of any of the assistant's currently OPEN, bounded operations listed below (each one "
    "already identified earlier, e.g. \"send the Four Faith renewal email to Jessica Zhou\").\n\n"
    "For EACH open action listed, decide exactly one of:\n"
    "- \"approved\": this message clearly gives permission for THIS specific action to proceed (an explicit "
    "yes/go-ahead/confirmation that plainly covers it -- not a vague general agreeableness about the "
    "conversation as a whole).\n"
    "- \"revoked\": this message clearly withdraws or cancels permission for THIS specific action (a no, a "
    "stop, a correction that cancels it, a request to wait).\n"
    "- \"unchanged\": this message says nothing that bears on this specific action's approval status one way "
    "or the other -- the overwhelmingly common case for any message that isn't directly about it.\n\n"
    "Judge each action independently and strictly by its own summary -- a message approving one action never "
    "automatically approves a different one, even a similar-looking one. Never infer approval from silence or "
    "from the user simply continuing the conversation about something else.\n\n"
    "USER MESSAGE JUST RECEIVED:\n---\n{message}\n---\n\n"
    "OPEN ACTIONS:\n{open_actions_block}\n\n"
    "Reply now."
)


def _open_actions_block(open_actions: Any) -> str:
    if not open_actions:
        return "(none)"
    return "\n".join(f"- id: {a['id']} -- summary: {a['summary']}" for a in open_actions)


def prompt_sanction(input: Any, context: Any) -> str:
    return _PROMPT.format(message=input["message"] or "(empty)", open_actions_block=_open_actions_block(input.get("open_actions")))


def verify_sanction(input: Any, output: Any) -> Optional[str]:
    decisions = output.get("decisions")
    if not isinstance(decisions, list):
        return '"decisions" must be a list'
    known_ids = {a["id"] for a in (input.get("open_actions") or [])}
    seen_ids: set = set()
    for entry in decisions:
        if not isinstance(entry, dict):
            return "every entry in \"decisions\" must be an object"
        action_id = entry.get("action_id")
        if action_id not in known_ids:
            return f'"action_id" {action_id!r} is not one of the given open_actions ids'
        if action_id in seen_ids:
            return f'"action_id" {action_id!r} appears more than once in "decisions"'
        seen_ids.add(action_id)
        if entry.get("decision") not in _DECISIONS:
            return f'"decision" must be one of {_DECISIONS}'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            SANCTION, "1",
            "Decides, from one real user message, whether any of the tab's currently open bounded actions "
            "just had their approval granted, revoked, or left unchanged.",
            input_schema=_object(
                {"message": _STRING, "open_actions": {"type": "array", "items": {"type": "object"}}},
                required=["message"],
            ),
            output_schema=_object(
                {"decisions": {"type": "array", "items": {"type": "object", "properties": {
                    "action_id": _STRING, "decision": {"type": "string", "enum": list(_DECISIONS)},
                }}}},
                required=["decisions"],
            ),
            build_prompt=prompt_sanction, verify=verify_sanction,
        ),
    ]
