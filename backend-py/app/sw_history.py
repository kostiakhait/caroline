"""The sw (small-model) engine's own memory across turns -- Phase 2b of
docs/SW_MODE_TESTING_PLAN.md, per explicit instruction (2026-10-22): Caroline
on SW must work no worse than on the Claude SDK, and the SDK replays a turn's
whole trajectory into the next one. Before this, an sw turn saw only the last
12 lines of visible dialogue; its tool calls and their results were gone the
moment it ended.

Layout, per tab session (workspace/sw-history/<session_id>/):
- calls/<turn_id>-<n>-<tool>.json -- one file per tool call: full arguments
  and the full result, written by TurnRecorder's executor wrapper, which sees
  the result before resolve_agentic cuts it to 8000 characters.
- history.jsonl -- append-only records: "turn" (the user's message, the
  final reply, a preview of each call with its file link), "turn_summary"
  (the sw.turn.tools.summarize microagent's linked record of that turn's tool
  work, added once it finishes) and "compaction" (sw.history.compact's
  summary of everything up to a given turn).

The next turn's context is the latest compaction summary plus every turn
after it: the user's message, then the assistant's tool-work record and its
reply. The Claude Code transcript keeps getting the plain question/answer
pair as before (chat_session_shared._append_small_model_turn_to_session) --
the narrator, language detection and the 24h-dialogue file read that one.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.logging_setup import log_event

# Per explicit instruction (2026-10-22): starting values, to be tuned against
# measured cost.
COMPACT_THRESHOLD_TOKENS = 40_000
KEEP_RECENT_TURNS = 6

PREVIEW_CHARS = 300
_CHARS_PER_TOKEN = 4


def _session_dir(workspace_dir: str, session_id: str) -> Path:
    return Path(workspace_dir) / "sw-history" / session_id


def _history_path(workspace_dir: str, session_id: str) -> Path:
    return _session_dir(workspace_dir, session_id) / "history.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _preview(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "..."


class TurnRecorder:
    """Records one sw turn's tool calls. wrap() returns an executor_fn that
    behaves exactly like the one it wraps and, after every call that returns,
    writes that call to its own file. Thread-safe: resolve_agentic calls the
    executor from a worker thread, and parallel-stage branches from several
    at once."""

    def __init__(self, workspace_dir: str, session_id: str) -> None:
        self.session_id = session_id
        self.turn_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.calls: list[dict[str, Any]] = []
        self._calls_dir = _session_dir(workspace_dir, session_id) / "calls"
        self._lock = threading.Lock()

    def record(self, name: str, args: dict[str, Any], result: str) -> None:
        with self._lock:
            n = len(self.calls) + 1
            safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:60]
            path = self._calls_dir / f"{self.turn_id}-{n:03d}-{safe_name}.json"
            self.calls.append({"n": n, "tool": name, "args": _preview(args), "result": _preview(result), "ref": str(path)})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"tool": name, "args": args, "result": result, "ts": _now_iso()}, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def wrap(self, executor_fn: Callable[[str, dict[str, Any]], str], skip: frozenset[str] | set[str] = frozenset()) -> Callable[[str, dict[str, Any]], str]:
        def recording_executor(name: str, args: dict[str, Any]) -> str:
            result = executor_fn(name, args)
            if name not in skip:
                try:
                    self.record(name, args or {}, str(result))
                except Exception as exc:  # noqa: BLE001 -- recording must never cost the call itself
                    log_event("engine", "sw_history_record_failed", tool=name, error=str(exc))
            return result

        return recording_executor


def _append(workspace_dir: str, session_id: str, record: dict[str, Any]) -> None:
    path = _history_path(workspace_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({**record, "ts": _now_iso()}, ensure_ascii=False, default=str) + "\n")


def append_turn(workspace_dir: str, session_id: str, turn_id: str, user_text: str, answer: str, calls: list[dict[str, Any]]) -> None:
    _append(workspace_dir, session_id, {"type": "turn", "turn_id": turn_id, "user": user_text, "answer": answer, "calls": calls})


def load(workspace_dir: str, session_id: str) -> tuple[str | None, list[dict[str, Any]]]:
    """(the latest compaction summary or None, every turn after it in order,
    each with its "tools_summary" merged in when one exists)."""
    path = _history_path(workspace_dir, session_id)
    if not path.exists():
        return None, []
    turns: list[dict[str, Any]] = []
    summaries: dict[str, str] = {}
    compaction: dict[str, Any] | None = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = rec.get("type")
            if kind == "turn":
                turns.append(rec)
            elif kind == "turn_summary":
                summaries[rec.get("turn_id")] = rec.get("summary") or ""
            elif kind == "compaction":
                compaction = rec
    for t in turns:
        if t.get("turn_id") in summaries:
            t["tools_summary"] = summaries[t["turn_id"]]
    if compaction is not None:
        ids = [t.get("turn_id") for t in turns]
        upto = compaction.get("upto_turn_id")
        if upto in ids:
            turns = turns[ids.index(upto) + 1:]
        return compaction.get("summary") or None, turns
    return None, turns


def _tools_text(turn: dict[str, Any]) -> str:
    if turn.get("tools_summary"):
        return turn["tools_summary"]
    return "\n".join(
        f"- {c.get('tool')}({c.get('args')}) -> {c.get('result')} ({c.get('ref')})" for c in turn.get("calls") or []
    )


def context_messages(turns: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The turns as chat messages for the next sw turn's context."""
    messages: list[dict[str, str]] = []
    for t in turns:
        messages.append({"role": "user", "content": t.get("user") or ""})
        parts = []
        if t.get("calls"):
            parts.append(
                "[Tool work in this turn -- the full record of each call is in the linked file, open it with "
                f"read_file when you need the details:\n{_tools_text(t)}]"
            )
        parts.append(t.get("answer") or "(no visible reply)")
        messages.append({"role": "assistant", "content": "\n\n".join(parts)})
    return messages


def summary_section(summary: str | None) -> str | None:
    if not summary:
        return None
    return (
        "EARLIER IN THIS CONVERSATION (a compressed summary of the turns no longer shown in full -- treat it "
        "as your own memory of them):\n" + summary
    )


def estimate_tokens(summary: str | None, turns: list[dict[str, Any]]) -> int:
    size = len(summary or "") + len(json.dumps(context_messages(turns), ensure_ascii=False))
    return size // _CHARS_PER_TOKEN


async def _summarize_turn_tools(user_text: str, answer: str, calls: list[dict[str, Any]]) -> str | None:
    from app.microagents.sw_turn_summary import TURN_TOOLS_SUMMARY
    from app.plugins.voice_api import _run_local_microagent

    result = await _run_local_microagent(
        TURN_TOOLS_SUMMARY, "app.microagents.sw_turn_summary", {"user_text": user_text, "answer": answer, "calls": calls},
    )
    if result is None or result.get("status") != "ok":
        log_event("engine", "sw_turn_summary_failed", error=(result or {}).get("error"))
        return None
    return ((result.get("output") or {}).get("summary") or "").strip() or None


async def _compact(previous_summary: str | None, turns: list[dict[str, Any]]) -> str | None:
    from app.microagents.sw_history_compact import HISTORY_COMPACT
    from app.plugins.voice_api import _run_local_microagent

    payload = [{"user": t.get("user") or "", "tools": _tools_text(t), "answer": t.get("answer") or ""} for t in turns]
    result = await _run_local_microagent(
        HISTORY_COMPACT, "app.microagents.sw_history_compact", {"previous_summary": previous_summary or "", "turns": payload},
    )
    if result is None or result.get("status") != "ok":
        log_event("engine", "sw_history_compaction_failed", error=(result or {}).get("error"))
        return None
    return ((result.get("output") or {}).get("summary") or "").strip() or None


async def after_turn(workspace_dir: str, session_id: str, tab_id: str, recorder: TurnRecorder, user_text: str, answer: str) -> None:
    """Background work once a turn is saved: the linked summary of its tool
    work, then a compaction if the history has outgrown its budget. The next
    sw turn waits for this before building its context."""
    try:
        if recorder.calls:
            started = time.monotonic()
            summary = await _summarize_turn_tools(user_text, answer, recorder.calls)
            if summary:
                _append(workspace_dir, session_id, {"type": "turn_summary", "turn_id": recorder.turn_id, "summary": summary})
            log_event(
                "engine", "sw_turn_summary_done", tab_id=tab_id, turn_id=recorder.turn_id, calls=len(recorder.calls),
                ok=bool(summary), ms=round((time.monotonic() - started) * 1000),
            )
        previous, turns = load(workspace_dir, session_id)
        tokens = estimate_tokens(previous, turns)
        if len(turns) <= KEEP_RECENT_TURNS or tokens <= COMPACT_THRESHOLD_TOKENS:
            return
        to_fold = turns[:-KEEP_RECENT_TURNS]
        started = time.monotonic()
        compacted = await _compact(previous, to_fold)
        if compacted:
            _append(workspace_dir, session_id, {"type": "compaction", "upto_turn_id": to_fold[-1]["turn_id"], "summary": compacted})
        log_event(
            "engine", "sw_history_compacted", tab_id=tab_id, tokens_before=tokens, turns_folded=len(to_fold),
            ok=bool(compacted), ms=round((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 -- background bookkeeping must never surface as a turn error
        log_event("engine", "sw_history_after_turn_failed", tab_id=tab_id, error=str(exc), error_type=type(exc).__name__)
