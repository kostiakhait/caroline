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

from dataclasses import dataclass, field
from typing import Any

from app.claude_ladder import LadderTurn, visible_text

MAX_DELIVERED_CHARS = 6000


@dataclass
class TurnRecord:
    """One real-user turn: the request, the work done (tool calls with results) and what was delivered."""

    user_text: str
    work: LadderTurn = field(init=False)
    delivered: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.work = LadderTurn(user_text=self.user_text)

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
