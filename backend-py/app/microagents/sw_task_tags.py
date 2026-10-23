"""sw.task.tags -- free tags for a task, for the tier statistics (2026-10-22,
explicit instruction: a microagent keeps statistics of which tier fails too
often on which tasks and tells the cascade where to start; task kinds are
free tags, not a fixed list). See app/sw_tier_stats.py.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

TASK_TAGS = "sw.task.tags"

MAX_TAGS = 6

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "Give a few short tags (1-3 words each, lowercase English) that say what KIND of task this is -- the kind "
    "of work, the objects, the channels or tools it involves (e.g. \"email reply\", \"find letter\", \"word "
    "document\", \"spreadsheet edit\", \"messenger send\", \"code script\"). Generic enough that similar future "
    "tasks get the same tags; never names, addresses, numbers or other specifics of this one task. "
    f"At most {MAX_TAGS} tags.\n\n"
    "TASK:\n{task}\n\n"
    'Reply with a JSON object: {{"tags": ["...", ...]}}'
)


def prompt_task_tags(input: Any, context: Any) -> str:
    return _PROMPT.format(task=input.get("task") or "(not given)")


def verify_task_tags(input: Any, output: Any) -> Optional[str]:
    tags = output.get("tags")
    if not isinstance(tags, list) or not tags or len(tags) > MAX_TAGS:
        return f'"tags" must be a list of 1 to {MAX_TAGS} tags'
    if not all(isinstance(t, str) and t.strip() and len(t) <= 40 for t in tags):
        return "every tag must be a short non-empty string"
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            TASK_TAGS, "1",
            "Tags a task by its kind -- work, objects, channels -- for statistics over similar tasks.",
            input_schema=_object({"task": _STRING}),
            output_schema=_object({"tags": {"type": "array", "items": _STRING}}),
            build_prompt=prompt_task_tags, verify=verify_task_tags,
        ),
    ]
