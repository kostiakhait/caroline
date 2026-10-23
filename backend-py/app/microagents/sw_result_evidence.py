"""sw.result.evidence -- which files a task was supposed to create or change
(2026-10-23, explicit choice after the D1 test): the verifier has no tools, and
asked "is the file right?" it said "never built, not saved" about a correct
document, because nobody had read the file back. The system now reads it for
it: this agent only names the files, the code reads them (read_file, which
renders Word / Excel / PowerPoint as text) and puts what is in them before the
verifier as plain fact.

Names only paths that occur in the task or in the calls so far -- the code
drops anything else, so a guessed path is never read.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

RESULT_EVIDENCE = "sw.result.evidence"

MAX_FILES = 3

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "Below: a task an AI assistant was given, and the tool calls it made so far. List the files the task was "
    "supposed to CREATE or CHANGE (a report, a spreadsheet, a script, a saved document) and that the calls "
    "touched or should have touched -- the paths exactly as they appear in the task or in the calls. A file "
    "that was only read, or only mentioned in passing, is not on the list; a task that makes no file leaves "
    "the list empty. At most {limit} paths.\n\n"
    "TASK:\n{task}\n\n"
    "CALLS SO FAR:\n{calls}\n\n"
    'Reply with a JSON object: {{"files": ["<path>", ...]}}'
)


def prompt_result_evidence(input: Any, context: Any) -> str:
    return _PROMPT.format(
        limit=MAX_FILES, task=input.get("task") or "(not given)", calls="\n".join(input.get("calls") or []) or "(none)",
    )


def verify_result_evidence(input: Any, output: Any) -> Optional[str]:
    files = output.get("files")
    if not isinstance(files, list) or not all(isinstance(f, str) and f.strip() for f in files):
        return '"files" must be a list of non-empty path strings (empty when the task makes no file)'
    if len(files) > MAX_FILES:
        return f"at most {MAX_FILES} paths"
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            RESULT_EVIDENCE, "1",
            "Names the files a task was supposed to create or change, from the task and the calls made.",
            input_schema=_object({"task": _STRING, "calls": {"type": "array", "items": _STRING}}, required=["task"]),
            output_schema=_object({"files": {"type": "array", "items": _STRING}}),
            build_prompt=prompt_result_evidence, verify=verify_result_evidence,
        ),
    ]
