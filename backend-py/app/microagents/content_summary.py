"""content.summarize -- a microagent that summarizes one arbitrary piece of
text (an email body, a document excerpt, a chat-transcript chunk, anything)
down to roughly a target length.

Added (2026-10-16), per explicit instruction, as the companion to that same
day's context-overflow pass: app/context_cap.py (Caroline, client-side)
truncates anything too long to a raw PREFIX window -- useful when the exact
wording matters, but a blunt instrument when the model only needs the GIST
of something long (a long email thread, an old dehydrated conversation
chunk) and would rather spend one cheap model call than read it piece by
piece. Caroline's summarize_content tool (files_plugin.py) calls this for
that case.

No existing microagent fits: memory.topics.summarize (TopicMemory.py) needs
a prior record + pre-segmented episodes; memory.documents.card/digest
(DocumentMemory.py) need a document's own already-computed part synopses.
Both are shaped around their own domain's prior state, not a bare string.
This one takes exactly that: text in, target_chars, summary out -- nothing
else.

Same shape as ReplyExpected.py/ActionSummary.py -- registered into the SAME
shared reply: Runtime Api2ReplyCommands.py already builds (its own
REPLY_MAX_ACTIVE_AGENTS-sized pool, 2026-10-16), its own command
(`reply:summarize`), its own billing label. Not a memory: agent -- it has
no opinion on what's worth remembering, only on compressing whatever text
it's handed; reply: already hosts other general-purpose cheap text
utilities (ActionSummary, ReplyTranslate), not just safety judgments."""

from __future__ import annotations

from typing import Any, List, Optional

from app.microagent_client import Microagent as M

SUMMARIZE = "content.summarize"

_STRING = {"type": "string"}

DEFAULT_TARGET_CHARS = 1000
# A summary longer than its own target is still useful -- verify_summarize
# below only rejects a reply that's barely shorter than the ORIGINAL text
# (i.e. not actually a summary at all), not one that overshot target_chars
# by some margin.
_MIN_COMPRESSION_RATIO = 0.8


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


# Evolution-engine genome (2026-10-17): see PermissionGuard.py's own
# GUARD_RULES_TEXT comment for the split's rationale; `{{TARGET_CHARS}}`
# is a literal placeholder (plain .replace(), never .format()), see
# BackgroundRelayWorth.py's own `{{TASK_CLAUSE}}` for the same pattern.
SUMMARIZE_RULES_TEXT = (
    "Summarize the text below in about {{TARGET_CHARS}} characters -- keep the concrete facts, decisions, "
    "names, numbers, and dates that someone would actually need; drop pleasantries, repetition, and "
    "filler. Write it as plain prose a person could act on without reading the original, in the same "
    "language the original is written in. Never invent anything not actually in the text.\n\n"
)


def _data_section(input: Any) -> str:
    return f"TEXT:\n{input['text']}"


def prompt_summarize(input: Any, context: Any) -> str:
    target = int(input.get("target_chars") or DEFAULT_TARGET_CHARS)
    rules = SUMMARIZE_RULES_TEXT.replace("{{TARGET_CHARS}}", str(target))
    return rules + _data_section(input)


def verify_summarize(input: Any, output: Any) -> Optional[str]:
    summary = (output.get("summary") or "").strip()
    if not summary:
        return '"summary" must be a non-empty string'
    original = input.get("text") or ""
    if original and len(summary) > len(original) * _MIN_COMPRESSION_RATIO:
        return "the reply is barely shorter than the original text -- summarize it, don't just restate it"
    return None


def llm_agents() -> List[M.Agent]:
    return [
        M.Agent(
            SUMMARIZE, "1",
            "Summarizes one arbitrary piece of text to roughly a target length, keeping the concrete facts "
            "and dropping the rest -- for when the gist is enough and the full text isn't worth reading.",
            input_schema=_object({"text": _STRING, "target_chars": {"type": "integer"}}, required=["text"]),
            output_schema=_object({"summary": _STRING}, required=["summary"]),
            build_prompt=prompt_summarize, verify=verify_summarize,
        ),
    ]
