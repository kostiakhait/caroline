"""sw.result.facts -- what the results of the turn's tool calls say about the statements a changing call is
about to write or send (2026-10-31, explicit instruction: "нам нужен экстрактор фактов, а не обрезка").

sw.content.grounding judged the statements against the turn's calls with each result cut to 200 characters: on
the akinakes writer a document_get showed the id and the title, not the Methods text the facts came from. Cutting
less only moves the limit. Instead, right before the changing call (small_model_engine._content_grounded), this
agent reads each result of the turn whole -- a long one in parts, all in parallel -- and quotes the passages that
bear on the statements sw.content.extract took out of the call. sw.content.grounding then judges the statements
against those quotes.

A quote counts only when it occurs in the part word for word, and the code checks it (kept_quotes), so nothing
the agent says can turn into support that is not in the result. Not refused by verify: on the second real run one
part kept "paraphrasing" until the ladder gave up, and the whole check failed open -- the batch went out unchecked.
Instead (2026-10-31, per explicit instruction) the caller asks once more with the misses named (input "rejected"),
then keeps what matched and drops the rest, logging the dropped text. What counts as word for word (_norm) ignores
what a model copying faithfully still changes: whitespace, case, typographic quotes and dashes, an ellipsis,
Markdown emphasis, and the escapes of a JSON result -- the results are JSON text, a line break inside a string
reads as backslash-n, and a correct quote across it could not match. readable() unwraps that JSON for the agent
in the first place.

Client-side only: run through the local microagent ladder."""

from __future__ import annotations

import ast
import json
import re
from typing import Any, List, Optional

from app.microagent_client import Microagent as M

RESULT_FACTS = "sw.result.facts"

# One model call reads at most this much of a result; a longer result is read in overlapping parts, all of them.
PART_CHARS = 8000
PART_OVERLAP = 300

_STRING = {"type": "string"}


def _object(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required}


def parts(text: str) -> list[str]:
    """The whole text, in parts of PART_CHARS that overlap by PART_OVERLAP so a sentence on a border is whole in one."""
    if len(text) <= PART_CHARS:
        return [text]
    step = PART_CHARS - PART_OVERLAP
    return [text[i:i + PART_CHARS] for i in range(0, len(text) - PART_OVERLAP, step)]


_PROMPT = (
    "An AI assistant is about to write or send a text, and below are the STATEMENTS that text makes. Below them "
    "is the RESULT of one tool call the assistant made earlier (or one part of a long result). For each statement, "
    "find the passages of the RESULT that state it, or state the data it is based on (the figures a total or a "
    "comparison is made from). Copy each passage WORD FOR WORD from the RESULT -- a sentence or a short run of "
    "sentences, never paraphrased, never shortened inside. A statement the RESULT says nothing about gets no "
    "passage; most results bear on few statements or none.\n\n"
    "STATEMENTS:\n{statements}\n\n"
    "RESULT of {tool}:\n{result}\n\n{rejected}"
    'Reply with a JSON object: {{"found": [{{"statement": <its number>, "quote": "<passage copied from the '
    'RESULT>"}}, ...]}} -- an empty list when the RESULT bears on none of them.'
)


def prompt_result_facts(input: Any, context: Any) -> str:
    statements = "\n".join(f"{i}. {s}" for i, s in enumerate(input.get("statements") or [], 1))
    rejected = "".join(f"- {q}\n" for q in input.get("rejected") or [])
    if rejected:
        rejected = ("Your previous answer gave these passages, and they are NOT in the RESULT word for word -- copy "
                    "the real wording of the RESULT instead, or leave them out:\n" + rejected + "\n")
    return _PROMPT.format(statements=statements or "(none)", tool=input.get("tool") or "", result=input.get("result") or "",
                          rejected=rejected)


def readable(value: Any, _depth: int = 0) -> str:
    """A tool result as text a model can quote from: JSON (or a Python literal) unwrapped into "key: value" lines,
    nested JSON strings too, so a line break is a line break and not backslash-n. Anything else is left as it is."""
    if _depth > 6:
        return str(value)
    if isinstance(value, dict):
        return "\n".join(f"{k}: {readable(v, _depth + 1)}" for k, v in value.items())
    if isinstance(value, list):
        return "\n".join(readable(v, _depth + 1) for v in value)
    if not isinstance(value, str):
        return str(value)
    text = value.strip()
    if text[:1] in ("{", "[", '"'):
        try:
            parsed, end = json.JSONDecoder(strict=False).raw_decode(text)  # a raw line break in a string is fine
        except ValueError:
            try:
                parsed, end = ast.literal_eval(text), len(text)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                return value
        if isinstance(parsed, (dict, list, str)) and parsed != text:
            rest = text[end:].strip()
            return readable(parsed, _depth + 1) + (("\n" + rest) if rest else "")
    return value


# A JSON escape as it reads in the text, and what a model copying it writes instead.
_ESCAPES = {"\\n": " ", "\\r": " ", "\\t": " ", '\\"': '"', "\\'": "'", "\\/": "/", "\\\\": "\\"}
_TYPOGRAPHY = str.maketrans({
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"', "‘": "'", "’": "'",
    "‚": "'", "′": "'", "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-",
    " ": " ", " ": " ", "*": None, "_": None, "`": None,
})


def _norm(text: str) -> str:
    """What "word for word" compares: see the module docstring."""
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    for escape, plain in _ESCAPES.items():
        text = text.replace(escape, plain)
    text = text.translate(_TYPOGRAPHY).replace("…", "...")
    return re.sub(r"\s+", " ", text).strip().lower()


def verify_result_facts(input: Any, output: Any) -> Optional[str]:
    found = output.get("found")
    if not isinstance(found, list):
        return '"found" must be a list (empty when the result bears on none of the statements)'
    count = len(input.get("statements") or [])
    for item in found:
        if not isinstance(item, dict) or not isinstance(item.get("statement"), int) or not 1 <= item["statement"] <= count:
            return f'each item needs "statement": a number from 1 to {count}'
        if not str(item.get("quote") or "").strip():
            return 'each item needs a non-empty "quote"'
    return None


def kept_quotes(input: Any, output: Any) -> tuple[list[str], list[str]]:
    """The quotes that really are in the part, and the ones that are not."""
    result = _norm(str(input.get("result") or ""))
    quotes = [str(item.get("quote") or "") for item in output.get("found") or [] if str(item.get("quote") or "").strip()]
    kept = [q for q in quotes if _norm(q) in result]
    return kept, [q for q in quotes if q not in kept]


def llm_agents() -> List["M.Agent"]:
    return [
        M.Agent(
            RESULT_FACTS, "3",
            "Quotes, word for word, the passages of one tool result (or one part of it) that state the given "
            "statements or the data they are based on.",
            input_schema=_object({"tool": _STRING, "result": _STRING, "statements": {"type": "array", "items": _STRING},
                                  "rejected": {"type": "array", "items": _STRING}}, ["tool", "result", "statements"]),
            output_schema=_object({"found": {"type": "array", "items": _object({"statement": {"type": "integer"}, "quote": _STRING})}}),
            build_prompt=prompt_result_facts, verify=verify_result_facts,
        ),
    ]
