"""akinakes.level.ownership -- is a request that went over the akinakes Agent Bus to everyone the work of this level
secretary, and of which of its projects or bureaus (akinakes plan, group 3, design F; owner, 2026-11-01 and 2026-11-05:
"every agent monitors the bus", "a microagent on the nano rung decides whether a request is its own", "no tags or
domains set by the sender, no fixed matching").

One call per broadcast request, run by app/akinakes_channel.py in a process started with AKINAKES_LEVEL_SECRETARY. A
clear "no" must stay cheap: verification fails only on a malformed answer, never on the decision itself, so a "no"
does not climb the ladder.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

LEVEL_OWNERSHIP = "akinakes.level.ownership"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "You are the secretary of every {level} of an organization. People and agents of the organization send requests "
    "over a shared message bus that every agent sees; this request was sent to everyone, not to you by name. Decide "
    "whether it is yours.\n\n"
    "It is yours when it is the business of one of your {level}s: something to record for that {level} (for a project: "
    "its objective, a decision, who does which task; for a bureau: a decision of the bureau, a summary of its "
    "discussion), or work that belongs to that {level} and that its members should do. Judge by meaning: what the "
    "request is about, where it was asked (a request asked in a {level}'s own room and about \"this {level}\" or "
    "\"us\" is usually that {level}'s), and what each {level} is and who is in it. If the request is the business of "
    "another kind of unit, of nobody in particular, or only a question or a greeting to whoever reads it, it is not "
    "yours. If you cannot tell which of your {level}s it belongs to, it is not yours.\n\n"
    "WHERE IT WAS ASKED:\n{origin}\n\n"
    "THE REQUEST, in the sender's words:\n---\n{request}\n---\n\n"
    "YOUR {LEVEL}S (id, name, parent, members):\n{rooms}\n\n"
    'Reply with a JSON object: {{"mine": true or false, "room_id": "<the id of the {level} it belongs to, or an empty '
    'string when it is not yours>", "why": "<one short sentence>"}}'
)


def _room_line(room: dict) -> str:
    members = []
    for m in room.get("members") or []:
        what = " -- ".join(str(x) for x in (m.get("role"), m.get("description")) if x)
        members.append(f'{m.get("uid")} ({m.get("kind") or "member"}{": " + what if what else ""})')
    parent = room.get("parent_name") or room.get("parent_id") or "none"
    return f'- {room.get("id")}: "{room.get("name")}", parent {parent}; members: {"; ".join(members) or "none"}'


def _origin_block(origin: Any) -> str:
    if not isinstance(origin, dict) or not origin.get("id"):
        return "(not known)"
    return (f'room {origin.get("id")}: "{origin.get("name") or ""}", a {origin.get("type") or "room"}, '
            f'parent {origin.get("parent_name") or origin.get("parent_id") or "none"}')


def prompt_level_ownership(input: Any, context: Any) -> str:
    level = str(input.get("level") or "unit")
    return _PROMPT.format(
        level=level,
        LEVEL=level.upper(),
        origin=_origin_block(input.get("origin")),
        request=input.get("request") or "(empty)",
        rooms="\n".join(_room_line(r) for r in input.get("rooms") or []) or "(none)",
    )


def verify_level_ownership(input: Any, output: Any) -> Optional[str]:
    mine = output.get("mine")
    if not isinstance(mine, bool):
        return '"mine" must be true or false'
    if mine and output.get("room_id") not in {r.get("id") for r in input.get("rooms") or []}:
        return '"room_id" must be the id of one of the listed rooms when "mine" is true'
    if not str(output.get("why") or "").strip():
        return '"why" must say in one sentence why'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            LEVEL_OWNERSHIP, "1",
            "Decides whether a request broadcast on the akinakes bus belongs to one of this secretary's projects or bureaus, and to which.",
            input_schema=_object(
                {
                    "level": _STRING,
                    "request": _STRING,
                    "origin": {"type": "object"},
                    "rooms": {"type": "array", "items": {"type": "object"}},
                },
                required=["level", "request", "rooms"],
            ),
            output_schema=_object({"mine": {"type": "boolean"}, "room_id": _STRING, "why": _STRING}),
            build_prompt=prompt_level_ownership, verify=verify_level_ownership,
        ),
    ]
