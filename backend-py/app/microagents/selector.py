"""evolution.selector -- given two results produced by two candidate
versions of the SAME microagent role, for the SAME real task, picks
whichever result better serves that role's own declared purpose -- or says
the two are genuinely indistinguishable. A separate agent from
evolution.mutator (which changes instructions) and from any future
evolution.evaluator (Phase C's own judge, for whether a Combinator's
merged result beats both of its parents -- a different comparison shape:
pairwise-against-a-merge, not pick-the-better-of-two).

Judges purely by the role's own stated purpose -- never by which model,
cost or speed produced which answer; those are separate fitness signals
combined elsewhere (API/Api2Evolution.py's own cull decision), not this
agent's concern.

The caller (API/Api2Evolution.py's run_evolvable) assigns which real
candidate is "A" and which is "B" RANDOMLY on every call, never by a fixed
"champion is always A" convention -- so this agent is never shown, and can
never pick up on, which side is the incumbent, closing off positional or
identity bias before it can start. The caller maps the verdict back to
champion/challenger itself, after the call."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

SELECT = "evolution.selector"

_STRING = {"type": "string"}
_WINNER_ENUM = {"type": "string", "enum": ["A", "B", "tie"]}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


def prompt_select(input: Any, context: Any) -> str:
    return (
        "You are comparing two real candidate results produced for the exact same task, by two versions of "
        "the same AI agent. Judge ONLY by how well each result serves the role's own stated purpose below -- "
        "never by length, confidence, tone, or anything else unrelated to actually doing the job.\n\n"
        f"PURPOSE this role exists for (judge by this, nothing else): {input['role_purpose']}\n\n"
        f"THE TASK both candidates were given:\n{input['task_input_summary']}\n\n"
        f"CANDIDATE A:\n{input['candidate_a_output']}\n\n"
        f"CANDIDATE B:\n{input['candidate_b_output']}\n\n"
        "Which candidate better serves the stated purpose for this exact task? If they are genuinely "
        "indistinguishable in quality -- not just similar, but with no real basis to prefer one -- say so "
        "rather than forcing a pick to seem decisive. Give one short, plain sentence explaining the verdict, "
        "for a log, not for an end user."
    )


def verify_select(input: Any, output: Any) -> Optional[str]:
    if output.get("winner") not in ("A", "B", "tie"):
        return '"winner" must be "A", "B" or "tie"'
    return None


def llm_agents() -> List[M.Agent]:
    return [
        M.Agent(
            SELECT, "1",
            "Picks the better of two real results for the same task and role, by that role's own stated "
            "purpose, or says they're indistinguishable.",
            input_schema=_object(
                {"role_purpose": _STRING, "task_input_summary": _STRING, "candidate_a_output": _STRING,
                 "candidate_b_output": _STRING},
                required=["role_purpose", "task_input_summary", "candidate_a_output", "candidate_b_output"],
            ),
            output_schema=_object({"winner": _WINNER_ENUM, "reason": _STRING}, required=["winner", "reason"]),
            build_prompt=prompt_select, verify=verify_select,
        ),
    ]
