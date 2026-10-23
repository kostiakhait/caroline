"""sw.tool.effect -- does a tool change anything? (2026-10-22, explicit
instruction: no fixed keyword list -- "a straight road to a screw-up"; the
model decides, from the tool's own description.) Decides once per tool --
the answer is kept by tool name and description hash (see
small_model_engine._changes_something); a changing tool's calls are then
checked against the task by sw.action.scope.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

TOOL_EFFECT = "sw.tool.effect"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "Below is one tool an AI assistant can call. Decide: does calling it CHANGE anything in the user's world "
    "-- their files, mailboxes, messages, chats, contacts, documents, accounts, money, the computer and its "
    "windows, anything other people see -- or does it only read, look, search, list or compute? The "
    "assistant's own bookkeeping (its memory, its plans, its notes about its own work) is not the user's "
    "world. When a tool can do either, it changes something.\n\n"
    "TOOL: {name}\n"
    "DESCRIPTION: {description}\n"
    "PARAMETERS: {params}\n\n"
    'Reply with a JSON object: {{"changes": true or false, "why": "<one short sentence>"}}'
)


def prompt_tool_effect(input: Any, context: Any) -> str:
    return _PROMPT.format(name=input.get("name") or "", description=input.get("description") or "(none)",
                          params=input.get("params") or "(none)")


def verify_tool_effect(input: Any, output: Any) -> Optional[str]:
    if not isinstance(output.get("changes"), bool):
        return '"changes" must be true or false'
    if not str(output.get("why") or "").strip():
        return '"why" must say in one sentence why'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            TOOL_EFFECT, "1",
            "Decides from a tool's description whether calling it changes anything in the user's world.",
            input_schema=_object({"name": _STRING, "description": _STRING, "params": _STRING}, required=["name", "description"]),
            output_schema=_object({"changes": {"type": "boolean"}, "why": _STRING}),
            build_prompt=prompt_tool_effect, verify=verify_tool_effect,
        ),
    ]
