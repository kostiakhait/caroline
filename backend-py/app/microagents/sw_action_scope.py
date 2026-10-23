"""sw.action.scope -- is this changing action part of the task? (2026-10-22,
explicit instruction: a checker for actions the task did not ask for.) Seen on
the sw test box the same day: asked to find a letter and answer its sender,
nano set out to file the letter into a "Projects/..." folder it created itself.

Asked before every tool call that changes something (see
small_model_engine._changes_something); a call outside the task is not made,
and the model is told so. Reading calls are never asked about.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

ACTION_SCOPE = "sw.action.scope"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI assistant is about to make a tool call that CHANGES something (creates, moves, deletes, writes, "
    "sends, marks, runs code...). Decide one thing: is this action part of the task the user gave -- either "
    "one of the actions the task asks for, or a step plainly needed to do one of them (writing a file the task "
    "asks to produce, running code to produce it, marking a letter read after reading it as asked)? An "
    "action the user did not ask for and the task does not need -- tidying up, filing, organizing, renaming, "
    "deleting, creating folders or extra things \"to be helpful\" -- is NOT in scope.\n\n"
    "TASK:\n{task}\n\n"
    "CALLS MADE SO FAR:\n{calls}\n\n"
    "THE CALL ABOUT TO BE MADE:\n{tool}({args})\n\n"
    'Reply with a JSON object: {{"in_scope": true or false, "why": "<one short sentence>"}}'
)


def prompt_action_scope(input: Any, context: Any) -> str:
    return _PROMPT.format(
        task=input.get("task") or "(not given)",
        calls="\n".join(input.get("calls") or []) or "(none)",
        tool=input.get("tool") or "",
        args=input.get("args") or "",
    )


def verify_action_scope(input: Any, output: Any) -> Optional[str]:
    if not isinstance(output.get("in_scope"), bool):
        return '"in_scope" must be true or false'
    if not str(output.get("why") or "").strip():
        return '"why" must say in one sentence why'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            ACTION_SCOPE, "1",
            "Decides whether a changing tool call is part of the user's task, or something nobody asked for.",
            input_schema=_object(
                {"task": _STRING, "tool": _STRING, "args": _STRING, "calls": {"type": "array", "items": _STRING}},
                required=["task", "tool", "args"],
            ),
            output_schema=_object({"in_scope": {"type": "boolean"}, "why": _STRING}),
            build_prompt=prompt_action_scope, verify=verify_action_scope,
        ),
    ]
