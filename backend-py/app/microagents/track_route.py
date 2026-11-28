"""turn.track.route -- which track (matter) does the owner's new message belong to? (2026-11-20, owner's order after
three parallel matters -- a vacancy, an earlier confirmation to Anita, a letter to Anita -- were mixed in one reply.)

Judges only: the track the message belongs to (an existing one, or a new matter), whether the message is a plain
mention or an explicit instruction about it, and for a new matter the first importance estimate. The importance
arithmetic is code (app/track_importance.py); the block that goes to the engine is built in app/memory_advisor.py.

Client-side only: run through the local microagent ladder (same as sw.memory.need)."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

TRACK_ROUTE = "turn.track.route"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI secretary works on several matters of her owner in parallel. Each matter is a TRACK. Below are the open "
    "tracks (id, short description, importance 0..1, [closed] if finished), the track this tab worked on last, the "
    "last lines of the conversation, and the owner's NEW message.\n\n"
    "Decide which track the NEW message belongs to. Read it in the light of the conversation: a short message such "
    "as \"check that the reply went out\" or \"yes\" belongs to the matter the conversation was just about, not to "
    "whichever track looks most like its words. A message about a vacancy is not about a mailbox check; a letter to "
    "a person is a track of that person's matter. A bare go-ahead (\"do it\", \"go on\", \"Делаем\") belongs to the "
    "matter of the OWNER'S OWN latest request that is not carried out yet (see his own messages below), or of the "
    "question the secretary just put to him in answer to him -- never to a report about an incoming letter or message "
    "that he did not react to.\n"
    "- track_id: the id of the existing track it belongs to; \"\" when it starts a NEW matter that none of the tracks "
    "covers (or when it is small talk that belongs to no matter).\n"
    "- matter: 2 to 6 words naming the matter in the language of the message, as a person would say it (\"the "
    "vacancy at V2 Infra Tech\", \"the session with Anita\", \"the letter to Lynette Dickerson\"). Always give it.\n"
    "- explicit: true when the owner gives an instruction or stresses importance about this matter (an order, \"do "
    "it\", \"this is important\", \"do not forget\", an angry correction of how it was handled); false for a plain "
    "mention or question.\n"
    "- importance: for a NEW matter only (track_id empty), how important it is for the owner, 0..1 (money, health, "
    "family, legal, deadlines today = high; routine = low); otherwise 0.\n\n"
    "OPEN TRACKS:\n{tracks}\n\nLAST WORKED ON IN THIS TAB: {current}\n\n"
    "THE OWNER'S OWN LATEST MESSAGES (latest last):\n{owner_messages}\n\n"
    "CONVERSATION (latest last):\n{dialogue}\n\nNEW MESSAGE:\n{message}\n\n"
    'Reply with a JSON object: {{"track_id": "<id or empty>", "matter": "...", "explicit": true|false, "importance": 0.0}}'
)


def prompt_track_route(input: Any, context: Any) -> str:
    tracks = input.get("tracks") or []
    lines = [
        f"- id: {t['id']} -- {t.get('description', '')}" + (f" -- importance {t['importance']:.2f}" if t.get("importance") is not None else "")
        + (" [closed]" if t.get("closed") else "")
        for t in tracks
    ]
    return _PROMPT.format(
        tracks="\n".join(lines) or "(none)", current=input.get("current_id") or "(none)",
        dialogue="\n".join(input.get("dialogue") or []) or "(none)", message=input.get("message") or "(empty)",
        owner_messages="\n".join(input.get("owner_messages") or []) or "(none)",
    )


def verify_track_route(input: Any, output: Any) -> Optional[str]:
    ids = {t["id"] for t in input.get("tracks") or []}
    track_id = output.get("track_id")
    if not isinstance(track_id, str) or (track_id and track_id not in ids):
        return '"track_id" must be "" or one of the given track ids'
    if not str(output.get("matter") or "").strip():
        return '"matter" must name the matter in a few words'
    if not isinstance(output.get("explicit"), bool):
        return '"explicit" must be true or false'
    importance = output.get("importance")
    if not isinstance(importance, (int, float)) or isinstance(importance, bool) or not 0 <= importance <= 1:
        return '"importance" must be a number from 0 to 1'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            TRACK_ROUTE, "2",
            "Decides which open track (matter) the owner's new message belongs to, whether it is an explicit "
            "instruction about it, and rates a new matter's importance.",
            input_schema=_object(
                {"message": _STRING, "dialogue": {"type": "array", "items": _STRING},
                 "owner_messages": {"type": "array", "items": _STRING},
                 "tracks": {"type": "array", "items": {"type": "object"}}, "current_id": _STRING},
                required=["message"],
            ),
            output_schema=_object({"track_id": _STRING, "matter": _STRING, "explicit": {"type": "boolean"}, "importance": {"type": "number"}}),
            build_prompt=prompt_track_route, verify=verify_track_route,
        ),
    ]
