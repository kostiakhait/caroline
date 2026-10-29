"""turn.rung.advice -- which step of an engine's model ladder a turn starts on,
judged from what is at stake, how the conversation has gone so far and the
user's reaction (2026-10-28, explicit instruction: "on serious questions such
as legal ones the advisor must take the model up the ladder, in every engine;
the model decides how far up"). No topic list, no fixed rule: the decision is
this agent's. See docs/STAKES_AWARE_LADDER_PLAN.md and app/rung_advice.py,
which calls it for the Claude ladder, the sw cascade and Codex's effort.

The advice is a floor: the engine's ladder still goes up on a failed verdict.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

RUNG_ADVICE = "turn.rung.advice"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI assistant answers each message of a conversation with a ladder of models, cheapest first. A step that "
    "fails is caught by a checker and the work goes up, but a careless answer that looks right can get through, "
    "and the user then has to correct it. Decide which step this message should START on.\n\n"
    "Judge three things, by meaning, not by keywords:\n"
    "1. STAKES: what a wrong or careless answer would cost the user. Legal, medical, financial matters, a document "
    "the user relies on, a decision with consequences -- high. Chit-chat, a lookup, a routine report, a small "
    "errand -- low.\n"
    "2. THE CONVERSATION SO FAR: if earlier turns on this same matter had to go up the ladder, or the user had to "
    "correct them, start at least on the step that finally managed it. A new, unrelated topic does not inherit "
    "that -- it can and should go back down.\n"
    "3. THE USER'S REACTION: dissatisfaction with the previous answer (\"you did not read it\", \"that is not what "
    "it says\", \"wrong again\") means the step that gave it was not enough -- start above it.\n"
    "Do not waste the top steps on small things; do not put a serious matter on the bottom step.\n\n"
    "LADDER (step: model -- what it is good for):\n{ladder}\n\n"
    "RECENT TURNS OF THIS CONVERSATION, as the ladder saw them (oldest first):\n{history}\n\n"
    "USER'S REACTION TO THE PREVIOUS ANSWER (-1 strongly dissatisfied .. +1 pleased, 0 none): {reaction}\n\n"
    "RECENT DIALOGUE:\n---\n{dialogue}\n---\n\n"
    "THE MESSAGE TO ANSWER NOW:\n---\n{request}\n---\n\n"
    'Reply with a JSON object: {{"start_rung": <step number>, "why": "<one short sentence>"}}'
)


def _ladder_block(ladder: Any) -> str:
    return "\n".join(f"{i}: {s.get('model')} -- {s.get('good_for')}" for i, s in enumerate(ladder or []))


def _history_block(history: Any) -> str:
    lines = []
    for h in history or []:
        notes = "; ".join(str(n) for n in h.get("escalation_notes") or [])
        lines.append(
            f"- {h.get('ago')} ago [{h.get('engine')}] \"{h.get('request')}\": started on {h.get('started')}, "
            f"ended on {h.get('ended')}" + (f"; why it went up: {notes}" if notes else "")
        )
    return "\n".join(lines) or "(none)"


def prompt_rung_advice(input: Any, context: Any) -> str:
    reaction = input.get("reaction")
    return _PROMPT.format(
        ladder=_ladder_block(input.get("ladder")),
        history=_history_block(input.get("history")),
        reaction="unknown" if reaction is None else f"{float(reaction):+.2f}",
        dialogue=input.get("dialogue") or "(none)",
        request=input.get("request") or "(empty)",
    )


def verify_rung_advice(input: Any, output: Any) -> Optional[str]:
    rung = output.get("start_rung")
    if not isinstance(rung, int) or isinstance(rung, bool) or not 0 <= rung < len(input.get("ladder") or []):
        return '"start_rung" must be one of the ladder\'s step numbers'
    if not str(output.get("why") or "").strip():
        return '"why" must say in one sentence why'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            RUNG_ADVICE, "1",
            "Picks the ladder step a turn starts on, from the stakes, the conversation so far and the user's reaction.",
            input_schema=_object(
                {
                    "request": _STRING,
                    "dialogue": _STRING,
                    "history": {"type": "array", "items": {"type": "object"}},
                    "reaction": {"type": ["number", "null"]},
                    "ladder": {"type": "array", "items": {"type": "object"}},
                },
                required=["request", "ladder"],
            ),
            output_schema=_object({"start_rung": {"type": "integer"}, "why": _STRING}),
            build_prompt=prompt_rung_advice, verify=verify_rung_advice,
        ),
    ]
