"""Shared context-overflow guards for plugin tools -- a model-facing tool
result must never be allowed to hand the model an unbounded amount of text
in one call.

Per explicit instruction (2026-10-16), after a real incident: a chat tab's
context overflowed past what auto-compaction could recover, because
email_read_range fetched up to 200 full message bodies concurrently with
only a message-COUNT cap, no total-character budget -- one real queued
submission reached 2.1 million characters. Fixed there first
(email_plugin.py's EMAIL_BODY_MAX_CHARS/bodyOffset, email_batch_plugin.py's
EMAIL_RANGE_TOTAL_BODY_CHARS), then a full audit of every plugin found the
same failure shape recurring in ~16 other files (ratatosk, telegram,
memory, notes, contacts, event memory, the browser/native_exe/anvar
helpers, ...). Rather than re-deriving the same truncate-with-a-continuation
-hint / stop-a-batch-early-with-a-total-budget logic in each file the way
the email fix first did, this module is the one place that logic lives --
every other fix in this pass calls into it instead of writing its own.

Three independent helpers, matching the three risk shapes the audit found:
- ONE long value (a long note/document/email/log) -- truncate().
- MANY full values in one batch call (a message-history sweep) -- cap_batch().
- A list of small objects with no other natural limit (a contact/chat
  listing) -- cap_count().

ITEM_MAX_CHARS/BATCH_MAX_CHARS reuse the exact numbers already established
and vetted earlier the same day (email_plugin.EMAIL_BODY_MAX_CHARS,
files_plugin.MAX_READ_BYTES/email_batch_plugin.EMAIL_RANGE_TOTAL_BODY_CHARS)
rather than picking fresh ones here."""

from __future__ import annotations

from typing import Callable, Sequence, TypeVar

ITEM_MAX_CHARS = 20_000
BATCH_MAX_CHARS = 200_000
LIST_MAX_COUNT = 200

T = TypeVar("T")


def truncate(text: str, max_chars: int = ITEM_MAX_CHARS, offset: int = 0, continue_param: str = "offset") -> tuple[str, str | None]:
    """Returns (window, note). window is text[offset:offset+max_chars]; note
    is None if that window reaches the end of text, otherwise a plain-
    language sentence telling the model the real total length and what
    `continue_param` value to pass next -- same shape as files_plugin.py's
    read_document_pages ("N more pages beyond this range -- call again with
    page_start=N") and email_plugin.py's own bodyOffset continuation."""
    total = len(text)
    window = text[offset:offset + max_chars]
    shown_end = offset + len(window)
    if shown_end >= total:
        return window, None
    note = (
        f"[... truncated: showing characters {offset}-{shown_end} of {total} total -- "
        f"call again with {continue_param}={shown_end} to continue reading ...]"
    )
    return window, note


def cap_batch(
    entries: Sequence[T],
    text_of: Callable[[T], str],
    label_of: Callable[[T], str],
    max_chars: int = BATCH_MAX_CHARS,
) -> tuple[list[T], list[str]]:
    """entries have ALREADY been fetched in full (each item's own text
    already truncated to ITEM_MAX_CHARS by the caller, if it goes through a
    per-item fetch that itself uses truncate() -- this only bounds the
    COMBINED total, same two-layer defense email_read_range uses: one
    pathological item can't blow the budget alone, and many ordinary-sized
    items together still can't either).

    Returns (included, skipped_labels) -- included is a prefix of entries
    whose cumulative text() length stays under max_chars; skipped_labels
    names everything cut off, for a "fetch these individually" follow-up
    message, same convention as email_read_range's own STOPPED EARLY note."""
    included: list[T] = []
    skipped: list[str] = []
    total = 0
    for entry in entries:
        if total > max_chars:
            skipped.append(label_of(entry))
            continue
        included.append(entry)
        total += len(text_of(entry))
    return included, skipped


def cap_count(items: Sequence[T], max_count: int = LIST_MAX_COUNT) -> tuple[list[T], int]:
    """Returns (items[:max_count], number cut off)."""
    if len(items) <= max_count:
        return list(items), 0
    return list(items[:max_count]), len(items) - max_count


def skipped_note(skipped_labels: Sequence[str], max_chars: int, retry_hint: str) -> str:
    """The plain-language tail every cap_batch() caller appends to its own
    report when skipped_labels is non-empty -- kept here too so the wording
    stays consistent across every plugin that uses cap_batch(), the same
    way email_read_range's own STOPPED EARLY note reads."""
    joined = ", ".join(skipped_labels)
    return (
        f"STOPPED EARLY: total text passed {max_chars} characters, so {len(skipped_labels)} more "
        f"item(s) were NOT fetched in full: {joined}. {retry_hint}"
    )
