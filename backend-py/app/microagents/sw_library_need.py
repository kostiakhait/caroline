"""sw.library.need -- which things (models, surface materials, textures, sky, sounds, effects, characters) does a
user's request ask the Ferret game builder to make a game out of, as ENGLISH library search queries? (2026-11-25,
explicit instruction after Caroline took a road surface and a sky from the web instead of the asset library: "a
microagent that additionally searches the library for materials and prompts Caroline, as the tools prompt her".)
A general mechanism, no special cases. See app/library_advisor.py.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

LIBRARY_NEED = "sw.library.need"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "A game builder assistant makes 3D games and scenes from things taken out of a shared asset library: models "
    "(vehicles, buildings, trees and plants, props, characters and people), surface materials and textures (asphalt, grass, sand, "
    "metal, wood, stone), sky and environment lighting (HDRI), sounds and music, effects (smoke, fire, dust). "
    "Below is a new message from the user, with the last lines of the conversation. List the THINGS the work will be "
    "made of, in this order: FIRST every thing the message itself names (always, each one), THEN what a scene like this "
    "always has or what a builder adds on its own to make it look real: the ground and its surface, plants and trees, "
    "the roadside or shore, fences and barriers, buildings, people and crowds, the sky, the sounds and the effects of what "
    "moves. (For a harbour, say: ship, water, pier, crane, rope, seagull, harbour sound, fog.) Leave out what the "
    "conversation shows is already in the scene. "
    "Give one short ENGLISH search query for each of those things (the library is English only), each ONE thing in one or two plain "
    "words, the thing itself and not adjectives about it (\"race car\", \"asphalt\", \"tree\", \"grass\", \"crowd\", "
    "\"sky\", \"engine sound\", \"smoke\"). Translate from the language of the message. If the message is not about making "
    "or changing something in a game or scene (a question, thanks, a bug report about the tool itself, a balance or code change "
    "that adds no new thing, talk about something else) it needs nothing: answer false with no queries.\n\n"
    "CONVERSATION (latest last):\n{dialogue}\n\n"
    "NEW MESSAGE:\n{message}\n\n"
    'Reply with a JSON object: {{"needs_library": true|false, "why": "<one short sentence>", "queries": ["...", ...]}}'
)


def prompt_library_need(input: Any, context: Any) -> str:
    dialogue = "\n".join(str(x)[:400] for x in (input.get("dialogue") or [])[-6:]) or "(none yet)"
    return _PROMPT.format(dialogue=dialogue, message=str(input.get("message") or "")[:2000])


def verify_library_need(input: Any, output: Any) -> Optional[str]:
    if not isinstance(output.get("needs_library"), bool):
        return '"needs_library" must be true or false'
    if not str(output.get("why") or "").strip():
        return '"why" must say in one sentence why'
    queries = output.get("queries")
    if not isinstance(queries, list):
        return '"queries" must be a list of queries'
    if not all(isinstance(q, str) and q.strip() for q in queries):
        return "every query must be a non-empty string"
    if output["needs_library"] and not queries:
        return 'when the library is needed, "queries" must say what to look for'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            LIBRARY_NEED, "1",
            "Lists the things a game-building request needs, as English asset-library search queries.",
            input_schema=_object({"message": _STRING, "dialogue": {"type": "array", "items": _STRING}}),
            output_schema=_object({"needs_library": {"type": "boolean"}, "why": _STRING,
                                   "queries": {"type": "array", "items": _STRING}},
                                  required=["needs_library", "why", "queries"]),
            build_prompt=prompt_library_need, verify=verify_library_need,
        ),
    ]
