"""sw.content.grounding -- which of the facts and requests a text states are NOT
supported by the task and by what the assistant's tools found? (2026-10-22,
explicit instruction after the D1 test: a Word report was made exactly as
asked, but its one-paragraph conclusion said "this shows steady growth compared
with the same period last year" -- there was no data about last year, and the
verifier let it through.)

Step 2 of two (2026-10-23): judges the lists that sw.content.extract
(sw_content_extract.py) took out of a changing call that passed sw.action.scope
(see small_model_engine._content_grounded). Facts that follow from the data
given (sums, comparisons between the given numbers) are fine; anything about
things the task and the results do not contain is invented, and so is every
question, request or promise the task did not ask for. The call is not made
until the text is fixed.

The agent only LISTS what is invented; whether the text is clean is decided by
the code (an empty list). In the first version it also gave a verdict, and on
the Ratatosk test it wrote "the promise to send an agenda is absent from the
message" in the reason while answering "grounded: true".

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

CONTENT_GROUNDING = "sw.content.grounding"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI assistant is about to write or send something, and below are the STATEMENTS OF FACT that the "
    "text contains. Check each against what the assistant actually has: the task the user "
    "gave and the results of its earlier tool calls. Supported: what the task says; what the results show; "
    "what follows from those by plain arithmetic or comparison of the given numbers (a total, a difference, "
    "\"August was the highest month\"); wording the user asked for. INVENTED: a claim about anything neither "
    "contains -- earlier periods, a year before, causes, reasons, trends beyond the given numbers, forecasts, "
    "plans, names, dates, amounts, promises or commitments nobody gave.\n\n"
    "TASK:\n{task}\n\n"
    "EARLIER TOOL CALLS AND THEIR RESULTS:\n{calls}\n\n"
    "STATEMENTS OF FACT IN THE TEXT:\n{facts}\n\n"
    'Reply with a JSON object: {{"invented": ["<each invented item, quoted briefly, with what is missing for '
    'it>", ...]}} -- an empty list when every item is supported.'
)


REQUEST_ASKED = "sw.request.asked"

_ASKED_PROMPT = (
    "An AI assistant is about to send a text that contains the question, request, offer or promise below. "
    "Look in the TASK the user gave. Quote the exact words of the task that ask for precisely this "
    "question, request, offer or promise. A task that only says to answer, reply or confirm does not ask the "
    "reply to ask anything new (where, who, when, which) or to promise anything (to send, to prepare, to "
    "call back). If the task has no such words, the quote is an empty string.\n\n"
    "TASK:\n{task}\n\n"
    "THE QUESTION, REQUEST, OFFER OR PROMISE IN THE TEXT:\n{request}\n\n"
    'Reply with a JSON object: {{"quote": "<exact words copied from the task, or empty>"}}'
)


def prompt_request_asked(input: Any, context: Any) -> str:
    return _ASKED_PROMPT.format(task=input.get("task") or "(not given)", request=input.get("request") or "")


def verify_request_asked(input: Any, output: Any) -> Optional[str]:
    if not isinstance(output.get("quote"), str):
        return '"quote" must be a string (empty when the task has no such words)'
    return None


def _squash(text: str) -> str:
    return " ".join(str(text).lower().split())


def asked(task: str, output: dict[str, Any]) -> bool:
    """True when the agent's quote is non-empty and really occurs in the task -- a quote the
    task does not contain is not evidence (decided here, by code, not by the model)."""
    quote = _squash(output.get("quote") or "").strip(" .\"'«»")
    return bool(quote) and quote in _squash(task)


def prompt_content_grounding(input: Any, context: Any) -> str:
    return _PROMPT.format(
        task=input.get("task") or "(not given)",
        calls="\n".join(input.get("calls") or []) or "(none)",
        facts="\n".join(f"- {f}" for f in (input.get("facts") or [])) or "(none)",
    )


def verify_content_grounding(input: Any, output: Any) -> Optional[str]:
    invented = output.get("invented")
    if not isinstance(invented, list) or not all(isinstance(x, str) and x.strip() for x in invented):
        return '"invented" must be a list of non-empty strings (empty when every item is supported)'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            CONTENT_GROUNDING, "3",
            "Lists the statements of fact and the requests in what a tool call writes or sends that are not "
            "supported by the task and the tool results.",
            input_schema=_object(
                {"task": _STRING, "facts": {"type": "array", "items": _STRING}, "calls": {"type": "array", "items": _STRING}},
                required=["task", "facts"],
            ),
            output_schema=_object({"invented": {"type": "array", "items": _STRING}}),
            build_prompt=prompt_content_grounding, verify=verify_content_grounding,
        ),
        M.Agent(
            REQUEST_ASKED, "1",
            "Quotes the words of the task that ask for one given question, request, offer or promise in an outgoing text.",
            input_schema=_object({"task": _STRING, "request": _STRING}),
            output_schema=_object({"quote": _STRING}),
            build_prompt=prompt_request_asked, verify=verify_request_asked,
        ),
    ]
