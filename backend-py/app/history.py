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

# Moved here from chat_session.py (2026-09-10) so this module -- the one
# that actually knows the raw content-block shape _push_message writes --
# can use it to find submit()-boundary stamps itself (see
# _split_into_submessages below). chat_session.py still imports this same
# instance for its own stamp-stripping needs.
_HISTORY_STAMP_PATTERN = re.compile(r"^\[(Sent: |(Sun|Mon|Tue|Wed|Thu|Fri|Sat), )[^\]]*\]\s*", re.IGNORECASE)


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


def _split_user_submessages(content: Any) -> list[Any]:
    """Bug fix (2026-09-10): confirmed live -- when multiple submit() calls
    (chat_session.py) queue up faster than the CLI drains the queue (always
    true right after a WS reconnect, when a startup greeting and a
    crash-resume nudge both fire back-to-back), the CLI can combine them
    into ONE "user" JSONL entry whose content list is just every
    submit()'s own blocks concatenated. _push_message always starts one
    logical submit() with a "[Sent: ...]"/weekday-stamp text block, so that
    stamp is a reliable per-submit() boundary -- split back into the
    original groups here, BEFORE text extraction, so each can be judged
    synthetic-vs-real independently. Without this, one flattened blob
    inherited whichever verdict its FIRST part deserved: a real user
    message that happened to queue behind a synthetic nudge (an edge case,
    but a real one -- confirmed by a live test where a message sent right
    after reconnect merged with the startup-greeting nudge) was being
    silently dropped along with the nudge it got stuck behind."""
    if not isinstance(content, list):
        return [content]
    groups: list[list[Any]] = []
    current: list[Any] = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) and _HISTORY_STAMP_PATTERN.match(b["text"]):
            if current:
                groups.append(current)
            current = [b]
        else:
            current.append(b)
    if current:
        groups.append(current)
    return groups or [content]


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
            content = (obj.get("message") or {}).get("content")
            # Bug fix (2026-09-10): for "user" entries only, undo a possible
            # multi-submit() merge before doing anything else -- see
            # _split_user_submessages's own docstring. Assistant entries
            # come straight from the SDK's own single response stream, one
            # per API round-trip, and are never merged this way.
            content_groups = _split_user_submessages(content) if obj.get("type") == "user" else [content]
            for group in content_groups:
                # Bug fix (2026-09-10): a message with a tool_use block
                # alongside text is, by construction, NOT the turn's final
                # answer -- one SDK "assistant" message is one API
                # round-trip with a single stop_reason, so any text
                # alongside a tool_use is pre-tool narration ("let me check
                # X", agentic self-talk), never something addressed to the
                # user. This mirrors chat.js's own live-rendering rule (its
                # hasToolUse check) exactly -- confirmed that rule already
                # exists client-side (2026-09-08 incident), but this
                # rebuild-from-disk path never replicated it, so a
                # reconnect/get_history replay (and the phone companion
                # mirror, which draws from this same function) could still
                # show internal tool narration as if it were a real chat
                # bubble.
                if obj.get("type") == "assistant" and isinstance(group, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_use" for b in group
                ):
                    continue
                text, attachments = _extract_text_and_attachments(group)
                if not text.strip() and not attachments:
                    continue
                # Bug fix (2026-09-09): this rebuild path is entirely
                # separate from the live sdk_message stream's own
                # [[NO_UPDATE]] suppression (see chat.js's assistant-message
                # handler) -- confirmed live, a no-update turn's full text
                # (explanation + trailing sentinel) was leaking into the
                # visible chat every time a tab reconnected and replayed
                # history via get_history, even after the live-path fix.
                # Checked as a substring, not exact equality, same reasoning
                # as the client-side fix: the model doesn't always reply
                # with ONLY the sentinel. Filtered here (server side, the
                # single source both read_recent_history and
                # read_archived_entries draw from) rather than only in
                # chat.js's get_history handler, so a no-update turn never
                # even reaches the client as part of the user-visible
                # transcript.
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
