"""Catches a reply agent that returned the output-format instructions as part of its text.

Microagent.py appends OUTPUT_FORMAT_TEMPLATE ("Reply with ONE JSON value ... It must match this JSON Schema: {...}")
right after the agent's own data section. Microagent.find_prompt_leak looks for the template's English first sentence
only, so a translator that translates the template along with TEXT gets through (2026-11-19, live: the mail report
reached the owner ending with "---" and the template in Russian, schema and all). The schema itself is copied
verbatim in any language, and so is the "---" line that closes TEXT; both are checked against the input text, so a
reply that legitimately quotes a schema or uses "---" is not rejected.
"""

from __future__ import annotations

import json
from typing import Optional


def _schema_text(output_schema: dict) -> str:
    return json.dumps(output_schema, ensure_ascii=False)


def _rule_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() == "---")


def format_echo(input_text: str, output_text: str, output_schema: dict) -> Optional[str]:
    """The reason to reject `output_text`, or None when it does not repeat the format instructions."""
    schema = _schema_text(output_schema)
    if schema in output_text and schema not in input_text:
        return ('"text" repeats the output-format instructions (the JSON Schema) -- return only the text itself, '
                "never the instructions about how to reply, in any language")
    if _rule_lines(output_text) > _rule_lines(input_text):
        return ('"text" contains a "---" line that is not in TEXT -- the "---" lines only mark where TEXT begins and '
                "ends; return what is between them, nothing after the closing one")
    return None
