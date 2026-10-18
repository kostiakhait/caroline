"""evolution.mutator -- given one microagent's current rules text (its
genome) and the purpose that agent exists to serve, produces a genuinely
different variant that still serves that exact purpose: different
structure, phrasing, emphasis, examples or coverage, never a change to
what the agent is FOR. The model ladder (which model class(es) the agent
escalates through) is the only other mutable gene this agent may touch;
input_schema/output_schema/verify are never shown to it at all -- there is
nothing in its own input for it to change them with (see EvolvableRoles.py's
resolve_agent_for_clone, the one place those stay fixed no matter what this
agent produces).

Called once per NEW challenger (API/Api2Evolution.py's run_evolvable),
never once per call -- an open tournament reuses the same challenger
across every comparison until it's promoted or culled, so this agent's own
cost is paid once per tournament, not once per request.

Registered into every Runtime that hosts at least one evolvable role (see
EvolvableRoles.EVOLVABLE_ROLES). It is itself an ordinary microagent and
is, by the evolution engine's own design, as eligible for this same
replicate/mutate/select process as anything else once a meta-agent
evolution path exists -- see the engine's own plan doc, Phase F, for the
(explicitly still open) question of how a mutated judge gets validated at
all."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

MUTATE = "evolution.mutator"

_STRING = {"type": "string"}
_LADDER_CHANGE_ENUM = {"type": "string", "enum": ["keep", "widen_to_mini_first", "narrow_to_nano_only"]}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


def prompt_mutate(input: Any, context: Any) -> str:
    hint = (input.get("mutation_hint") or "").strip()
    return (
        "You are producing ONE new variant of an existing AI agent's instructions, for a real evolutionary "
        "experiment: this variant will run side by side with the current one on real tasks, and the one that "
        "serves its purpose better survives. The agent's PURPOSE below must not change -- only HOW it "
        "pursues that purpose may.\n\n"
        f"PURPOSE (never change what this agent is for): {input['purpose']}\n\n"
        f"CURRENT INSTRUCTIONS (the genome to mutate):\n---\n{input['parent_rules_text']}\n---\n\n"
        f"CURRENT MODEL LADDER: {input['parent_ladder_summary']}\n\n"
        "Write a new version of the instructions that still achieves the exact same purpose and still covers "
        "every explicit rule the current version states (do not silently drop a rule a real incident put "
        "there) -- but genuinely differs in at least one of: structure, phrasing/emphasis, which examples are "
        "used, or ordering. A cosmetic rewrite that changes nothing real is not a useful variant here.\n\n"
        "You may also propose a ladder change if you have a concrete reason (e.g. this role's judgment calls "
        "seem to need stronger reasoning than the default nano-first ladder gives them, or seem cheap/clear-"
        "cut enough that nano alone would do) -- \"keep\" is the right answer whenever you have no such "
        "reason; do not change the ladder just to have changed something.\n\n"
        f"{hint}"
    )


def verify_mutate(input: Any, output: Any) -> Optional[str]:
    text = (output.get("mutated_rules_text") or "").strip()
    if len(text) < 200:
        return '"mutated_rules_text" is too short to be a real variant of the current instructions'
    if text == (input.get("parent_rules_text") or "").strip():
        return "the mutated instructions must genuinely differ from the current ones"
    return None


def llm_agents() -> List[M.Agent]:
    return [
        M.Agent(
            MUTATE, "1",
            "Produces a genuinely different variant of a microagent's instructions that still serves its "
            "exact same stated purpose; may also propose a model-ladder change.",
            input_schema=_object(
                {"purpose": _STRING, "parent_rules_text": _STRING, "parent_ladder_summary": _STRING,
                 "mutation_hint": _STRING},
                required=["purpose", "parent_rules_text", "parent_ladder_summary"],
            ),
            output_schema=_object(
                {"mutated_rules_text": _STRING, "ladder_change": _LADDER_CHANGE_ENUM, "rationale": _STRING},
                required=["mutated_rules_text", "ladder_change", "rationale"],
            ),
            build_prompt=prompt_mutate, verify=verify_mutate,
        ),
    ]
