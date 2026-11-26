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

MAX_QUERIES = 5

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "A game builder assistant makes 3D games and scenes from things taken out of a shared asset library: models "
    "(vehicles, buildings, trees, props, characters), surface materials and textures (asphalt, grass, sand, "
    "metal, wood, stone), sky and environment lighting (HDRI), sounds and music, effects (smoke, fire, dust). "
    "Below is a new message from the user, with the last lines of the conversation. List the THINGS the work "
    "asked for (or implied by it, including the setting: a race needs a road surface, a sky, an engine sound) "
    "that the library may hold and that are not already settled in the conversation. Give 1 to "
    f"{MAX_QUERIES} short ENGLISH search queries (the library is English only), each naming ONE thing with its "
    "kind in one to four plain words (for example \"asphalt road texture\", \"overcast sky hdri\", \"engine "
    "loop sound\", \"race car model\"). Translate from the language of the message. If the message is not about "
    "making or changing something in a game or scene (a question, thanks, a bug report about the tool "
    "itself, talk about something else) it needs nothing: answer false with no queries.\n\n"
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
    if not isinstance(queries, list) or len(queries) > MAX_QUERIES:
        return f'"queries" must be a list of at most {MAX_QUERIES} queries'
    if not all(isinstance(q, str) and q.strip() and len(q) <= 80 for q in queries):
        return "every query must be a short non-empty string"
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
