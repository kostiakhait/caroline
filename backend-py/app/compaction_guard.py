"""Compaction that compacts, not clears (2026-10-26, explicit instruction).

The CLI's /compact has the MODEL write the summary of the older conversation, under Caroline's own system prompt -- which
tells it to answer internal messages with [[NO_UPDATE]]. Twice on 2026-10-26 (16:11, two tabs) the "summary" was literally
"[[NO_UPDATE]]", once (04:12) a greeting; the context fell from 50 748 to 7 651 tokens, a draft waiting for the user's
approval was gone, and Caroline logged "success". This module holds the three parts of the cure:

  1. the instruction the compaction is given (COMPACT_COMMAND), so it is a summarization task and not a chat turn;
  2. the check of what came out (last_summary / is_degenerate), read from the session file after the boundary;
  3. the recovery (digest_before_boundary + recovery_block): when the summary is unusable, what the conversation held
     before the boundary is rebuilt from the file and handed to the model with the next message."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SUMMARY_MIN_CHARS = 600
SENTINEL = "[[NO_UPDATE]]"
CONTINUATION_PREFIX = "This session is being continued"
DIGEST_MAX_CHARS = 60_000
TOOL_TEXT_CHARS = 300
READ_TAIL_BYTES = 40_000_000

COMPACT_INSTRUCTIONS = (
    "This is a summarization task, not a chat turn. Write a real, detailed summary of the conversation in your usual structure "
    "(primary request and intent, key facts, what was done, pending and open items, current work, next step), several paragraphs "
    "long. NEVER answer with [[NO_UPDATE]], a greeting or any other short reply, whatever your instructions say about replies "
    "to internal messages. Keep VERBATIM: every draft message or email that was shown to the user and is awaiting their approval "
    "or decision (full text and recipient); every action already done (what was sent, to whom, when); the user's last request and "
    "what was done about it; all names, addresses, numbers and dates that matter."
)
COMPACT_COMMAND = "/compact " + COMPACT_INSTRUCTIONS


def _read_rows(path: Path) -> list[dict[str, Any]]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > READ_TAIL_BYTES:
                f.seek(size - READ_TAIL_BYTES)
                f.readline()
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _text_of(row: dict[str, Any]) -> str:
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _last_boundary(rows: list[dict[str, Any]]) -> int | None:
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].get("type") == "system" and rows[i].get("subtype") == "compact_boundary":
            return i
    return None


def last_summary(path: Path) -> tuple[str | None, int | None, list[dict[str, Any]]]:
    """(the summary text after the last compaction boundary, the boundary's row index, all rows). None when the file has
    no boundary or no continuation message after it."""
    rows = _read_rows(path)
    boundary = _last_boundary(rows)
    if boundary is None:
        return None, None, rows
    for row in rows[boundary + 1: boundary + 8]:
        if row.get("type") == "user":
            text = _text_of(row).strip()
            if text.startswith(CONTINUATION_PREFIX):
                body = text.split("</tools-used-before-summary>")[-1]
                body = re.split(r"\n\s*If you need specific details", body)[0]
                return body.strip(), boundary, rows
    return None, boundary, rows


def is_degenerate(summary: str | None) -> bool:
    """True when what came out is not a summary: a placeholder, a greeting, something far too short."""
    body = (summary or "").strip()
    # A real summary may MENTION the sentinel (it describes how the assistant replies to internal messages); a placeholder
    # starts with it.
    return len(body) < SUMMARY_MIN_CHARS or body.startswith(SENTINEL)


def digest_before_boundary(rows: list[dict[str, Any]], boundary: int, max_chars: int = DIGEST_MAX_CHARS) -> str:
    """The conversation before the boundary as plain text, newest last, cut from the OLD end: what the user wrote, what the
    assistant said, and the tool calls with short results. Internal service messages are left out."""
    lines: list[str] = []
    for row in rows[:boundary]:
        kind = row.get("type")
        message = row.get("message") or {}
        content = message.get("content")
        if kind == "user":
            text = _text_of(row).strip()
            if not text or "caroline-internal-turn" in text or text.startswith(("<command", "<local-command", CONTINUATION_PREFIX)):
                continue
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        lines.append("  -> " + str(b.get("content"))[:TOOL_TEXT_CHARS].replace("\n", " "))
                continue
            lines.append("USER: " + re.sub(r"^\[Sent:[^\]]*\]\s*", "", text))
        elif kind == "assistant":
            text = _text_of(row).strip()
            if text and SENTINEL not in text:
                lines.append("ASSISTANT: " + text)
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        lines.append(f"  [tool] {b.get('name')} {json.dumps(b.get('input'), ensure_ascii=False)[:TOOL_TEXT_CHARS]}")
    digest = "\n".join(lines)
    return digest if len(digest) <= max_chars else "..." + digest[-max_chars:]


def recovery_block(summary: str, state: str) -> str:
    """The text handed to the model with its next message when the compaction summary was unusable."""
    parts = [
        "[Context recovery. The automatic summary of the earlier conversation was lost when the context was compacted. "
        "The system rebuilt it from the exact record: treat it as what you remember of the conversation before this point.]"
    ]
    if summary.strip():
        parts.append("WHAT HAPPENED BEFORE (rebuilt summary):\n" + summary.strip())
    if state.strip():
        parts.append(state.strip())
    return "\n\n".join(parts)
