"""The model ladder of a Claude tab: every turn starts on Haiku; when an outside verifier
(the cascade's own, small_model_engine.verify_claude_attempt) finds the attempt did not finish
the task, the same session goes on with Sonnet, and only if that fails too, with Opus.

Same idea as the sw cascade (nano -> mini -> Sonnet -> Opus), carried onto the Claude CLI:
one long-lived session, so the next rung sees everything the previous one did and said --
the model is switched with the SDK's set_model, nothing is replayed. What a rung says is
held back until the verifier has ruled on it, so the user reads only the answer that
passed (or the top rung's).

This module is the turn's state and the pure parts (what to hold, what the verifier is
shown, what the next rung is told); the wiring into the message loop is in
chat_session_mixins/ladder.py. Unset model override means "ladder"; a model chosen in the
settings pins that model and turns the ladder off."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# The CLI's family aliases (subscription_mode.CLAUDE_MODEL_ALIASES), cheapest first.
RUNGS: tuple[str, ...] = ("haiku", "sonnet", "opus")
# Tool results longer than this are cut in the verifier's trace (it only needs to see what happened).
TRACE_RESULT_CHARS = 4500
TRACE_LINES_KEPT = 40
NO_UPDATE_SENTINEL = "[[NO_UPDATE]]"


@dataclass
class LadderTurn:
    """One turn's walk up the ladder. `active` until the turn is settled (the verdict was
    "done", or the top rung answered)."""

    user_text: str
    rung: int = 0
    active: bool = True
    held: list[dict[str, Any]] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    open_calls: dict[str, tuple[str, str]] = field(default_factory=dict)
    escalations: list[str] = field(default_factory=list)

    @property
    def model(self) -> str:
        return RUNGS[self.rung]

    @property
    def is_top(self) -> bool:
        return self.rung >= len(RUNGS) - 1

    def observe(self, wire: dict[str, Any]) -> None:
        """Records the real tool calls and their results of this rung, for the verifier."""
        content = (wire.get("message") or {}).get("content")
        if not isinstance(content, list) or wire.get("parent_tool_use_id"):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and wire.get("type") == "assistant":
                args = json.dumps(block.get("input") or {}, ensure_ascii=False, default=str)
                self.open_calls[str(block.get("id"))] = (str(block.get("name")), args[:600])
            elif block.get("type") == "tool_result" and wire.get("type") == "user":
                name, args = self.open_calls.pop(str(block.get("tool_use_id")), ("?", "{}"))
                self.trace.append(f"- {name}({args}) -> {_result_text(block.get('content'))[:TRACE_RESULT_CHARS]}")
        del self.trace[:-TRACE_LINES_KEPT]

    def should_hold(self, wire: dict[str, Any]) -> bool:
        """What waits for the verdict: the result, and the assistant's own text. Tool work
        (tool_use-only messages, tool results) goes through, so the tab shows it is working."""
        if wire.get("type") == "result":
            return True
        if wire.get("type") != "assistant" or wire.get("parent_tool_use_id"):
            return False
        return bool(visible_text(wire))

    def next_rung(self, note: str) -> None:
        self.escalations.append(f"{self.model}: {note}")
        self.rung += 1
        self.held = []
        self.trace = []
        self.open_calls = {}


def visible_text(wire: dict[str, Any]) -> str:
    content = (wire.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b.get("text", "") for b in content
        if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
    )


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def candidate_text(turn: LadderTurn, result_text: str | None) -> str:
    """The reply under judgement: the result message's own text, else the last text the
    assistant held back."""
    if result_text and result_text.strip():
        return result_text.strip()
    for envelope in reversed(turn.held):
        text = visible_text(envelope.get("message") or {})
        if text:
            return text
    return ""


def needs_verdict(turn: LadderTurn, candidate: str, is_error: bool) -> bool:
    """False for what has nothing to judge: the top rung (nobody is left to hand it to), an
    error result, and a deliberately silent internal turn."""
    if turn.is_top or is_error:
        return False
    return NO_UPDATE_SENTINEL not in candidate


def continuation_note(turn: LadderTurn, missing: str) -> str:
    """What the next rung is told, as a message of its own in the same session. The user has
    not seen the previous rung's draft."""
    return (
        f"[Internal: an automatic check found that the previous attempt at the user's request did not finish the "
        f"task: {missing or 'it is incomplete'}. The user has NOT seen that attempt's reply. Everything the previous "
        f"attempt did and said is above. Do the remaining work yourself now, with your tools, and answer the user "
        f"directly -- do not mention this note or the previous attempt.]"
    )


def ladder_enabled(model_override: str | None, engine_kind: str, setting: Any = True) -> bool:
    """The ladder runs for the Claude engine when no model is pinned (and has not been switched off)."""
    return engine_kind == "claude" and not model_override and setting is not False


def user_text_of(message: dict[str, Any]) -> str:
    """The user's own words in a queued SDK input message (without the [Sent: ...] line and
    machine-marked blocks)."""
    content = (message.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        text = block.get("text", "") if isinstance(block, dict) and block.get("type") == "text" else ""
        if text and not text.startswith("[Sent:") and not text.startswith("⁣"):
            parts.append(text)
    return "\n".join(parts)
