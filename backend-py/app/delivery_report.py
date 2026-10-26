"""The end-of-turn check of what the user actually got (2026-10-26, explicit instruction).

When a Claude tab's turn has ended and its answer was approved (the model ladder's verdict, if any, is already
in), a verifier compares the transcript of what was DELIVERED to the user -- the messages that really reached the
window, after review and translation, not what the model drafted -- with the work that was done in the turn (the
real tool calls and their results), and answers one question: did the user receive enough information about the
result? If not, it writes the missing report, which is sent to the user directly, as a message of its own (no new
model turn).

This module holds the record of one turn and the pure parts; the verifier call is
small_model_engine.verify_delivery, the wiring is chat_session_mixins/delivery_report.py."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# edition:cut claude model ladder
from app.claude_ladder import TRACE_LINES_KEPT, LadderTurn, visible_text
# edition:end
#ic| TRACE_LINES_KEPT = 40
#ic|
#ic|
#ic| def visible_text(wire: dict[str, Any]) -> str:
#ic|     content = (wire.get("message") or {}).get("content")
#ic|     if not isinstance(content, list):
#ic|         return ""
#ic|     return "\n".join(
#ic|         b.get("text", "") for b in content
#ic|         if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
#ic|     )
#ic|
#ic|
#ic| @dataclass
#ic| class TurnWork:
#ic|     """What one turn did: its tool calls as trace lines (filled from the sw turn recorder, see sw_trace)."""
#ic|
#ic|     user_text: str
#ic|     trace: list[str] = field(default_factory=list)
#ic|     open_calls: dict[str, tuple[str, str]] = field(default_factory=dict)
#ic|
#ic|     def observe(self, wire: dict[str, Any]) -> None:
#ic|         return None  # the turn's work is filled in at its end (sw_trace), not message by message

MAX_DELIVERED_CHARS = 6000


@dataclass
class TurnRecord:
    """One real-user turn: the request, the work done (tool calls with results) and what was delivered."""

    user_text: str
    # edition:cut claude model ladder
    work: LadderTurn = field(init=False)
    # edition:end
    #ic| work: TurnWork = field(init=False)
    delivered: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # edition:cut claude model ladder
        self.work = LadderTurn(user_text=self.user_text)
        # edition:end
        #ic| self.work = TurnWork(user_text=self.user_text)

    def observe(self, wire: dict[str, Any] | None) -> None:
        if wire is not None:
            self.work.observe(wire)

    def note_sent(self, envelope: Any) -> None:
        text = delivered_text(envelope)
        if text:
            self.delivered.append(text)

    @property
    def has_work(self) -> bool:
        return bool(self.work.trace or self.work.open_calls)

    def delivered_transcript(self) -> str:
        if not self.delivered:
            return "(nothing was shown to the user in this turn)"
        text = "\n---\n".join(self.delivered)
        return text if len(text) <= MAX_DELIVERED_CHARS else "...\n" + text[-MAX_DELIVERED_CHARS:]


def sw_trace(calls: list[dict[str, Any]]) -> list[str]:
    """An sw turn's tool calls (sw_history.TurnRecorder.calls) as the trace lines the verifier reads, the same shape as
    LadderTurn.observe writes for a Claude turn."""
    return [f"- {c.get('tool')}({c.get('args')}) -> {c.get('result')}" for c in calls][-TRACE_LINES_KEPT:]


DUPLICATE_SHARE = 0.5       # the share of the SHORTER text's words found in the other one that makes the two a repeat
MIN_STEMS_FOR_COMPARISON = 4  # a shorter text than this ("Done.") proves nothing about being a repeat
DUPLICATE_SHARE_ALL = 0.75   # ... or found across all of the recent messages together


def _stems(text: str) -> set[str]:
    """Word stems (the first five letters of each word of four or more), so that an inflected or reworded repeat
    still matches: 'пропускаю' / 'пропускаться'."""
    words = re.findall(r"[^\W\d_]{4,}", (text or "").lower())
    return {w[:5] for w in words}


def is_duplicate(report: str, earlier_messages: list[str]) -> bool:
    """True when what the report says has already been said to the user: most of its words are in one earlier message,
    or in the recent ones taken together (2026-10-26: a report restated the answer shown a few seconds before it)."""
    wanted = _stems(report)
    if not wanted:
        return False
    seen: set[str] = set()
    for message in earlier_messages:
        have = _stems(message)
        smaller = min(len(wanted), len(have))
        if smaller >= MIN_STEMS_FOR_COMPARISON and len(wanted & have) / smaller >= DUPLICATE_SHARE:
            return True
        seen |= have
    return bool(seen) and len(wanted & seen) / len(wanted) >= DUPLICATE_SHARE_ALL


def delivered_text(envelope: Any) -> str:
    """The visible text of a message that was sent to the window; '' for anything else (status, results, ...)."""
    if not isinstance(envelope, dict) or envelope.get("type") != "sdk_message":
        return ""
    wire = envelope.get("message")
    if not isinstance(wire, dict) or wire.get("type") != "assistant" or wire.get("parent_tool_use_id"):
        return ""
    text = visible_text(wire).strip()
    if not text or "[[NO_UPDATE]]" in text:
        return ""
    return f"[progress note] {text}" if envelope.get("isNarration") else text
