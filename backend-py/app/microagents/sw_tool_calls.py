"""sw.tools.calls -- step 2 of the tool advisor (see sw_tool_groups.py): for
ONE tool group -- the most likely one not suggested yet in this task -- the
concrete calls to make now, and which of them are independent of each other
(those run in parallel, explicit instruction 2026-10-22).

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

TOOL_CALLS = "sw.tools.calls"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI assistant is doing a task with tools and has not finished it. Below: the task, what is still "
    "missing, the calls it made so far with their results, and the tools of one group. Say which of these "
    "tools to call now, with which arguments (only the ones that matter, values taken from the task and the "
    "results so far -- never invent them), to get what is missing. Mark a call independent when it does not "
    "need the result of another call in your list.\n\n"
    "TASK:\n{task}\n\n"
    "WHAT IS STILL MISSING:\n{gap}\n\n"
    "CALLS SO FAR:\n{calls}\n\n"
    "TOOLS OF THE GROUP \"{group}\":\n{tools}\n\n"
    'Reply with a JSON object: {{"calls": [{{"tool": "<tool name exactly as listed>", "args": "<the arguments '
    'that matter, briefly>", "why": "<one short sentence>", "independent": true or false}}, ...]}}'
)


def prompt_tool_calls(input: Any, context: Any) -> str:
    tools = "\n".join(f"- {t.get('name')}: {t.get('description')}" for t in (input.get("tools") or [])) or "(none)"
    return _PROMPT.format(
        task=input.get("task") or "(not given)",
        gap=input.get("gap") or "(not given)",
        calls="\n".join(input.get("calls") or []) or "(none)",
        group=input.get("group") or "",
        tools=tools,
    )


def verify_tool_calls(input: Any, output: Any) -> Optional[str]:
    calls = output.get("calls")
    if not isinstance(calls, list) or not calls:
        return '"calls" must be a non-empty list'
    known = {t.get("name") for t in (input.get("tools") or [])}
    for c in calls:
        if not isinstance(c, dict) or c.get("tool") not in known:
            return f"every tool must be one of the listed tools exactly, got {c!r}"
        if not isinstance(c.get("independent"), bool):
            return '"independent" must be true or false'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            TOOL_CALLS, "1",
            "For one tool group, the concrete calls an unfinished task should make now, and which are independent.",
            input_schema=_object(
                {
                    "task": _STRING, "gap": _STRING, "group": _STRING,
                    "calls": {"type": "array", "items": _STRING},
                    "tools": {"type": "array", "items": {"type": "object"}},
                },
                required=["task", "group", "tools"],
            ),
            output_schema=_object({"calls": {"type": "array", "items": {"type": "object"}}}),
            build_prompt=prompt_tool_calls, verify=verify_tool_calls,
        ),
    ]
