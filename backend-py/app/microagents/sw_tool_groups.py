"""sw.tools.groups -- step 1 of the tool advisor (2026-10-22, explicit
instruction: the checker that hints is a tool advisor -- it knows the tools
and says "try this tool", ranked from the most likely; one group per
attempt). Given an unfinished attempt, ranks the plugin groups by how likely
they are to close the gap. Step 2 (sw_tool_calls.py) turns the top untried
group into concrete calls.

Client-side only, same as sw_attempt_progress.py: run through
app/microagent_client.py's local Runtime (the microagent ladder)."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

TOOL_GROUPS = "sw.tools.groups"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI assistant is doing a task with tools and has not finished it. Below: the task, what is still "
    "missing, the tool calls it made so far with their results, and the groups of tools it has (each group "
    "with the names of its tools). Rank the groups by how likely their tools are to get what is missing -- "
    "most likely first. Leave out groups that cannot help at all. Prefer looking things up (memory, the "
    "user's saved data, the mailbox, files) over asking the user.\n\n"
    "TASK:\n{task}\n\n"
    "WHAT IS STILL MISSING:\n{gap}\n\n"
    "CALLS SO FAR:\n{calls}\n\n"
    "GROUPS ALREADY SUGGESTED IN THIS TASK (rank them only if they are still the best bet):\n{tried}\n\n"
    "TOOL GROUPS:\n{groups}\n\n"
    'Reply with a JSON object: {{"groups": [{{"name": "<group name exactly as listed>", "why": "<one short '
    'sentence>"}}, ...]}}'
)


def _render_groups(groups: Any) -> str:
    return "\n".join(f"- {g.get('name')}: {', '.join(g.get('tools') or [])}" for g in (groups or [])) or "(none)"


def prompt_tool_groups(input: Any, context: Any) -> str:
    return _PROMPT.format(
        task=input.get("task") or "(not given)",
        gap=input.get("gap") or "(not given)",
        calls="\n".join(input.get("calls") or []) or "(none)",
        tried=", ".join(input.get("tried") or []) or "(none)",
        groups=_render_groups(input.get("groups")),
    )


def verify_tool_groups(input: Any, output: Any) -> Optional[str]:
    ranked = output.get("groups")
    if not isinstance(ranked, list) or not ranked:
        return '"groups" must be a non-empty list'
    known = {g.get("name") for g in (input.get("groups") or [])}
    for g in ranked:
        if not isinstance(g, dict) or g.get("name") not in known:
            return f'every group name must be one of the listed groups exactly, got {g!r}'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            TOOL_GROUPS, "1",
            "Ranks an assistant's tool groups by how likely they are to get what an unfinished task still misses.",
            input_schema=_object(
                {
                    "task": _STRING, "gap": _STRING,
                    "calls": {"type": "array", "items": _STRING},
                    "tried": {"type": "array", "items": _STRING},
                    "groups": {"type": "array", "items": {"type": "object"}},
                },
                required=["task", "groups"],
            ),
            output_schema=_object({"groups": {"type": "array", "items": {"type": "object"}}}),
            build_prompt=prompt_tool_groups, verify=verify_tool_groups,
        ),
    ]
