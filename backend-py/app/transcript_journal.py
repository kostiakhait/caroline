"""Append-only journal of every chat entry a tab has shown or had in its session transcript (2026-10-30).

After a real incident: the chat window keeps its transcript in one localStorage blob, and the backend's copy
(companion-visible-N.json) is rewritten from whatever that blob holds, so a blob that lost the day's entries
silently became the only copy the window could restore. The history rebuilt from the Claude session file
was asked for only when the blob was completely empty.

Here nothing is ever rewritten: each entry is one JSON line appended to workspace/transcript-journal/<tab>.jsonl,
a line cut by a crash is skipped on read, and `missing_for_client` says which journaled entries a window's own
transcript lacks, so the window can take them back on every connect."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any, Iterable

from app.logging_setup import log_event

# One entry of the window and the same entry from the session file differ by a few seconds in `ts`.
SAME_ENTRY_WINDOW_MS = 10 * 60 * 1000

# What the session file holds besides conversation: slash-command echoes, interrupt and rate-limit lines, and the
# time stamp the backend puts in front of a user's text. None of it was ever a bubble in the window.
_STAMP = re.compile(r"^\[(Sent: |(Sun|Mon|Tue|Wed|Thu|Fri|Sat), )[^\]]*\]\s*", re.IGNORECASE)
_NOISE_PREFIXES = (
    "<command-", "<local-command", "[Request interrupted", "No response requested", "You've hit your session limit",
    "This session is being continued",
)
_NOISE_MARKERS = ("[[NO_UPDATE]]", "PreCompact callback", "[[caroline-internal-turn]]", "[[caroline-attachment-note]]")

_lock = threading.Lock()
_keys: dict[str, set[str]] = {}


def journal_path(workspace_dir: str, tab_id: str) -> Path:
    return Path(workspace_dir) / "transcript-journal" / f"{tab_id}.jsonl"


def _text_hash(text: str) -> str:
    return hashlib.sha1(" ".join(_clean(text or "").split()).encode("utf-8")).hexdigest()[:16]


def _ts_ms(entry: dict[str, Any]) -> int:
    try:
        return int(float(entry.get("ts") or 0))
    except (TypeError, ValueError):
        return 0


def _entry_key(entry: dict[str, Any]) -> str:
    return f"{entry['role']}|{_ts_ms(entry)}|{_text_hash(entry.get('text') or '')}"


def _clean(text: str) -> str:
    return _STAMP.sub("", text.replace("⁣", "")).strip()


def _usable(entry: Any) -> bool:
    if not (isinstance(entry, dict) and entry.get("role") in ("user", "assistant") and isinstance(entry.get("text"), str)):
        return False
    text = _clean(entry["text"])
    return text != "" and not text.startswith(_NOISE_PREFIXES) and not any(m in text for m in _NOISE_MARKERS)


def read_entries(workspace_dir: str, tab_id: str) -> list[dict[str, Any]]:
    """Every journaled entry, oldest first. A damaged line (a write cut by a crash) is skipped, never fatal."""
    path = journal_path(workspace_dir, tab_id)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    skipped = 0
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if _usable(entry):
                entries.append(entry)
    if skipped:
        log_event("engine", "transcript_journal_damaged_lines", tab_id=tab_id, skipped=skipped)
    entries.sort(key=_ts_ms)
    return entries


def append_entries(workspace_dir: str, tab_id: str, entries: Iterable[Any]) -> int:
    """Appends the entries not yet journaled (same role, same ts, same text); returns how many were new.
    Attachments are kept as names only -- the bytes live in the attachment store."""
    path = journal_path(workspace_dir, tab_id)
    with _lock:
        known = _keys.get(str(path))
        if known is None:
            known = {_entry_key(e) for e in read_entries(workspace_dir, tab_id)}
            _keys[str(path)] = known
        lines = []
        for e in entries:
            if not _usable(e):
                continue
            key = _entry_key(e)
            if key in known:
                continue
            known.add(key)
            atts = [
                {k: a.get(k) for k in ("name", "mimeType") if a.get(k) is not None}
                for a in (e.get("attachments") or [])
                if isinstance(a, dict)
            ]
            lines.append(json.dumps({"role": e["role"], "text": _clean(e["text"]), "ts": _ts_ms(e), "attachments": atts}, ensure_ascii=False))
        if not lines:
            return 0
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # a first byte "\n" closes a line an earlier crash left half written, so it cannot swallow ours
            needs_break = path.exists() and path.stat().st_size > 0 and not _ends_with_newline(path)
            with path.open("a", encoding="utf-8", newline="\n") as f:
                if needs_break:
                    f.write("\n")
                f.write("\n".join(lines) + "\n")
                f.flush()
        except OSError as exc:
            _keys.pop(str(path), None)
            log_event("engine", "transcript_journal_append_failed", tab_id=tab_id, error=str(exc)[:200])
            return 0
        return len(lines)


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as f:
        f.seek(-1, 2)
        return f.read(1) == b"\n"


def missing_for_client(
    journaled: list[dict[str, Any]], client_entries: list[dict[str, Any]], limit: int = 400
) -> list[dict[str, Any]]:
    """The journaled entries the window's transcript lacks, oldest first. Only entries newer than the window's
    oldest one count (an old tail the window rotated out on purpose is not brought back), and an entry is
    present when the window has the same role and text within SAME_ENTRY_WINDOW_MS."""
    client = [e for e in client_entries if _usable(e)]
    floor = min((_ts_ms(e) for e in client if _ts_ms(e)), default=0)
    have: dict[tuple[str, str], list[int]] = {}
    for e in client:
        have.setdefault((e["role"], _text_hash(e["text"])), []).append(_ts_ms(e))
    missing = []
    for e in journaled:
        ts = _ts_ms(e)
        if ts < floor:
            continue
        times = have.get((e["role"], _text_hash(e["text"])), [])
        if any(abs(ts - t) <= SAME_ENTRY_WINDOW_MS for t in times):
            continue
        missing.append(e)
    return missing[-limit:]
