"""sw.attempt.progress -- per explicit instruction (2026-10-22): one narrow
question, asked when the verifier finds a tier's attempt unfinished and the
tier has already been retried: did this attempt move the task forward?
Yes -> the same tier tries again; no -> the task goes to the next tier.

Seen on the sw test box the same day: "a call not made before" counted as
progress, and nano kept searching folders that do not exist (Sent, Archive),
each empty search a "new" call, never escalating -- the letter it looked for
was in Junk, which one email_list_folders would have shown.

Client-side only, same as sw_turn_summary.py: run through
app/microagent_client.py's local Runtime (the microagent ladder), never
deployed to Camerlengo."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

ATTEMPT_PROGRESS = "sw.attempt.progress"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI assistant is working on a task with tools. It has made several attempts; the latest one is still "
    "not finished. Decide one thing: did the LATEST attempt move the task forward?\n\n"
    "Progress means the latest attempt got something real that the earlier attempts did not have and that "
    "brings the task closer to done: a found item, new content read, a result produced, a step completed, "
    "or a real new fact about where to look.\n"
    "NOT progress: calls that came back empty, with an error, or with what was already known; the same "
    "search again with other words or in other places that turned up nothing; reports, summaries, saved "
    "notes or questions to the user without new results behind them.\n\n"
    "TASK:\n{task}\n\n"
    "EARLIER ATTEMPTS (tool calls and their results):\n{earlier}\n\n"
    "LATEST ATTEMPT (tool calls and their results):\n{latest}\n\n"
    'Reply with a JSON object: {{"progress": true or false, "new": "<one sentence: what the latest attempt '
    'got that is new, or why it got nothing>"}}'
)


def _lines(items: Any) -> str:
    return "\n".join(str(x) for x in (items or [])) or "(none)"


def prompt_attempt_progress(input: Any, context: Any) -> str:
    return _PROMPT.format(
        task=input.get("task") or "(not given)",
        earlier=_lines(input.get("earlier")),
        latest=_lines(input.get("latest")),
    )


def verify_attempt_progress(input: Any, output: Any) -> Optional[str]:
    if not isinstance(output.get("progress"), bool):
        return '"progress" must be true or false'
    if not str(output.get("new") or "").strip():
        return '"new" must say in one sentence what is new, or why nothing is'
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            ATTEMPT_PROGRESS, "1",
            "Decides whether the latest attempt at a tool-using task got something real and new that moves the "
            "task forward, compared with the earlier attempts.",
            input_schema=_object(
                {
                    "task": _STRING,
                    "earlier": {"type": "array", "items": _STRING},
                    "latest": {"type": "array", "items": _STRING},
                },
                required=["task", "latest"],
            ),
            output_schema=_object({"progress": {"type": "boolean"}, "new": _STRING}, required=["progress", "new"]),
            build_prompt=prompt_attempt_progress, verify=verify_attempt_progress,
        ),
    ]
