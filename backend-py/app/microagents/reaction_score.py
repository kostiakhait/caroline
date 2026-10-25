"""turn.reaction.score -- the user's reaction for the evolution (per
explicit special order, 2026-10-19): from one real user message, scores
the user's emotional reaction to something Caroline did on a continuous
scale from -1 (complete disapproval) to +1 (complete approval), and tries
to attribute that reaction to a specific open action (app/action_registry.py)
or the tab's currently active track (app/memory_topics.py's
get_current_topic). The overwhelmingly common case is a message with no
evaluable reaction at all -- a score near 0 with no target, which is not an
error.

Client-side only, same "Camerlengo is a universal server" boundary as
action_sanction.py/permission_identify.py: this is Caroline-specific
judgment about Caroline's own user's reaction to Caroline's own work, never
deployed to Camerlengo, run entirely via app/microagent_client.py's local
Runtime + the generic microagent:call primitive.

Called from chat_session_mixins/permission_guard.py's _check_reaction_score
(submit()'s real-user-message path, same cadence as _check_action_sanction),
on EVERY real user message -- never skipped, since a reaction can be about
something that happened with no open action at all (a bare conversational
compliment/complaint about the current track). Every run is logged by the
caller, including the no-op case, per explicit instruction ("Логировать
все это надо").

The score goes to the evolution as the user's reaction to the previous turn
(2026-10-24, permission_guard.py's _check_reaction_score; the attribution
fields are logged, not used there) -- never this agent's own concern."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

SCORE = "turn.reaction.score"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "You are watching one real message a user just sent to their AI assistant, judging ONLY their emotional "
    "reaction to something the assistant recently did, is doing, or said -- NOT whether the message itself "
    "asks for something new.\n\n"
    "Score this reaction on a continuous scale from -1.0 (complete disapproval -- clearly upset, frustrated, "
    "rejecting what was done) to +1.0 (complete approval -- clearly pleased, grateful, satisfied), with 0.0 "
    "meaning no discernible reaction either way. Most real messages carry NO evaluable reaction at all -- a "
    "plain new request, a neutral follow-up question, small talk -- and 0.0 with no target is the correct, "
    "expected answer for those; do not strain to find a reaction that isn't really there, and do not confuse "
    "a merely business-like or terse tone with disapproval.\n\n"
    "If, and only if, the score is clearly non-zero, also decide WHAT specifically the reaction is about -- "
    "pick ONE of the candidates listed below (an open, bounded action, or the tab's current longer-running "
    "track), whichever one the message is actually reacting to. Never invent a target not listed, and never "
    "guess a target for a message that is really just reacting to the conversation in general -- leave both "
    "target fields null in that case even if the score itself is non-zero.\n\n"
    "USER MESSAGE JUST RECEIVED:\n---\n{message}\n---\n\n"
    "OPEN ACTIONS (candidates for target_action_id -- empty means none open):\n{open_actions_block}\n\n"
    "CANDIDATE TRACK IDS (candidates for target_track_id -- empty means none):\n{track_ids_block}\n\n"
    "Reply now."
)


def _open_actions_block(open_actions: Any) -> str:
    if not open_actions:
        return "(none)"
    return "\n".join(f"- id: {a['id']} -- summary: {a['summary']}" for a in open_actions)


def _track_ids_block(track_ids: Any) -> str:
    if not track_ids:
        return "(none)"
    return "\n".join(f"- {tid}" for tid in track_ids)


def prompt_score(input: Any, context: Any) -> str:
    return _PROMPT.format(
        message=input["message"] or "(empty)",
        open_actions_block=_open_actions_block(input.get("open_actions")),
        track_ids_block=_track_ids_block(input.get("candidate_track_ids")),
    )


def verify_score(input: Any, output: Any) -> Optional[str]:
    score = output.get("reaction_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return '"reaction_score" must be a number'
    if not (-1.0 <= float(score) <= 1.0):
        return '"reaction_score" must be between -1.0 and 1.0'
    target_action_id = output.get("target_action_id")
    if target_action_id is not None:
        known_ids = {a["id"] for a in (input.get("open_actions") or [])}
        if target_action_id not in known_ids:
            return f'"target_action_id" must be null or one of the given open_actions ids, got {target_action_id!r}'
    target_track_id = output.get("target_track_id")
    if target_track_id is not None:
        known_tracks = set(input.get("candidate_track_ids") or [])
        if target_track_id not in known_tracks:
            return f'"target_track_id" must be null or one of the given candidate_track_ids, got {target_track_id!r}'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            SCORE, "1",
            "Scores the user's emotional reaction to something the assistant recently did, from -1 (complete "
            "disapproval) to +1 (complete approval), and attributes a non-trivial reaction to a specific open "
            "action or the tab's current track, when one is clearly implicated.",
            input_schema=_object(
                {
                    "message": _STRING,
                    "open_actions": {"type": "array", "items": {"type": "object"}},
                    "candidate_track_ids": {"type": "array", "items": _STRING},
                },
                required=["message"],
            ),
            output_schema=_object(
                {
                    "reaction_score": {"type": "number"},
                    "target_action_id": {"type": ["string", "null"]},
                    "target_track_id": {"type": ["string", "null"]},
                },
                required=["reaction_score", "target_action_id", "target_track_id"],
            ),
            build_prompt=prompt_score, verify=verify_score,
        ),
    ]
