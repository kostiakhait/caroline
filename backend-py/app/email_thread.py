"""One correspondence read whole: every letter of a thread from the inbox and from Sent, in order, each with its author.

Owner's complaint (2026-11-27, tab 1): Caroline did not understand that letters are connected. She read the letters of the
thread "Re: Декабрь" one by one: Oksana's letter of 15:58 was never reported, the owner's own answer of 16:02 (written by
him on his phone) was taken for a letter of unknown origin that "has to be investigated", and the day of a party was
mixed up across three letters. There was no way to see the correspondence as one conversation. This module is the pure
part of email_get_thread (app/plugins/email_plugin.py): the subject key, the plain text of a letter without the quoted
history, the author of each letter and the assembled text. The IMAP side is in the plugin."""

from __future__ import annotations

import html as html_lib
import re
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

# The same prefixes action_ledger's _subject_key strips, plus the numbered forms some clients write ("Re[2]:").
_SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|sv|antw|ответ|пересл)(\[\d+\])?\s*:\s*)+", re.IGNORECASE)
_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# Where the quoted history of a letter starts: "On <date> <someone> wrote:" and its translations, an Outlook header
# block, a line of "-----Original Message-----".
_QUOTE_HEADER = re.compile(
    r"^\s*(on\s.+\swrote:|.+\sнаписал[аи]?:|\d{1,2}[./]\d{1,2}[./]\d{2,4}.*(wrote|написал[аи]?):|-{2,}\s*original message\s*-{2,}|"
    r"-{2,}\s*исходное сообщение\s*-{2,}|from:\s.+|от:\s.+)\s*$",
    re.IGNORECASE,
)
_GMAIL_QUOTE = re.compile(r"<div[^>]*class=\"?[^\">]*gmail_quote.*", re.IGNORECASE | re.DOTALL)
_BLOCKQUOTE = re.compile(r"<blockquote.*?</blockquote>", re.IGNORECASE | re.DOTALL)
_TAG_BREAK = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h\d)\b[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_STYLE = re.compile(r"<(style|script|head)\b.*?</\1>", re.IGNORECASE | re.DOTALL)

SENT_BY_LEDGER_WINDOW_MS = 15 * 60 * 1000   # a logged send and the copy in Sent are the same letter within this time


def subject_key(subject: Any) -> str:
    """The subject without Re:/Fwd: and their translations, lowercased, spaces collapsed: one key per thread."""
    return " ".join(_SUBJECT_PREFIX.sub("", str(subject or "")).lower().split())


def addresses(text: Any) -> set[str]:
    if isinstance(text, (list, tuple)):
        text = ", ".join(str(t) for t in text)
    return {a.lower() for _, a in getaddresses([str(text or "")]) if a} | {a.lower() for a in _ADDRESS.findall(str(text or ""))}


def html_to_text(value: str) -> str:
    text = _STYLE.sub("", value)
    text = _TAG_BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = html_lib.unescape(text).replace("\xa0", " ").replace("\r", "")
    return "\n".join(line.rstrip() for line in text.split("\n"))


def own_text(message: dict[str, Any]) -> str:
    """What the author of this letter wrote himself: the plain text without the quoted earlier letters."""
    raw_html = message.get("html")
    raw_text = message.get("text") or message.get("body")
    if isinstance(raw_html, str) and raw_html.strip():
        text = html_to_text(_BLOCKQUOTE.sub("", _GMAIL_QUOTE.sub("", raw_html)))
    else:
        text = str(raw_text or "").replace("\r", "")
    kept: list[str] = []
    for line in text.split("\n"):
        if line.lstrip().startswith(">"):
            continue
        if _QUOTE_HEADER.match(line) and any(k.strip() for k in kept):
            break
        kept.append(line)
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def _normal(text: Any) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", str(text or "").lower()).split())


def message_ms(message: dict[str, Any]) -> int:
    try:
        return int(parsedate_to_datetime(str(message.get("date") or "")).timestamp() * 1000)
    except (TypeError, ValueError, IndexError, OverflowError):
        return 0


def logged_send(message: dict[str, Any], ledger: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The action-log entry of this very letter, if Caroline sent it: the start of the logged text is the start of the
    letter's own text, or the same thread and recipient within minutes of each other."""
    body = _normal(own_text(message))
    ms = message_ms(message)
    key = subject_key(message.get("subject"))
    recipients = addresses(message.get("to")) | addresses(message.get("cc"))
    for entry in reversed(ledger):
        if not str(entry.get("tool") or "").startswith("email"):
            continue
        logged = _normal(entry.get("text"))[:120]
        if logged and body.startswith(logged[: max(20, min(len(logged), len(body)))]):
            return entry
        close = ms and abs(int(entry.get("ms") or 0) - ms) <= SENT_BY_LEDGER_WINDOW_MS
        if close and key and key == subject_key(entry.get("subject")) and recipients & addresses(entry.get("to")):
            return entry
    return None


def author_of(message: dict[str, Any], mailbox: str, ledger: list[dict[str, Any]]) -> str:
    """Who wrote this letter, in words for the model."""
    sender = addresses(message.get("from"))
    if (mailbox or "").lower() not in sender:
        return f"written by {message.get('from') or 'the other side'} (the other side of this correspondence)"
    entry = logged_send(message, ledger)
    if entry is not None:
        return f"written by YOU, Caroline, from this mailbox on the owner's instruction (your action log: {entry.get('when')})"
    return ("written from this mailbox WITHOUT you: you never sent it (it is not in your action log). If this is one of the "
            "owner's own mailboxes, the owner wrote it himself, from another device or program. It is his own letter, not "
            "something to investigate")


def same_thread(message: dict[str, Any], key: str, counterparts: set[str]) -> bool:
    """One correspondence: the same subject key and the same other side (by the addresses in From, To or Cc)."""
    if subject_key(message.get("subject")) != key:
        return False
    if not counterparts:
        return True
    people = addresses(message.get("from")) | addresses(message.get("to")) | addresses(message.get("cc"))
    return bool(people & counterparts)


def letter_for_reminders(message: dict[str, Any]) -> str:
    """One letter as reminder.extract reads it: who, when (relative days count from the letter's date), its own words."""
    return (f"Date: {message.get('date') or '?'}\nFrom: {message.get('from') or '?'}\nTo: {message.get('to') or '?'}\n"
            f"Subject: {message.get('subject') or ''}\n\n{own_text(message)}")


def thread_for_reminders(messages: list[dict[str, Any]], budget_chars: int = 5500) -> str:
    """The latest letters of a thread, NEWEST FIRST (where it stands now comes first, and a new letter changes the start
    of the text, so it is looked at again), as many as fit the extractor's prompt. `messages` is oldest first."""
    parts: list[str] = []
    used = 0
    for message in reversed(messages):
        block = letter_for_reminders(message)
        if parts and used + len(block) > budget_chars:
            break
        parts.append(block)
        used += len(block)
    return "A correspondence, the newest letter first.\n\n" + "\n\n--- the letter before it ---\n".join(parts) if parts else ""


def render(messages: list[dict[str, Any]], mailbox: str, ledger: list[dict[str, Any]], start: int, budget_chars: int) -> tuple[str, int]:
    """The thread as text from letter number `start` (0-based, oldest first) for about `budget_chars` characters.
    Returns (text, the number of the first letter not shown, or -1 when everything is shown)."""
    total = len(messages)
    parts: list[str] = []
    used = 0
    index = start
    while index < total:
        m = messages[index]
        own = own_text(m) or "(no text of its own)"
        block = (
            f"--- Letter {index + 1} of {total} | {m.get('date') or '?'} | folder {m.get('folder')} uid {m.get('uid')}\n"
            f"From: {m.get('from') or '?'}\nTo: {m.get('to') or '?'}" + (f"\nCc: {m['cc']}" if m.get("cc") else "") +
            f"\nSubject: {m.get('subject') or ''}\nAuthor: {author_of(m, mailbox, ledger)}\n"
            + (f"Attachments: {', '.join(str(a.get('filename')) for a in m['attachments'])}\n" if m.get("attachments") else "")
            + f"\n{own}\n"
        )
        if parts and used + len(block) > budget_chars:
            break
        parts.append(block)
        used += len(block)
        index += 1
    return "\n".join(parts), (index if index < total else -1)
