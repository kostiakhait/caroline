"""The record of what Caroline has actually DONE on the user's behalf, kept by code (2026-10-26, explicit instruction).

A finished outbound action (an email sent, a message posted) is a fact of its own, not something the model has to remember
to write down: on 2026-10-26 an email the user had approved went out at 08:52, nothing was saved, and the only memory note
written afterwards (a forced one, before a context compaction) said the reply was "NOT sent yet, waiting for the user's
yes" -- because by then the model no longer knew. This log is written by dispatch() the moment such a tool call succeeds,
and the same fact is handed to memory. It is shown to the model before a compaction so that what is done is never
recorded as pending.

<workspace>/action-ledger.jsonl, one line per finished action; never rewritten."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.logging_setup import log_event

MAX_TEXT_CHARS = 600
RECIPIENT_KEYS = ("to", "recipient", "recipients", "chat", "chat_id", "chatId", "phone", "number", "channel", "address_to")
TEXT_KEYS = ("text", "message", "body", "html", "caption")
SUBJECT_KEYS = ("subject", "title")


def ledger_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "action-ledger.jsonl"


def _first(args: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = args.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _as_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def describe(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """What a finished send tool call did, from its own arguments."""
    recipient = _first(args, RECIPIENT_KEYS)
    subject = _first(args, SUBJECT_KEYS)
    text = _first(args, TEXT_KEYS)
    entry: dict[str, Any] = {"tool": tool}
    if args.get("address") or args.get("from"):
        entry["from"] = _as_text(args.get("from") or args.get("address"))
    if recipient is not None:
        entry["to"] = _as_text(recipient)
    if subject is not None:
        entry["subject"] = _as_text(subject)
    if text is not None:
        entry["text"] = _as_text(text)[:MAX_TEXT_CHARS]
    return entry


def fact_text(entry: dict[str, Any]) -> str:
    """The sentence handed to memory: written as a finished fact, with the exact words that went out."""
    kind = "an email" if entry["tool"].startswith("email") else f"a message ({entry['tool']})"
    parts = [f"DONE on {entry['when']}: Caroline sent {kind}"]
    if entry.get("from"):
        parts.append(f"from {entry['from']}")
    if entry.get("to"):
        parts.append(f"to {entry['to']}")
    if entry.get("subject"):
        parts.append(f"with the subject \"{entry['subject']}\"")
    head = " ".join(parts) + "."
    body = f" What was sent: \"{entry['text']}\"" if entry.get("text") else ""
    return head + body + " This has already been sent -- it is not a draft and not waiting for anyone's approval."


def append(workspace_dir: str, tab_id: str | None, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Never raises."""
    try:
        now = time.time()
        entry = {
            "ms": int(now * 1000), "when": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
            "tab": tab_id, **describe(tool, args if isinstance(args, dict) else {}),
        }
        with open(ledger_path(workspace_dir), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry
    except Exception as exc:  # noqa: BLE001
        log_event("engine", "action_ledger_append_failed", tool=tool, error=str(exc)[:200])
        return None


def recent(workspace_dir: str, hours: float = 24.0) -> list[dict[str, Any]]:
    try:
        raw = ledger_path(workspace_dir).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    cutoff = (time.time() - hours * 3600) * 1000
    entries = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("ms", 0) >= cutoff:
            entries.append(entry)
    return entries


def checker_note(workspace_dir: str, hours: float = 24.0, limit: int = 8) -> str:
    """For the checks that read the dialogue (delivery completeness): what is already DONE, so a message about a
    finished action is never taken for a missing draft ("include the text now" -- 2026-10-26, for an email already sent)."""
    entries = recent(workspace_dir, hours)[-limit:]
    if not entries:
        return ""
    lines = [
        f"- {e.get('when')}: {e.get('tool')} to {e.get('to', '?')}" + (f' "{e["subject"]}"' if e.get("subject") else "") + " -- ALREADY SENT"
        for e in entries
    ]
    return (
        "ACTIONS ALREADY DONE (from the system's action log). A message about one of these reports a finished matter: "
        "it never lacks a 'draft to send', and nothing about it is waiting for approval:\n" + "\n".join(lines)
    )


def flush_addendum(workspace_dir: str, hours: float = 24.0, limit: int = 12) -> str:
    """For the save-before-compaction message: what is already DONE, so it is never written down as pending."""
    entries = recent(workspace_dir, hours)[-limit:]
    if not entries:
        return ""
    lines = []
    for e in entries:
        subject = f' "{e["subject"]}"' if e.get("subject") else ""
        lines.append(f"- {e.get('when')}: {e.get('tool')} to {e.get('to', '?')}{subject} -- SENT")
    return (
        "\n[From the action log kept by the system -- these are DONE, sent for real. Never save them as unsent, "
        "pending or waiting for approval:\n" + "\n".join(lines) + "]"
    )
