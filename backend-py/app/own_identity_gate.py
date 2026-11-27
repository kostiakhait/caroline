"""The final-text gate for sends under the owner's own identity (2026-11-20, explicit order).

The owner's rule: whatever goes out under HIS name (his mailboxes, his messengers) goes out only after the FINAL
exact text was shown to him on the screen and he said yes AFTER seeing it. A direct order does not approve it; an edit
he dictated himself does not either -- the edited text is shown again.

Incident (2026-11-20 11:40): a letter to Anita went from his gmail unseen. The ladder verifier threw the draft away as
"unfinished work", so it never reached the window; his angry "ДА..." typed BEFORE the draft existed was counted as the
approval; and the 2026-10-20 "explicit order = approved" shortcut let the send through.

Plain code, no model, except the one yes/no judgement of the user's later messages, which the caller makes with the
existing action.sanction agent. What is "shown" is read from the exact dialogue record (app/dialogue_log.py): only
entries that really reached the window (kind shown or report), never withheld ones or progress notes.
"""

from __future__ import annotations

import html
import re
from typing import Any, Optional

from app import dialogue_log

BODY_KEYS = ("text", "body", "html", "message", "caption", "content")
_TAGS = re.compile(r"<[^>]+>")
_MD = re.compile(r"[*_`#]+")
_QUOTE_MARK = re.compile(r"(?m)^\s*>+ ?")
_SPACE = re.compile(r"\s+")
SAME_TEXT_WINDOW_MS = 24 * 3600 * 1000


def normalize(text: str) -> str:
    """Comparison form: markup, quote markers, line breaks and case removed. The chat shows a draft as a quote with bold
    labels; the mail carries the bare body -- both must come out the same."""
    text = html.unescape(_TAGS.sub(" ", text or ""))
    text = _QUOTE_MARK.sub("", text)
    text = _MD.sub("", text)
    return _SPACE.sub(" ", text).strip().lower()


def split_quoted(text: str) -> list[tuple[bool, str]]:
    """Splits a message into (is_quote, chunk) runs; a quote is a run of lines starting with '>'. Quoted runs are the
    draft of an outgoing message and are shown to the user verbatim: the translation layer must not touch them, or the
    window shows a translation of the body while the send carries the original, and the gate can never match."""
    runs: list[tuple[bool, list[str]]] = []
    for line in (text or "").splitlines(keepends=True):
        quoted = line.lstrip().startswith(">")
        if runs and runs[-1][0] == quoted:
            runs[-1][1].append(line)
        else:
            runs.append((quoted, [line]))
    return [(q, "".join(lines)) for q, lines in runs]


def body_of(args: dict[str, Any] | None) -> str:
    """The message body of a send call's arguments (the first body-like argument that is a non-empty string)."""
    for key in BODY_KEYS:
        value = (args or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def shown_entry(entries: list[dict[str, Any]], body: str) -> Optional[dict[str, Any]]:
    """The latest assistant entry that reached the window and contains the whole body (after normalization)."""
    wanted = normalize(body)
    if not wanted:
        return None
    for entry in reversed(entries):
        if entry.get("role") != dialogue_log.ASSISTANT or entry.get("kind") not in (dialogue_log.SHOWN, dialogue_log.REPORT):
            continue
        if wanted in normalize(entry.get("text") or ""):
            return entry
    return None


def messages_after(entries: list[dict[str, Any]], shown_ms: int) -> list[str]:
    """What the user typed after the draft was on the screen, oldest first."""
    return [e["text"] for e in entries if e.get("role") == dialogue_log.USER and int(e.get("ms") or 0) > shown_ms]


def _has_quote(text: str) -> bool:
    return any(quoted and chunk.strip() for quoted, chunk in split_quoted(text))


def approval_window(entries: list[dict[str, Any]], body: str, sent_ms: int = 0) -> tuple[Optional[dict[str, Any]], list[str]]:
    """(the latest showing of the body, the user's messages that may answer it).

    A repeat of the SAME text does not take back a yes given to it (2026-11-27, live: the draft was shown, the owner said
    "Да", the same draft was shown again, and the gate counted only what came after the repeat -- twice in a row). So the
    answers count from the earliest showing of this body in the last 24 hours that no OTHER quoted draft came after, and
    never from before the last time this body was actually sent (`sent_ms`): one yes is one send."""
    shown = shown_entry(entries, body)
    if shown is None:
        return None, []
    wanted = normalize(body)
    latest = int(shown.get("ms") or 0)
    first = latest
    position = next(i for i, e in enumerate(entries) if e is shown)
    for entry in reversed(entries[:position]):
        if entry.get("role") != dialogue_log.ASSISTANT or entry.get("kind") not in (dialogue_log.SHOWN, dialogue_log.REPORT):
            continue
        ms = int(entry.get("ms") or 0)
        if ms <= sent_ms or latest - ms > SAME_TEXT_WINDOW_MS:
            break
        text = entry.get("text") or ""
        if wanted in normalize(text):
            first = ms
        elif _has_quote(text):
            break  # another draft stood on the screen: what was said then answered that one
    return shown, messages_after(entries, max(first, sent_ms))


def missing_reason(body: str, shown: Optional[dict[str, Any]], answers: list[str], approved: bool) -> Optional[str]:
    """None when the send may go. Otherwise one sentence for the model, saying exactly what is missing."""
    if not body:
        return None  # nothing textual to compare (a file-only send is the file-transfer guard's business)
    if shown is None:
        return ("this goes out under the user's own identity and its exact final text has not been shown to the user yet. "
                "Show the user the complete final text (recipient, subject, body) as a markdown blockquote (every line "
                "starting with '> '), in the language it will be sent in, word for word as it will go out; quoted text is "
                "displayed verbatim and never translated. Then end your turn, and send only after the user answers yes to "
                "that text.")
    if not answers:
        return ("the final text was shown, but the user has not answered since. End your turn and wait for the user's "
                "yes; a yes given before the text was shown does not count.")
    if not approved:
        return ("the user's answer after the shown text is not a yes to it. Do not send; do what the user said, and if "
                "the text changes, show the new final text again and wait for a yes.")
    return None
