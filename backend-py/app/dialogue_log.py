"""The exact record of the dialogue with the user, as it happened on the screen (2026-10-26, explicit instruction).

The CLI's own transcript is what the MODEL said; between that and the window there are filters (reply review, translation,
silence rules), so it is not what the user was SHOWN. This log holds what really crossed: every message the user sent, every
message that reached the window (after the filters, in the form the user read it), and -- marked as withheld -- what the
model said that a filter kept back. The model reads it through the get_dialogue_transcript tool (plugins/dialogue_plugin.py).

One JSON line per entry in <workspace>/dialogue-log-<tab>.jsonl; appended in the order things happened; never rewritten."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.logging_setup import log_event

USER, ASSISTANT = "user", "assistant"
SHOWN, NOTE, REPORT, WITHHELD = "shown", "progress_note", "report", "withheld"

READ_TAIL_BYTES = 3_000_000
DEFAULT_LIMIT = 30
MAX_LIMIT = 200
MAX_OUTPUT_CHARS = 24_000


def log_path(workspace_dir: str, tab_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", tab_id or "1")
    return Path(workspace_dir) / f"dialogue-log-{safe}.jsonl"


def append(workspace_dir: str, tab_id: str, role: str, text: str, kind: str = SHOWN, **meta: Any) -> None:
    """Never raises: the log is a record, not part of the dialogue's own path."""
    if not (text or "").strip():
        return
    try:
        now = time.time()
        entry = {"ts": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"), "ms": int(now * 1000),
                 "role": role, "kind": kind, "text": text, **{k: v for k, v in meta.items() if v}}
        with open(log_path(workspace_dir, tab_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        log_event("engine", "dialogue_log_append_failed", tab_id=tab_id, error=str(exc)[:200])


def read_entries(workspace_dir: str, tab_id: str) -> list[dict[str, Any]]:
    path = log_path(workspace_dir, tab_id)
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > READ_TAIL_BYTES:
                f.seek(size - READ_TAIL_BYTES)
                f.readline()  # a cut line
            raw = f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return []
    entries = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("text"):
            entries.append(entry)
    return entries


def _label(entry: dict[str, Any]) -> str:
    if entry.get("role") == USER:
        return "User" + (" (voice)" if entry.get("voice") else "")
    kind = entry.get("kind")
    if kind == WITHHELD:
        return "Caroline [WITHHELD -- said by the model, NOT shown to the user]"
    if kind == NOTE:
        return "Caroline [progress note]"
    if kind == REPORT:
        return "Caroline [report added by the delivery check]"
    return "Caroline"


def select(entries: list[dict[str, Any]], limit: int = DEFAULT_LIMIT, since_minutes: float | None = None,
           contains: str | None = None, include_withheld: bool = True) -> list[dict[str, Any]]:
    picked = entries
    if not include_withheld:
        picked = [e for e in picked if e.get("kind") != WITHHELD]
    if since_minutes is not None and since_minutes > 0:
        cutoff = (time.time() - since_minutes * 60) * 1000
        picked = [e for e in picked if e.get("ms", 0) >= cutoff]
    if contains and contains.strip():
        needle = contains.strip().lower()
        picked = [e for e in picked if needle in str(e.get("text", "")).lower()]
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    return picked[-limit:]


def render(entries: list[dict[str, Any]]) -> str:
    """Whole messages, oldest first; when the total is too long the OLDEST are left out and the text says so."""
    blocks = [f"[{e.get('ts', '?')}] {_label(e)}:\n{e['text']}" for e in entries]
    out, total, skipped = [], 0, 0
    for block in reversed(blocks):
        if total + len(block) > MAX_OUTPUT_CHARS and out:
            skipped += 1
            continue
        out.append(block)
        total += len(block)
    out.reverse()
    head = f"(the {skipped} oldest of the selected messages are left out for length; ask with a smaller limit or contains)\n\n" if skipped else ""
    return head + "\n\n".join(out) if out else "(no messages match)"
