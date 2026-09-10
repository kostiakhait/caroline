"""Ports the failure-classification regexes and pure matching logic from
backend/src/server.ts (lines ~203-320) -- hard-won, incident-driven
knowledge accumulated over months, ported near-verbatim (the actual
patterns are correct, tested-by-fire knowledge, not re-derived from a
summary). Deliberately pure/stateless here (no ChatSession access, no I/O)
so this piece is safe to unit-test in isolation; the STATEFUL orchestration
(which classification triggers which recovery action, restart-budget
bookkeeping, connState transitions) lives in chat_session.py, which calls
into these functions.
"""

from __future__ import annotations

import re

# --- classifier refusal -----------------------------------------------------
# The SDK's own classifier occasionally refuses with wording that suggests
# session-level poisoning ("Start a new session to continue"), but a
# same-session resubmit of the identical request was confirmed live to
# succeed -- a transient, request-scoped false positive, not real session
# corruption.
CLASSIFIER_REFUSAL_PATTERN = re.compile(r"can't help with this\.\s*Start a new session to continue", re.IGNORECASE)
_CLASSIFIER_REFUSAL_CATEGORY_RE = re.compile(r"Details:\s*\[([^\]]+)\]", re.IGNORECASE)


def extract_classifier_refusal_category(text: str) -> str | None:
    m = _CLASSIFIER_REFUSAL_CATEGORY_RE.search(text)
    return m.group(1) if m else None


# --- billing / balance exhaustion ------------------------------------------
# Primary detection is the SDK's own structured field
# (message.error == "billing_error", checked in chat_session.py directly,
# not here) -- these text patterns are only a catch-block fallback for
# when the failure surfaces as a thrown error before any message is
# yielded.
SW_BALANCE_ERROR_PATTERN = re.compile(r"insufficient_balance|wallet balance is too low", re.IGNORECASE)
ANTHROPIC_BALANCE_ERROR_PATTERN = re.compile(r"credit balance is too low", re.IGNORECASE)


def detect_balance_exhaustion(text: str) -> str | None:
    """Returns "sw", "anthropic", or None."""
    if SW_BALANCE_ERROR_PATTERN.search(text):
        return "sw"
    if ANTHROPIC_BALANCE_ERROR_PATTERN.search(text):
        return "anthropic"
    return None


# --- Claude Code CLI usage-cap message --------------------------------------
# Broadened from an original literal-phrase match after a second real
# message ("You've hit your session limit") slipped through.
CC_CLI_LIMIT_PATTERN = re.compile(r"hit your .*\blimit\b|monthly spend limit|cc_cli_limit_message", re.IGNORECASE)

# --- "Prompt is too long" ----------------------------------------------------
# Root cause: the CLI's own automatic-compaction feature fails outright
# under any env-overridden ANTHROPIC_API_KEY/BASE_URL (every chat source
# except own-anthropic-oauth) with "Not logged in" internally -- it needs
# the real OAuth path. When compaction can't shrink an oversized resumed
# session, the turn fails with this text instead of a real reply.
PROMPT_TOO_LONG_PATTERN = re.compile(r"^Prompt is too long\b", re.IGNORECASE)

# --- tool-use-concurrency session corruption --------------------------------
# Extensive bisection (a real broken transcript, direct claude.exe
# invocation bypassing the SDK) found NO single content anomaly --
# tool_use/tool_result pairing intact, ids matched, no orphans/duplicates.
# Retrying the SAME resume fails identically every time. There is nothing
# to shrink or fork here -- forking would just copy the same broken
# history forward. The only actual recovery is abandoning the session id
# entirely (see chat_session.py's reset_unrecoverable_session).
TOOL_CONCURRENCY_ERROR_PATTERN = re.compile(r"tool use concurrency issues", re.IGNORECASE)

# Stream-level (not an assistant message) sibling of the same failure: a
# tab's stored session id pointing at a .jsonl that no longer exists on
# disk. claude.exe reports this on stderr with the stream just ending,
# never as an assistant message.
SESSION_NOT_FOUND_PATTERN = re.compile(r"No conversation found with session ID", re.IGNORECASE)

# --- standalone "Not logged in" ---------------------------------------------
# Same root cause as PROMPT_TOO_LONG (the CLI's own internal compaction
# call complaining under an env-overridden API key), just without that
# prefix. No recovery action -- suppressed entirely; the account's real
# login state is fine.
NOT_LOGGED_IN_PATTERN = re.compile(r"Not logged in", re.IGNORECASE)
