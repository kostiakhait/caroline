"""Ports backend/src/compaction.ts -- ages out old context from a tab's
ever-growing resumed Claude Code session without ever touching the live/
original transcript. A session is never restarted on its own -- resume:
session_id persists across full app restarts -- so its .jsonl transcript
on disk only grows, and every historical tool_use/tool_result (screenshots
especially) gets resent as input on every subsequent turn.

Approach: fork the live session (the SDK's fork_session() -- the only
sanctioned way to get a *new* session file with correctly remapped uuids/
parentUuid chain; there is no SDK API to filter an existing session's
content in place), then hand-edit the resulting COPY's .jsonl lines
directly. Mutating the copy is safe -- nothing reads it until the caller
points resume: at it and restarts the query(). The original stays
untouched, so a stub note pointing back at it always resolves to
something real.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from claude_agent_sdk import fork_session

from app.durability import claude_project_dir
from app.logging_setup import log_event

ONE_HOUR_MS = 60 * 60 * 1000
ONE_DAY_MS = 24 * ONE_HOUR_MS

# Replaces an old ">1h" age gate for images/tool payloads/text -- a byte
# budget tracks the actual API-context-size constraint directly instead of
# guessing via a clock. Lowered 100KB -> 50KB once this budget also became
# enforced every turn (dehydrate.py's age_previous_turns_in_place), not
# just hourly/reactively -- the hourly pass here is now a redundant
# backstop, not the primary enforcement. Shared with dehydrate.py so the
# two never drift apart.
RECENT_CONTENT_BUDGET_BYTES = 50 * 1024  # 50KB


def _parse_entry_age_ms(entry: dict[str, Any], now_ms: float) -> float | None:
    timestamp = entry.get("timestamp")
    if not isinstance(timestamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return now_ms - parsed.timestamp() * 1000


def _stub_note(parent_path: str, detail: str) -> dict[str, Any]:
    return {
        "type": "text",
        "text": f"[{detail}, вытеснено из недавнего контекста при плановом сжатии -- это НЕ прошлая сессия, "
        f"разговор продолжается тот же самый, просто часть истории убрана из контекста для экономии места. "
        f"Полное содержимое сохранено в файле: {parent_path}. Если содержимое сейчас нужно -- прочитай файл "
        f"сам(а) через Read; не спрашивай пользователя и не называй это \"прошлой сессией\".]",
    }


def _stub_text_if_worthwhile(text: str, parent_path: str, detail: str) -> dict[str, Any]:
    """Only replaces a text block if the replacement is actually smaller
    -- no point shrinking a two-word reply into a longer stub note."""
    stub = _stub_note(parent_path, detail)
    if len(text.encode("utf-8")) <= len(stub["text"].encode("utf-8")):
        return {"type": "text", "text": text}
    return stub


def _filter_tool_result_block(block: dict[str, Any], parent_path: str) -> dict[str, Any]:
    """Rewrites a tool_result block's content in place: image blocks ->
    stub, plain text blocks inside the array -> stub too, or the whole
    string -> stub (string-content case -- Read, Bash, Grep, and most
    other tools return their result as a plain string, not an array)."""
    if block.get("type") != "tool_result":
        return block
    content = block.get("content")
    if isinstance(content, list):
        filtered_inner = []
        for inner in content:
            if inner.get("type") == "image":
                filtered_inner.append(_stub_note(parent_path, "скриншот"))
            elif inner.get("type") == "text" and isinstance(inner.get("text"), str):
                filtered_inner.append(_stub_text_if_worthwhile(inner["text"], parent_path, "результат инструмента"))
            else:
                filtered_inner.append(inner)
        return {**block, "content": filtered_inner}
    if isinstance(content, str):
        return {**block, "content": _stub_note(parent_path, "результат инструмента")["text"]}
    return block


def _age_out_tool_content(content: list[dict[str, Any]], parent_path: str) -> list[dict[str, Any]]:
    """Applied to any entry the caller's recent-content-budget pass
    determined is NOT within the live budget window: strip image bytes,
    non-image tool_use/tool_result payloads, and plain text (only if the
    stub would actually be smaller)."""
    out = []
    for block in content:
        if block.get("type") == "image":
            out.append(_stub_note(parent_path, "скриншот"))
        elif block.get("type") == "tool_result":
            out.append(_filter_tool_result_block(block, parent_path))
        elif block.get("type") == "tool_use":
            # Only shrink `input` -- NOT add any extra property. An
            # ad-hoc extra field here previously poisoned every resume
            # that touched a stubbed entry (the API rejects unrecognized
            # fields on a replayed content block).
            out.append({**block, "input": {}})
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            out.append(_stub_text_if_worthwhile(block["text"], parent_path, "текст"))
        else:
            out.append(block)
    return out


def process_entry(entry: dict[str, Any], now_ms: float, parent_path: str, keep_live: bool) -> dict[str, Any]:
    """Exported for direct unit testing of the filter logic without
    touching fork_session/fs. `keep_live` -- computed by the caller's
    backward pass over ALL entries -- true means this entry falls within
    the most recent RECENT_CONTENT_BUDGET_BYTES of real (<=1day-old)
    content and stays untouched; false means it gets aged out via
    _age_out_tool_content, UNLESS it's also >1day old, in which case the
    whole turn collapses instead (independent of the byte budget)."""
    if entry.get("type") not in ("user", "assistant"):
        return entry
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return entry
    age_ms = _parse_entry_age_ms(entry, now_ms)
    if age_ms is None:
        return entry

    if age_ms > ONE_DAY_MS:
        # Whole turn collapses to one note, regardless of the byte budget
        # -- uuid/parentUuid untouched so the chain stays walkable.
        message = {**(entry.get("message") or {}), "content": [_stub_note(parent_path, "часть диалога")]}
        if entry.get("type") == "assistant" and message.get("stop_reason") == "tool_use":
            message["stop_reason"] = "end_turn"
        return {**entry, "message": message}
    if not keep_live:
        return {**entry, "message": {**(entry.get("message") or {}), "content": _age_out_tool_content(content, parent_path)}}
    return entry


@dataclass
class CompactionResult:
    new_session_id: str
    compacted_at: float
    parent_path: str


async def compact_session_if_due(
    workspace_dir: str, current_session_id: str, last_compacted_at: float | None, force: bool = False,
) -> CompactionResult | None:
    """Returns None if it's not yet due (last_compacted_at within the last
    hour) unless `force` is set. `force` is for the urgent-compaction path
    (a "Prompt is too long" turn). last_compacted_at is None (nothing
    recorded yet) always runs immediately.

    Never raises -- background, optional housekeeping; any failure here
    must never take down or block the live conversation. Purely
    algorithmic throughout (local file reads/rewrites only, no model/API
    call) so it works the same regardless of whether any chat source
    currently has usable tokens."""
    now = time.time() * 1000
    if not force and last_compacted_at is not None and now - last_compacted_at < ONE_HOUR_MS:
        return None

    parent_path = str(claude_project_dir(workspace_dir) / f"{current_session_id}.jsonl")
    fork_started_at = time.monotonic()
    # fork_session is synchronous (file I/O) -- offload so it never blocks
    # the event loop other tabs/connections are sharing.
    result = await asyncio.to_thread(fork_session, current_session_id, directory=workspace_dir)
    new_session_id = result.session_id
    log_event("engine", "compaction_forked", current_session_id=current_session_id, new_session_id=new_session_id, duration_ms=round((time.monotonic() - fork_started_at) * 1000, 1))

    fork_path = claude_project_dir(workspace_dir) / f"{new_session_id}.jsonl"
    raw = fork_path.read_text(encoding="utf-8")
    lines = [l for l in raw.split("\n") if l]

    # fork_session() never copies CLI-internal bookkeeping entries into a
    # fork, by design, every time -- confirmed harmless: a fork missing
    # those resumes and responds completely normally, so a missing
    # bookkeeping entry is NOT treated as a sign of a broken fork here.
    parsed: list[dict[str, Any] | None] = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except Exception as exc:
            log_event("engine", "compaction_parse_failed", fork_path=str(fork_path), error=str(exc))
            parsed.append(None)

    # Backward pass (newest -> oldest): keep the most recent
    # RECENT_CONTENT_BUDGET_BYTES of real (<=1day-old) content live.
    # >1day entries never consume the budget -- they're collapsed by the
    # separate age rule inside process_entry regardless.
    keep_live = [False] * len(parsed)
    budget_remaining = RECENT_CONTENT_BUDGET_BYTES
    for i in range(len(parsed) - 1, -1, -1):
        entry = parsed[i]
        if not entry or entry.get("type") not in ("user", "assistant"):
            continue
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list) or not entry.get("timestamp"):
            continue
        age_ms = _parse_entry_age_ms(entry, now)
        if age_ms is None or age_ms > ONE_DAY_MS:
            continue
        if budget_remaining <= 0:
            continue
        keep_live[i] = True
        budget_remaining -= len(json.dumps(content, ensure_ascii=False).encode("utf-8"))

    rewritten = []
    for i, line in enumerate(lines):
        entry = parsed[i]
        if entry is None:
            rewritten.append(line)  # already logged above
            continue
        try:
            rewritten.append(json.dumps(process_entry(entry, now, parent_path, keep_live[i]), ensure_ascii=False))
        except Exception as exc:
            log_event("engine", "compaction_process_failed", fork_path=str(fork_path), line=i, error=str(exc))
            rewritten.append(line)
    fork_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    return CompactionResult(new_session_id=new_session_id, compacted_at=now, parent_path=parent_path)


async def get_session_file_size_bytes(workspace_dir: str, session_id: str) -> int | None:
    """Plain filesystem stat -- no SDK call, no model call, nothing that
    can hang. Urgent compaction triggers on this signal directly (not on
    whatever the SDK does or doesn't say) since a session too big to even
    reach 'init' may never produce a "Prompt is too long" message (or any
    message) at all. Returns None if the file doesn't exist yet."""
    try:
        file_path = claude_project_dir(workspace_dir) / f"{session_id}.jsonl"
        return file_path.stat().st_size
    except Exception as exc:
        log_event("engine", "get_session_file_size_failed", session_id=session_id, error=str(exc))
        return None
