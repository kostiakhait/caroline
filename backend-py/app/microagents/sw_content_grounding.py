"""sw.content.grounding -- are the facts a text states supported by the task and
by what the assistant's tools found? (2026-10-22, explicit instruction after the
D1 test: a Word report was made exactly as asked, but its one-paragraph
conclusion said "this shows steady growth compared with the same period last
year" -- there was no data about last year, and the verifier let it through.)

Step 2 of two (2026-10-23): judges the list of facts that sw.content.extract
(sw_content_extract.py) took out of a changing call that passed sw.action.scope
(see small_model_engine._content_grounded). Facts that follow from the data
given (sums, comparisons between the given numbers) are fine; anything about
things the task and the results do not contain is invented, and the call is not
made until the text is fixed.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

CONTENT_GROUNDING = "sw.content.grounding"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI assistant is about to write or send something, and below are the STATEMENTS OF FACT that the text "
    "contains. Check each against what the assistant actually has: the task the user gave and the results of "
    "its earlier tool calls. Supported: what the task says; what the results show; what follows from those by "
    "plain arithmetic or comparison of the given numbers (a total, a difference, \"August was the highest "
    "month\"); wording the user asked for. INVENTED: a claim about anything neither contains -- earlier "
    "periods, a year before, causes, reasons, trends beyond the given numbers, forecasts, plans, names, dates, "
    "amounts, promises or commitments nobody gave.\n\n"
    "TASK:\n{task}\n\n"
    "EARLIER TOOL CALLS AND THEIR RESULTS:\n{calls}\n\n"
    "STATEMENTS OF FACT IN THE TEXT:\n{facts}\n\n"
    'Reply with a JSON object: {{"grounded": true or false, "invented": "<each invented statement, quoted '
    'briefly, and what is missing for it; empty when grounded>"}}'
)


def prompt_content_grounding(input: Any, context: Any) -> str:
    return _PROMPT.format(
        task=input.get("task") or "(not given)",
        calls="\n".join(input.get("calls") or []) or "(none)",
        facts="\n".join(f"- {f}" for f in (input.get("facts") or [])) or "(none)",
    )


def verify_content_grounding(input: Any, output: Any) -> Optional[str]:
    if not isinstance(output.get("grounded"), bool):
        return '"grounded" must be true or false'
    if not output["grounded"] and not str(output.get("invented") or "").strip():
        return 'when not grounded, "invented" must say which statements'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            CONTENT_GROUNDING, "1",
            "Checks that the statements of fact in what a tool call writes or sends are supported by the task and "
            "the tool results, and names the invented ones.",
            input_schema=_object(
                {"task": _STRING, "facts": {"type": "array", "items": _STRING}, "calls": {"type": "array", "items": _STRING}},
                required=["task", "facts"],
            ),
            output_schema=_object({"grounded": {"type": "boolean"}, "invented": _STRING}, required=["grounded"]),
            build_prompt=prompt_content_grounding, verify=verify_content_grounding,
        ),
    ]
