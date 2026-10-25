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


def _context_block(input: Any) -> str:
    """What the judge knows beyond the two answers (2026-10-24, explicit order: "Нам надо
    дать судье контекст запроса"): the role's full rules, the user's request this call was
    serving, and the last lines of the dialogue. Each part only when the caller has it."""
    parts = []
    if input.get("role_rules"):
        parts.append("THE ROLE'S RULES -- what a good answer of this role must do (the version currently in "
                     f"use; placeholders like {{{{DATA_BLOCK}}}} stand for the task data shown below):\n"
                     f"<<<\n{input['role_rules']}\n>>>")
    if input.get("user_request"):
        parts.append(f"WHAT THE USER ASKED -- the request this call was made to serve:\n<<<\n{input['user_request']}\n>>>")
    if input.get("recent_dialogue"):
        parts.append(f"THE LAST LINES OF THE DIALOGUE, oldest first:\n<<<\n{input['recent_dialogue']}\n>>>")
    return "\n\n".join(parts) + "\n\n" if parts else ""


def prompt_select(input: Any, context: Any) -> str:
    return (
        "You are comparing two real candidate results produced for the exact same task, by two versions of "
        "the same AI agent. Judge ONLY by how well each result serves the role's purpose and rules below, "
        "for what the user actually wanted -- never by length, confidence, tone, or anything else unrelated "
        "to actually doing the job. Use the user's request and the dialogue to see what the right answer "
        "for THIS task is: a result that follows the rules but misses what the user needed is the worse one.\n\n"
        f"PURPOSE this role exists for: {input['role_purpose']}\n\n"
        + _context_block(input) +
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
            # "2" (2026-10-24): the judge also gets the role's rules, the user's request and the dialogue.
            SELECT, "2",
            "Picks the better of two real results for the same task and role, by that role's own stated "
            "purpose and rules and what the user asked, or says they're indistinguishable.",
            input_schema=_object(
                {"role_purpose": _STRING, "task_input_summary": _STRING, "candidate_a_output": _STRING,
                 "candidate_b_output": _STRING, "role_rules": _STRING, "user_request": _STRING,
                 "recent_dialogue": _STRING},
                required=["role_purpose", "task_input_summary", "candidate_a_output", "candidate_b_output"],
            ),
            output_schema=_object({"winner": _WINNER_ENUM, "reason": _STRING}, required=["winner", "reason"]),
            build_prompt=prompt_select, verify=verify_select,
        ),
    ]
