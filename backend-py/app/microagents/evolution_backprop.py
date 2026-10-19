"""evolution.backprop -- Agent 2 of the evolution-feedback loop (per
explicit special order, 2026-10-19): given Agent 1's (turn.reaction.score)
attributed emotional-reaction score and the recent tournament rounds
(app/evolution_journal.py) that were run in service of the same action or
track, judges whether evolution.selector's own verdict in each of those
rounds actually tracks with how the user reacted, and proposes a bounded
weight_selector adjustment (how much Camerlengo's cull/promote formula
trusts the Selector's verdict for that role) for each round genuinely
implicated.

Client-side only, same "Camerlengo is a universal server" boundary as every
other new agent built today: this is Caroline-specific judgment, never
deployed to Camerlengo. The adjustment is only ever a RECOMMENDATION from
this agent -- applying it (fetch current weight_selector via
evolution:getSettings, clamp, evolution:setSettings) is deterministic code
in app/evolution_client.py, never left to the model.

Deliberately NOT given the recent_rounds' own numeric win/loss counters --
only each round's role/tournament id and evolution.selector's own stated
verdict_reason, the one piece of text that can actually establish whether a
round's verdict has anything to do with the thing the user reacted to
(a harsh-sounding message approving something unrelated must never
penalize a Selector verdict it had nothing to do with).

Called from chat_session_mixins/permission_guard.py's _run_evolution_backprop,
only when Agent 1 found a non-trivial, attributed reaction AND at least one
matching journaled round exists -- an empty recent_rounds list is handled
by the caller without even reaching this agent (see that method's own
early return)."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

BACKPROP = "evolution.backprop"

_STRING = {"type": "string"}
_WEIGHT_DELTA_BOUND = 0.2


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI assistant's microagent evolution engine runs tournaments between a champion and a challenger "
    "version of the same small AI role, judged by a separate \"evolution.selector\" agent. You are now "
    "applying a delayed, real-world correction signal to that judging process: the actual human user has "
    "just reacted -- on a -1 (complete disapproval) to +1 (complete approval) scale -- to something the "
    "assistant did, and that reaction has been attributed to a specific action/track. Below are the recent "
    "tournament rounds that were run in service of that SAME action/track, each with its own role, "
    "tournament id, and evolution.selector's own one-sentence verdict_reason for that round.\n\n"
    "For each round, judge whether evolution.selector's own stated reasoning for THAT round actually has "
    "anything to do with what the user just reacted to. If a round's reasoning is genuinely unrelated to the "
    "user's reaction (even though it happened to be run for the same action/track), leave it out of "
    "adjustments entirely -- do not penalize or reward a judgment call that had nothing to do with the thing "
    "being reacted to.\n\n"
    "For every round that IS genuinely implicated, propose weight_delta: a positive number (the user's "
    "reaction confirms the Selector's verdict was right to trust) when reaction_score and the round's outcome "
    "point the same way, or a negative number (the Selector's verdict should be trusted less going forward) "
    "when they point opposite ways. Magnitude should reflect both how strong the reaction was and how "
    "directly this specific round is implicated -- never the maximum every time. It is normal and common for "
    "NO round to be genuinely implicated -- return an empty adjustments list rather than forcing a pick.\n\n"
    "USER'S REACTION:\nreaction_score: {reaction_score} (-1 = complete disapproval, +1 = complete approval)\n"
    "What this reaction is about: {target_summary}\n\n"
    "RECENT TOURNAMENT ROUNDS FOR THIS SAME ACTION/TRACK:\n{rounds_block}\n\n"
    "Reply now."
)


def _rounds_block(rounds: Any) -> str:
    if not rounds:
        return "(none)"
    return "\n".join(
        f"- role: {r['role']} -- tournament_id: {r['tournament_id']} -- selector's reason: "
        f"{r.get('verdict_reason') or '(none given)'}"
        for r in rounds
    )


def prompt_backprop(input: Any, context: Any) -> str:
    return _PROMPT.format(
        reaction_score=input["reaction_score"], target_summary=input["target_summary"] or "(unknown)",
        rounds_block=_rounds_block(input.get("recent_rounds")),
    )


def verify_backprop(input: Any, output: Any) -> Optional[str]:
    adjustments = output.get("adjustments")
    if not isinstance(adjustments, list):
        return '"adjustments" must be a list'
    known_pairs = {(r["role"], r["tournament_id"]) for r in (input.get("recent_rounds") or [])}
    seen: set = set()
    for entry in adjustments:
        if not isinstance(entry, dict):
            return "every entry in \"adjustments\" must be an object"
        role, tournament_id = entry.get("role"), entry.get("tournament_id")
        if (role, tournament_id) not in known_pairs:
            return f'("role", "tournament_id") {(role, tournament_id)!r} is not one of the given recent_rounds'
        if role in seen:
            return f'role {role!r} appears more than once in "adjustments"'
        seen.add(role)
        delta = entry.get("weight_delta")
        if not isinstance(delta, (int, float)) or isinstance(delta, bool):
            return '"weight_delta" must be a number'
        if not (-_WEIGHT_DELTA_BOUND <= float(delta) <= _WEIGHT_DELTA_BOUND):
            return f'"weight_delta" must be between -{_WEIGHT_DELTA_BOUND} and {_WEIGHT_DELTA_BOUND}'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            BACKPROP, "1",
            "Given a user's attributed emotional-reaction score and the recent tournament rounds run for the "
            "same action/track, proposes a bounded weight_selector adjustment for each round whose "
            "evolution.selector verdict is genuinely implicated by that reaction.",
            input_schema=_object(
                {
                    "target_summary": _STRING,
                    "reaction_score": {"type": "number"},
                    "recent_rounds": {"type": "array", "items": {"type": "object"}},
                },
                required=["target_summary", "reaction_score", "recent_rounds"],
            ),
            output_schema=_object(
                {"adjustments": {"type": "array", "items": {"type": "object", "properties": {
                    "role": _STRING, "tournament_id": {"type": "integer"}, "weight_delta": {"type": "number"},
                }}}},
                required=["adjustments"],
            ),
            build_prompt=prompt_backprop, verify=verify_backprop,
        ),
    ]
