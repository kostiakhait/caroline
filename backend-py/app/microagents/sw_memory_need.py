"""sw.memory.need -- does answering a user message depend on something Caroline may already know (who the
user is, their contacts, earlier work, saved credentials, agreements, preferences)? (2026-10-26, explicit
instruction after a reinstalled Caroline did not know her owner and did not look in her memory: "there must
be an advisor microagent that demands the search in memory when the request implies it" -- a general
mechanism, never special cases.) The answer is the memory queries to run. See app/memory_advisor.py.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

MEMORY_NEED = "sw.memory.need"

MAX_QUERIES = 3

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI personal assistant keeps a long-term memory: who its owner is, people and contacts, accounts and "
    "saved credentials, earlier work and decisions, agreements, preferences. A new message from the user is "
    "below, with the last lines of the conversation. Decide whether a good answer needs something that may "
    "be in that memory and is NOT already in the conversation lines below -- anything that refers to the "
    "user, their people, their things, their earlier requests or agreements, or that needs a fact only the "
    "user could have given before (a name, address, password, preference, what was done or decided). "
    "Greetings, thanks, and general-knowledge questions that do not depend on the user do not need memory. "
    "When in doubt, say it is needed: a needless look-up costs little, a missed one makes the assistant "
    "answer as if it did not know the user.\n"
    f"If needed, give 1 to {MAX_QUERIES} short search queries (in the language of the message, each about "
    "one subject, naming the thing to find, not the whole message).\n\n"
    "CONVERSATION (latest last):\n{dialogue}\n\n"
    "NEW MESSAGE:\n{message}\n\n"
    'Reply with a JSON object: {{"needs_memory": true|false, "why": "<one short sentence>", "queries": ["...", ...]}}'
)


def prompt_memory_need(input: Any, context: Any) -> str:
    dialogue = "\n".join(str(x)[:400] for x in (input.get("dialogue") or [])[-6:]) or "(none yet)"
    return _PROMPT.format(dialogue=dialogue, message=str(input.get("message") or "")[:2000])


def verify_memory_need(input: Any, output: Any) -> Optional[str]:
    needs = output.get("needs_memory")
    if not isinstance(needs, bool):
        return '"needs_memory" must be true or false'
    if not str(output.get("why") or "").strip():
        return '"why" must say in one sentence why'
    queries = output.get("queries")
    if not isinstance(queries, list) or len(queries) > MAX_QUERIES:
        return f'"queries" must be a list of at most {MAX_QUERIES} queries'
    if not all(isinstance(q, str) and q.strip() and len(q) <= 200 for q in queries):
        return "every query must be a short non-empty string"
    if needs and not queries:
        return 'when memory is needed, "queries" must say what to look for'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            MEMORY_NEED, "1",
            "Decides whether a user message needs the assistant's long-term memory, and what to search for.",
            input_schema=_object({"message": _STRING, "dialogue": {"type": "array", "items": _STRING}}),
            output_schema=_object({"needs_memory": {"type": "boolean"}, "why": _STRING,
                                   "queries": {"type": "array", "items": _STRING}}),
            build_prompt=prompt_memory_need, verify=verify_memory_need,
        ),
    ]
