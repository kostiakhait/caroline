"""sw.turn.tools.summarize -- per explicit instruction (2026-10-22), Phase 2b
of docs/SW_MODE_TESTING_PLAN.md: after an sw turn that called tools, writes
a compact record of that tool work for the model's own memory in LATER
turns -- what it looked at, created, changed, sent, found -- each line
linking to the file that holds the call's full arguments and result
(app/sw_history.py writes those). The next turn's context carries this
record instead of the raw calls; the model opens a linked file when it
needs the full detail.

Client-side only, same as delivery_completeness.py/action_sanction.py: a
Caroline-specific agent run through app/microagent_client.py's local
Runtime, never deployed to Camerlengo."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

TURN_TOOLS_SUMMARY = "sw.turn.tools.summarize"

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "An AI assistant just finished one turn of a conversation with its user. During the turn it called "
    "the tools listed below. Write a compact record of that tool work for the assistant's OWN memory: in "
    "later turns it will see this record instead of the raw calls, and must be able to tell from it what "
    "it already looked at, created, changed, sent or found -- so it never redoes work or contradicts "
    "itself.\n\n"
    "Rules:\n"
    "- One line per meaningful action, in the order they happened. Merge repeated or trivial calls "
    "(retries, listing a folder before reading a file in it) into the line they served. Skip pure "
    "bookkeeping (planning, checking an operation's status) unless its result matters.\n"
    "- Keep exact identifiers: file paths, names, email addresses, message/thread ids, numbers, dates, "
    "and the concrete outcome (what was found, what was written, what failed and why).\n"
    "- End every line with the LINK of the call(s) it describes, in parentheses, exactly as given -- the "
    "assistant opens that file when it needs the full arguments and result.\n"
    "- Plain facts only, no commentary, nothing that isn't in the calls. Write in English, but copy names, "
    "code words, file names, quoted text and any value the user gave EXACTLY as written, in their original "
    "language and script -- never translate or transliterate them (\"Кассиопея\" stays \"Кассиопея\").\n\n"
    "USER'S MESSAGE THIS TURN:\n{user_text}\n\n"
    "ASSISTANT'S FINAL REPLY THIS TURN:\n{answer}\n\n"
    "TOOL CALLS (in order; args and result are shortened previews -- the link holds the full record):\n"
    "{calls}\n\n"
    "Reply now."
)


def _render_calls(calls: list[dict[str, Any]]) -> str:
    lines = []
    for c in calls:
        lines.append(
            f"#{c.get('n')} {c.get('tool')}\n  args: {c.get('args')}\n  result: {c.get('result')}\n  link: {c.get('ref')}"
        )
    return "\n".join(lines) or "(none)"


def prompt_turn_tools_summary(input: Any, context: Any) -> str:
    return _PROMPT.format(
        user_text=input.get("user_text") or "(empty)",
        answer=input.get("answer") or "(no visible reply)",
        calls=_render_calls(input.get("calls") or []),
    )


def verify_turn_tools_summary(input: Any, output: Any) -> Optional[str]:
    summary = (output.get("summary") or "").strip()
    if not summary:
        return '"summary" must be a non-empty string'
    refs = [c.get("ref") for c in (input.get("calls") or []) if c.get("ref")]
    if refs and not any(ref in summary for ref in refs):
        return "every line must end with the link of the call(s) it describes, exactly as given"
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            TURN_TOOLS_SUMMARY, "1",
            "Turns one finished turn's tool calls into a compact, linked record of what was looked at, created, "
            "changed, sent or found -- the assistant's memory of its own tool work in later turns.",
            input_schema=_object(
                {
                    "user_text": _STRING,
                    "answer": _STRING,
                    "calls": {"type": "array", "items": {"type": "object"}},
                },
                required=["calls"],
            ),
            output_schema=_object({"summary": _STRING}, required=["summary"]),
            build_prompt=prompt_turn_tools_summary, verify=verify_turn_tools_summary,
        ),
    ]
