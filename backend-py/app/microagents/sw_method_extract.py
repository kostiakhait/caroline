"""sw.method.extract -- after a real turn with tool work, did the assistant find a way of doing this KIND of task
that it would want next time? (2026-11-02, explicit instruction after Caroline told the user she had "no channel
to ChatGPT" in a session where she had worked with ChatGPT in the embedded browser the whole time: she must
remember "earlier I solved this kind of task like this" -- a general mechanism, never special cases.) The answer
is one short method sentence for the long-term memory, or nothing. See app/method_memory.py.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

METHOD_EXTRACT = "sw.method.extract"

MAX_METHOD_CHARS = 400

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI personal assistant just finished a task for its owner using tools. Below: the owner's request, the "
    "tool calls it made (with a preview of each result), and what it told the owner. Decide whether the way the "
    "task was done is worth remembering for NEXT time -- a method that was not obvious: a first attempt that "
    "failed and another route that worked, a service reached through a particular window or site, a particular "
    "tool or script or sequence of steps that did the job, a limitation discovered (what does NOT work). "
    "Do NOT remember routine work any assistant would do the same way (reading a file, a plain search, one "
    "direct call that just worked), a task that was not completed, or a method that did not work.\n"
    "If it is worth remembering, write ONE or TWO sentences, in English, in the form \"To <do this kind of "
    "thing>: <what worked>; <what did not work, if known>\", about the KIND of task and in terms of the goal "
    "and the tools/sites/windows used. Never include passwords, keys, tokens, card or account numbers, or "
    "personal details of anyone; names of people and one-off values of this occasion do not belong either -- "
    "the method must be reusable on a different occasion.\n\n"
    "OWNER'S REQUEST:\n{request}\n\n"
    "TOOL CALLS:\n{trace}\n\n"
    "WHAT THE OWNER WAS TOLD:\n{delivered}\n\n"
    'Reply with a JSON object: {{"worth_saving": true|false, "method": "<the sentence(s), or an empty string>"}}'
)


def prompt_method_extract(input: Any, context: Any) -> str:
    trace = "\n".join(str(x)[:300] for x in (input.get("trace") or [])[-25:]) or "(none)"
    return _PROMPT.format(
        request=str(input.get("request") or "")[:1500], trace=trace, delivered=str(input.get("delivered") or "")[:1500],
    )


def verify_method_extract(input: Any, output: Any) -> Optional[str]:
    worth = output.get("worth_saving")
    if not isinstance(worth, bool):
        return '"worth_saving" must be true or false'
    method = output.get("method")
    if not isinstance(method, str):
        return '"method" must be a string'
    method = method.strip()
    if worth and not method:
        return 'when it is worth saving, "method" must hold the sentence'
    if not worth and method:
        return 'when it is not worth saving, "method" must be empty'
    if len(method) > MAX_METHOD_CHARS:
        return f'"method" must be at most {MAX_METHOD_CHARS} characters'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            METHOD_EXTRACT, "1",
            "Decides whether a finished task was done by a non-obvious method worth remembering, and words it.",
            input_schema=_object({"request": _STRING, "trace": {"type": "array", "items": _STRING}, "delivered": _STRING}),
            output_schema=_object({"worth_saving": {"type": "boolean"}, "method": _STRING}),
            build_prompt=prompt_method_extract, verify=verify_method_extract,
        ),
    ]
