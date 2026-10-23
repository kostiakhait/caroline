"""sw.tier.advice -- which tier of the cascade to start a task from, given its
tags and the statistics of how each tier did on tasks with similar tags
(2026-10-22, explicit instruction: tell the escalator where to start when a
tier fails too often on a kind of task). The cascade starts from the higher of
this advice and the fast lane's choice. See app/sw_tier_stats.py.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

TIER_ADVICE = "sw.tier.advice"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI assistant solves tasks with a ladder of models, cheapest first; a task the lower model cannot "
    "finish goes up the ladder, and the lower model's attempt is paid for anyway. Below: the tags of a new "
    "task, the ladder, and statistics -- for tags of earlier tasks, how many tasks each step handled itself "
    "(success) and how many it had to pass up (fail). Decide which step to start the new task from: the "
    "cheapest step that does NOT fail too often on tasks like this one. Match tags by meaning, not spelling. "
    "With no or too little data for tasks like this, answer step 0.\n\n"
    "TASK TAGS: {tags}\n\n"
    "LADDER (step: model):\n{tiers}\n\n"
    "STATISTICS (tag | step | total | success | fail):\n{stats}\n\n"
    'Reply with a JSON object: {{"start_tier": <step number>, "why": "<one short sentence>"}}'
)


def prompt_tier_advice(input: Any, context: Any) -> str:
    tiers = "\n".join(f"{i}: {m}" for i, m in enumerate(input.get("tiers") or []))
    rows = "\n".join(
        f"{r.get('tag')} | {r.get('tier')} | {r.get('total')} | {r.get('success')} | {r.get('fail')}"
        for r in (input.get("stats") or [])
    ) or "(no data yet)"
    return _PROMPT.format(tags=", ".join(input.get("tags") or []) or "(none)", tiers=tiers, stats=rows)


def verify_tier_advice(input: Any, output: Any) -> Optional[str]:
    tier = output.get("start_tier")
    if not isinstance(tier, int) or isinstance(tier, bool) or not 0 <= tier < len(input.get("tiers") or []):
        return '"start_tier" must be one of the ladder\'s step numbers'
    if not str(output.get("why") or "").strip():
        return '"why" must say in one sentence why'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            TIER_ADVICE, "1",
            "Picks the ladder step to start a task from, given its tags and how each step did on similar tasks.",
            input_schema=_object(
                {
                    "tags": {"type": "array", "items": _STRING},
                    "tiers": {"type": "array", "items": _STRING},
                    "stats": {"type": "array", "items": {"type": "object"}},
                },
                required=["tags", "tiers"],
            ),
            output_schema=_object({"start_tier": {"type": "integer"}, "why": _STRING}),
            build_prompt=prompt_tier_advice, verify=verify_tier_advice,
        ),
    ]
