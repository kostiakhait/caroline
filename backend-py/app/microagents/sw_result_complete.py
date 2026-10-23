"""sw.result.complete -- does a finished file really contain every element the
task asked for? (2026-10-23, explicit choice after the D1 test.) Seen on the sw
test box: asked for "a short conclusion in one paragraph", nano wrote
doc.add_paragraph("короткий вывод в один абзац") -- the task's own words as the
paragraph -- and every check passed: the facts check had no fact to object to,
and the verifier, shown the file, let it through.

Given the task and the file's text as the system read it (see
small_model_engine._result_evidence), lists each requested element that is
missing, only named instead of written, or duplicated. What it finds goes to the
verifier as ground truth.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

RESULT_COMPLETE = "sw.result.complete"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "Below: a task, and the text of the file that was made for it. Go through what the task asked the file to "
    "contain, element by element, and find every element that is not really there:\n"
    "- MISSING: the file has nothing for it;\n"
    "- ONLY NAMED: the file has the task's own wording, or a label or placeholder, instead of the thing itself "
    "(a \"short conclusion\" asked for, and the paragraph says \"short conclusion\" or \"a conclusion in one "
    "paragraph\" or \"[conclusion]\" -- the conclusion itself is not written);\n"
    "- WRONG: it contradicts the task (other numbers, other order, another title);\n"
    "- DUPLICATED: it is there more than once where the task wanted it once.\n"
    "An element that is present and as asked is not a problem. Judge only the file's content, never how the "
    "file was made.\n\n"
    "TASK:\n{task}\n\n"
    "THE FILE'S CONTENT:\n{content}\n\n"
    'Reply with a JSON object: {{"problems": ["<element>: <missing | only named | wrong | duplicated> -- <one '
    'short sentence>", ...]}} -- an empty list when the file is complete.'
)


def prompt_result_complete(input: Any, context: Any) -> str:
    return _PROMPT.format(task=input.get("task") or "(not given)", content=input.get("content") or "(empty)")


def verify_result_complete(input: Any, output: Any) -> Optional[str]:
    problems = output.get("problems")
    if not isinstance(problems, list) or not all(isinstance(p, str) and p.strip() for p in problems):
        return '"problems" must be a list of non-empty strings (empty when the file is complete)'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            RESULT_COMPLETE, "1",
            "Finds the elements a task asked a file to contain that are missing, only named instead of written, "
            "wrong, or duplicated.",
            input_schema=_object({"task": _STRING, "content": _STRING}),
            output_schema=_object({"problems": {"type": "array", "items": _STRING}}),
            build_prompt=prompt_result_complete, verify=verify_result_complete,
        ),
    ]
