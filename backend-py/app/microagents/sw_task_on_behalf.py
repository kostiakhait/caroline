"""sw.task.on_behalf -- does the task say to act in the USER's own name? (2026-10-23.)
Asked only when sw.account.consistency found a call acting as another identity
than the work was read with (see small_model_engine._account_consistent): the
one reason such a switch is right is that the task itself asks to write as the
user. Split out of the consistency agent, where this question, asked together
with naming the identities, came back "yes" three times in four on a task that
merely mentioned a mailbox.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

TASK_ON_BEHALF = "sw.task.on_behalf"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "Read the task below. Does it say that something must be written, sent or done IN THE USER'S OWN NAME -- "
    "as the user, from the user's own account, on the user's behalf, \"from me\" -- as opposed to merely naming "
    "a mailbox, a chat or an account to work with? A task that only says where to read or which conversation "
    "to answer does NOT ask for the user's own name.\n\n"
    "TASK:\n{task}\n\n"
    'Reply with a JSON object: {{"in_users_name": true or false, "why": "<one short sentence quoting the words '
    'of the task that say so; empty when false>"}}'
)


def prompt_task_on_behalf(input: Any, context: Any) -> str:
    return _PROMPT.format(task=input.get("task") or "(not given)")


def verify_task_on_behalf(input: Any, output: Any) -> Optional[str]:
    if not isinstance(output.get("in_users_name"), bool):
        return '"in_users_name" must be true or false'
    if output["in_users_name"] and not str(output.get("why") or "").strip():
        return 'when true, "why" must quote the words of the task'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            TASK_ON_BEHALF, "1",
            "Decides whether a task says to act in the user's own name, rather than only naming an account to work with.",
            input_schema=_object({"task": _STRING}),
            output_schema=_object({"in_users_name": {"type": "boolean"}, "why": _STRING}, required=["in_users_name"]),
            build_prompt=prompt_task_on_behalf, verify=verify_task_on_behalf,
        ),
    ]
