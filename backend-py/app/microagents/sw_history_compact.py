"""sw.history.compact -- per explicit instruction (2026-10-22), Phase 2b of
docs/SW_MODE_TESTING_PLAN.md: the sw engine's equivalent of the Claude SDK's
compaction. Once an sw conversation's history outgrows its budget
(app/sw_history.py's COMPACT_THRESHOLD_TOKENS), the older turns are folded,
together with the previous compressed summary, into one new summary the
following turns carry instead of those turns themselves; the newest turns
always stay verbatim.

Client-side only, same as sw_turn_summary.py: run through
app/microagent_client.py's local Runtime, never deployed to Camerlengo."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

HISTORY_COMPACT = "sw.history.compact"

_STRING = {"type": "string"}

_SECTIONS = ("Done", "Decided", "Still open", "User's standing instructions", "Links that still matter")


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


_PROMPT = (
    "You are compressing the older part of a conversation between a user and their AI assistant, so the "
    "assistant can keep working with full awareness of it while those turns are no longer shown in full. "
    "Merge the PREVIOUS SUMMARY (if any) with the TURNS below into ONE new summary that replaces both.\n\n"
    "Use exactly these sections, in this order, as plain headings:\n"
    "Done -- what has been completed, with concrete outcomes.\n"
    "Decided -- choices made and agreements reached, by the user or together.\n"
    "Still open -- unfinished tasks, pending questions, promised follow-ups.\n"
    "User's standing instructions -- preferences and rules the user set that still apply (how to address "
    "them, what never to do, formats, approvals required).\n"
    "Links that still matter -- file links from the tool-work records that later turns may need to open, "
    "each with a few words on what it holds.\n\n"
    "Keep exact identifiers (paths, names, addresses, ids, numbers, dates). Copy names, code words, quoted "
    "text and any value the user gave EXACTLY as the user wrote them, in their original language and script "
    "-- never translate or transliterate them, even if a tool-work record already did (the user's own words "
    "in the turns are the authority). Drop pleasantries, repetition and anything fully superseded. Never "
    "invent anything. Write the section contents in the language the user writes in; keep links exactly as "
    "given.\n\n"
    "PREVIOUS SUMMARY:\n{previous_summary}\n\n"
    "TURNS (oldest first):\n{turns}\n\n"
    "Reply now."
)


def _render_turns(turns: list[dict[str, Any]]) -> str:
    blocks = []
    for i, t in enumerate(turns, 1):
        block = f"--- Turn {i} ---\nUser: {t.get('user') or ''}"
        if t.get("tools"):
            block += f"\nTool work: {t['tools']}"
        block += f"\nAssistant: {t.get('answer') or '(no visible reply)'}"
        blocks.append(block)
    return "\n\n".join(blocks) or "(none)"


def prompt_history_compact(input: Any, context: Any) -> str:
    return _PROMPT.format(
        previous_summary=input.get("previous_summary") or "(none -- this is the first compaction)",
        turns=_render_turns(input.get("turns") or []),
    )


def verify_history_compact(input: Any, output: Any) -> Optional[str]:
    summary = (output.get("summary") or "").strip()
    if not summary:
        return '"summary" must be a non-empty string'
    missing = [s for s in _SECTIONS if s.lower() not in summary.lower()]
    if missing:
        return f"the summary must use all of these section headings: {', '.join(_SECTIONS)} (missing: {', '.join(missing)})"
    return None


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            HISTORY_COMPACT, "1",
            "Folds the older turns of a conversation, plus its previous summary, into one structured summary "
            "(done / decided / still open / user's standing instructions / links that still matter) that "
            "later turns carry instead of those turns.",
            input_schema=_object(
                {"previous_summary": _STRING, "turns": {"type": "array", "items": {"type": "object"}}},
                required=["turns"],
            ),
            output_schema=_object({"summary": _STRING}, required=["summary"]),
            build_prompt=prompt_history_compact, verify=verify_history_compact,
        ),
    ]
