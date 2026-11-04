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
import re
import time
from datetime import datetime
from email.utils import getaddresses
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


_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|sv|ответ|пересл)\s*:\s*)+", re.IGNORECASE)


def _addresses(text: Any) -> set[str]:
    return {a.lower() for _, a in getaddresses([str(text or "")]) if a} | {a.lower() for a in _ADDRESS.findall(str(text or ""))}


def _subject_key(subject: Any) -> str:
    return " ".join(_SUBJECT_PREFIX.sub("", str(subject or "")).lower().split())


def sent_to(workspace_dir: str, sender: Any, subject: Any, days: float = 30.0, limit: int = 3) -> list[dict[str, Any]]:
    """What Caroline sent to this sender, or in this thread (the same subject without Re:/Fwd:): the entries of the action log.
    For a mail she reads -- see note_for_mail. Never raises."""
    try:
        senders = _addresses(sender)
        key = _subject_key(subject)
        found = []
        for e in recent(workspace_dir, days * 24):
            same_person = bool(senders & _addresses(e.get("to")))
            same_thread = bool(key) and key == _subject_key(e.get("subject"))
            if same_person or same_thread:
                found.append(e)
        return found[-limit:]
    except Exception as exc:  # noqa: BLE001
        log_event("engine", "action_ledger_sent_to_failed", error=str(exc)[:200])
        return []


def note_for_mail(workspace_dir: str, sender: Any, subject: Any, text_chars: int = 500) -> str:
    """The line under a mail Caroline has just read (2026-11-04, explicit instruction: the reply of Anita Frohlich to a letter
    Caroline herself had written was taken for a message she did not know -- and the owner was asked whether HE had written it):
    done by code at the moment of reading, so it does not depend on the model remembering to look in Sent."""
    entries = sent_to(workspace_dir, sender, subject)
    if not entries:
        return ""
    lines = []
    for e in entries:
        text = " ".join(str(e.get("text") or "").split())
        said = f': "{text[:text_chars]}{"..." if len(text) > text_chars else ""}"' if text else ""
        subject_part = f' subject "{e["subject"]}"' if e.get("subject") else ""
        lines.append(f"- {e.get('when')}: you sent {e.get('tool')} to {e.get('to', '?')}{subject_part}{said}")
    return (
        "[Action log: this mail is from someone you wrote to, or belongs to a thread you wrote in. What you sent -- you wrote "
        "it yourself, on the owner's instruction -- so this mail may be an answer to it; read it in that light and do not ask the "
        "owner whether he wrote it:\n" + "\n".join(lines) + "]"
    )


def short_mark_for_mail(workspace_dir: str, sender: Any, subject: Any) -> str:
    """One short marker for a line of a mail list."""
    entries = sent_to(workspace_dir, sender, subject, limit=1)
    if not entries:
        return ""
    e = entries[-1]
    return f"you wrote to this sender/thread on {e.get('when')}: " + " ".join(str(e.get("text") or "").split())[:120]


BULK_RUN = 3                 # this many sends in a row by the same tool, close in time, are one mailing
BULK_GAP_S = 600


def _groups(entries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Consecutive sends by one tool, minutes apart, grouped: a mailing to fifty shops must not push a letter to one person out."""
    groups: list[list[dict[str, Any]]] = []
    for e in entries:
        last = groups[-1] if groups else None
        if last and last[-1].get("tool") == e.get("tool") and e.get("ms", 0) - last[-1].get("ms", 0) <= BULK_GAP_S * 1000:
            last.append(e)
        else:
            groups.append([e])
    return groups


def _line(e: dict[str, Any], text_chars: int) -> str:
    subject = f' subject "{e["subject"]}"' if e.get("subject") else ""
    sender = f" from {e['from']}" if e.get("from") else ""
    text = " ".join(str(e.get("text") or "").split())
    said = f': "{text[:text_chars]}{"..." if len(text) > text_chars else ""}"' if text else ""
    return f"- {e.get('when')}: {e.get('tool')}{sender} to {e.get('to', '?')}{subject}{said}"


def system_prompt_clause(workspace_dir: str, days: float = 7.0, limit: int = 5, text_chars: int = 160) -> str:
    """For the main model's system prompt, rebuilt at every engine start: what Caroline herself has sent in the last days,
    with the words that went out (2026-11-04, explicit instruction: she wrote to Anita Frohlich about Tuesday 10:00 and, after
    several compactions and restarts, asked the user whether HE had written that). Nothing about a sent message may depend on the
    conversation still holding it; a reply to it must be recognised as a reply to her own message."""
    groups = _groups(recent(workspace_dir, days * 24))[-limit:]
    if not groups:
        return ""
    lines = []
    for group in groups:
        if len(group) < BULK_RUN:
            lines.extend(_line(e, text_chars) for e in group)
            continue
        recipients = ", ".join(str(e.get("to", "?")) for e in group[:4]) + (", ..." if len(group) > 4 else "")
        lines.append(f"- {group[0].get('when')} .. {group[-1].get('when')}: {len(group)} messages by {group[0].get('tool')}, "
                     f"one mailing (to {recipients}); the first one: " + _line(group[0], 200)[2:].split(": ", 1)[-1])
    return (
        "WHAT YOU HAVE SENT RECENTLY (from the system's action log, oldest first): you wrote and sent each of these yourself, "
        "on the owner's instruction, and the words below are what really went out. When someone answers one of them, it is an "
        "answer to YOUR message: take what you proposed or promised in it as the starting point, and never ask the owner whether "
        "he wrote it. Older ones, and replies to them, show up as a note under a mail you read, and are in the Sent folder.\n"
        + "\n".join(lines)
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
