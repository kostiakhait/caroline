"""Ports backend/src/history.ts -- rebuilds the visible chat transcript from
the real Claude Code CLI session transcript (~/.claude/projects/<sanitized-
workspace-path>/*.jsonl), independent of the chat UI's own localStorage-based
echo. Emergency recovery path: the chat UI calls "get_history" once on
startup if its own localStorage transcript is empty (see chat.js), and
"expand_dehydrated_ref" reuses read_archived_entries to turn one of
dehydrate.py's archive files back into the same shape for a human to read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.logging_setup import log_event

_ATTACHMENT_NOTE_PREFIXES = [
    "[This image is also saved at ",
    "[This document is also saved at ",
    "[Attached file saved to ",
]

_UUID_PREFIX_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-", re.IGNORECASE,
)


def _sanitize_project_dir_name(path: str) -> str:
    return re.sub(r"[\\:]", "-", path)


def _extract_attachment_note(block_text: str) -> dict[str, str] | None:
    for prefix in _ATTACHMENT_NOTE_PREFIXES:
        if not block_text.startswith(prefix):
            continue
        rest = block_text[len(prefix):]
        dash_idx = rest.find(" -- ")
        saved_path = (rest[:dash_idx] if dash_idx >= 0 else re.sub(r"\.?\]\s*$", "", rest)).strip()
        base = re.split(r"[\\/]", saved_path)[-1] or saved_path
        name = _UUID_PREFIX_RE.sub("", base)
        return {"name": name}
    return None


def _latest_session_file(workspace_dir: str) -> Path | None:
    project_dir = Path.home() / ".claude" / "projects" / _sanitize_project_dir_name(workspace_dir)
    if not project_dir.exists():
        return None
    files = [f for f in project_dir.iterdir() if f.suffix == ".jsonl"]
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_mtime)


def _extract_text_and_attachments(content: Any) -> tuple[str, list[dict[str, str]]]:
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []
    text_parts: list[str] = []
    attachments: list[dict[str, str]] = []
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "text" or not isinstance(b.get("text"), str):
            continue
        note = _extract_attachment_note(b["text"])
        if note:
            attachments.append(note)
        else:
            text_parts.append(b["text"])
    return "\n\n".join(text_parts), attachments


def _extract_entries_from_jsonl(raw: str, source_label: str) -> list[dict[str, Any]]:
    import time as _time

    entries: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if obj.get("type") not in ("user", "assistant"):
                continue
            text, attachments = _extract_text_and_attachments((obj.get("message") or {}).get("content"))
            if not text.strip() and not attachments:
                continue
            # Bug fix (2026-09-09): this rebuild path is entirely separate
            # from the live sdk_message stream's own [[NO_UPDATE]]
            # suppression (see chat.js's assistant-message handler) --
            # confirmed live, a no-update turn's full text (explanation +
            # trailing sentinel) was leaking into the visible chat every
            # time a tab reconnected and replayed history via
            # get_history, even after the live-path fix. Checked as a
            # substring, not exact equality, same reasoning as the
            # client-side fix: the model doesn't always reply with ONLY
            # the sentinel. Filtered here (server side, the single source
            # both read_recent_history and read_archived_entries draw
            # from) rather than only in chat.js's get_history handler, so
            # a no-update turn never even reaches the client as part of
            # the user-visible transcript.
            if obj.get("type") == "assistant" and "[[NO_UPDATE]]" in text:
                continue
            ts_raw = obj.get("timestamp")
            ts_ms: float | None = None
            if ts_raw:
                try:
                    from datetime import datetime
                    ts_ms = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp() * 1000
                except Exception:
                    ts_ms = None
            entry: dict[str, Any] = {
                "role": obj["type"], "text": text,
                "ts": ts_ms if ts_ms is not None else _time.time() * 1000,
            }
            if attachments:
                entry["attachments"] = attachments
            entries.append(entry)
        except Exception as exc:
            log_event("engine", "read_history_skipped_malformed_line", source=source_label, error=str(exc))
    return entries


def read_recent_history(workspace_dir: str, limit: int = 200) -> list[dict[str, Any]]:
    file = _latest_session_file(workspace_dir)
    if not file:
        return []
    entries = _extract_entries_from_jsonl(file.read_text(encoding="utf-8"), str(file))
    return entries[-limit:]


def read_archived_entries(file_path: str) -> list[dict[str, Any]]:
    return _extract_entries_from_jsonl(Path(file_path).read_text(encoding="utf-8"), file_path)
