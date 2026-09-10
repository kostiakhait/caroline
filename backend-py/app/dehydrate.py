"""Ports backend/src/dehydrate.ts -- rewrites the LIVE session .jsonl file
IN PLACE, every turn, replacing raw image/document bytes with a link to a
file already on disk, and (agePreviousTurnsInPlace) collapsing everything
older than a recent-content byte budget into one reference entry. Safe
ONLY because of WHEN the two call sites (chat_session.py) invoke it -- both
are points where no turn is in flight and the CLI subprocess is not
currently reading/writing the file:
  - runLoop's pre-resume check, before query() is even created.
  - inputStream()'s generator, awaited right before yielding the NEXT
    queued turn to the CLI -- the CLI only asks for its next prompt once
    it has fully finished (and flushed to disk) the previous turn.
Never call this from anywhere else, and never let a call race a live
message-consuming loop for the CURRENT turn.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.durability import claude_project_dir
from app.logging_setup import log_event

DEHYDRATED_DIR_NAME = "dehydrated"


def dehydrated_dir(workspace_dir: str) -> Path:
    """Exported so a future expand-on-click handler can validate a
    requested path is actually inside this directory before reading it."""
    return Path(workspace_dir) / DEHYDRATED_DIR_NAME


def _extension_for(media_type: Any) -> str:
    """media_type "image/png" -> "png", "application/pdf" -> "pdf". Falls
    back to "bin" for anything unrecognized rather than failing the whole
    pass."""
    if not isinstance(media_type, str):
        return "bin"
    parts = media_type.split("/")
    if len(parts) < 2 or not parts[1]:
        return "bin"
    return parts[1].split("+")[0]


def _write_dehydrated_file(workspace_dir: str, media_type: Any, base64_data: str) -> str:
    directory = dehydrated_dir(workspace_dir)
    directory.mkdir(parents=True, exist_ok=True)
    file_path = directory / f"{uuid.uuid4()}.{_extension_for(media_type)}"
    file_path.write_bytes(base64.b64decode(base64_data))
    return str(file_path)


def _write_dehydrated_text_file(workspace_dir: str, content: str) -> str:
    directory = dehydrated_dir(workspace_dir)
    directory.mkdir(parents=True, exist_ok=True)
    file_path = directory / f"{uuid.uuid4()}.txt"
    file_path.write_text(content, encoding="utf-8")
    return str(file_path)


def _format_timestamp_for_model(dt: datetime) -> str:
    """Best-effort port of formatTimestampForModel's JS toLocaleString
    (en-US, weekday/year/month/day short + hour:minute + short tz name) --
    converts to the system's local timezone first, matching the original's
    unqualified `new Date(...).toLocaleString(...)` (no explicit
    timeZone), then formats by hand rather than relying on
    platform-specific strftime flags (Windows lacks POSIX's %-d)."""
    local = dt.astimezone()
    weekday = local.strftime("%a")
    month = local.strftime("%b")
    hour12 = local.strftime("%I").lstrip("0") or "0"
    minute = local.strftime("%M")
    ampm = local.strftime("%p")
    tz_name = local.strftime("%Z")
    parts = f"{weekday}, {month} {local.day}, {local.year}, {hour12}:{minute} {ampm}"
    return f"{parts} {tz_name}".strip()


# Per explicit instruction: pushMessage's own "[Sent: ...]" line only ever
# lands on a real/proactive USER turn -- every OTHER entry (a
# tool_result-only user entry, every assistant entry) has no timestamp at
# all once it reaches the model, even though the CLI itself already
# records one on disk. Stamped once per entry here. Skipped for entries
# that already start with a "[Sent: " block OR this function's OWN stamp
# shape, so neither ever gets double-stamped (a real bug found live:
# session-id oscillation caused repeated rescans from line 0, and an
# earlier version only checked for "[Sent: ", producing 350+ duplicate
# stamps on some entries).
_TIMESTAMP_STAMP_PATTERN = re.compile(r"^\[(Sent: |(Sun|Mon|Tue|Wed|Thu|Fri|Sat), )")


def _stamp_timestamp_if_missing(entry: dict[str, Any], content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not content:
        return content
    timestamp = entry.get("timestamp")
    if not isinstance(timestamp, str):
        return content
    # Bug fix (2026-09-10): confirmed live as the root cause of a chronic
    # "API Error: 400 due to tool use concurrency issues." (262 hits in one
    # prod-log window). With the interleaved-thinking beta active (CLI
    # 2.1.x default, effort=high), the Anthropic API is strict about the
    # SHAPE of any assistant message that carries a tool_use block when
    # it's replayed on resume: a leading `thinking`/`redacted_thinking`
    # block must stay first, and you may not slip a bare `text` block in
    # front of a `tool_use` that has no preceding thinking. This function
    # used to just splice the stamp in at index 0 unconditionally, which
    # did exactly that on every tool-call turn. Now: skip any leading
    # thinking run, and only stamp if the block landed on is itself a
    # `text` block -- never ahead of a tool_use / tool_result / image /
    # bare thinking entry.
    insert_at = 0
    while insert_at < len(content) and content[insert_at].get("type") in ("thinking", "redacted_thinking"):
        insert_at += 1
    anchor = content[insert_at] if insert_at < len(content) else None
    if not (anchor and anchor.get("type") == "text" and isinstance(anchor.get("text"), str)):
        return content
    if _TIMESTAMP_STAMP_PATTERN.match(anchor["text"]):
        return content  # already stamped (this entry, or an earlier rescan)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return content
    stamp = {"type": "text", "text": f"[{_format_timestamp_for_model(parsed)}]"}
    return [*content[:insert_at], stamp, *content[insert_at:]]


def _dehydrated_note(detail: str, file_path: str) -> dict[str, Any]:
    """Same tone/shape as compaction.py's stub_note() -- kept SEPARATE
    (not shared) since the two mean different things: that one says "aged
    out, not resent"; this one says "already on disk from THIS same turn,
    and won't be resent from here on"."""
    return {
        "type": "text",
        "text": f"[{detail}, вытеснено на диск по завершении хода -- это НЕ прошлая сессия, это часть ТЕКУЩЕГО, "
        f"непрерывающегося разговора, просто убранная из контекста для экономии места, и не передаётся повторно "
        f"автоматически. Сохранено в файле: {file_path}. Если содержимое сейчас нужно -- прочитай файл сам(а) "
        f"через Read; не спрашивай пользователя, не проси прислать это заново и не называй это \"прошлой "
        f"сессией\".]",
    }


# Bug fix ported as-is: \w in a JS regex without "u" is ASCII-only, so an
# earlier version of this pattern never matched Cyrillic. Match the actual
# gendered endings ("сохранено"/"сохранена") explicitly instead.
_EXTRACT_PATH_RE = re.compile(r"[Сс]охранен[оа] в файле: (.+?)\. ")


def extract_dehydrated_file_path(text: str) -> str | None:
    """Lets a frontend detect one of OUR OWN notes (any of this module's
    templates) and pull out the exact path to offer as a clickable
    expand-in-place link instead of a raw path in prose."""
    m = _EXTRACT_PATH_RE.search(text)
    return m.group(1) if m else None


def _dehydrate_block(block: dict[str, Any], workspace_dir: str) -> tuple[dict[str, Any], bool]:
    """Recurses into tool_result.content (screenshots and similar
    tool-produced images live there, never at the top level of a
    message). Returns the SAME block object (identity-equal-ish; here
    just the same dict) when nothing changed, so the caller can cheaply
    tell "nothing changed"."""
    source = block.get("source") if isinstance(block.get("source"), dict) else None
    if block.get("type") == "image" and source and source.get("type") == "base64" and isinstance(source.get("data"), str):
        file_path = _write_dehydrated_file(workspace_dir, source.get("media_type"), source["data"])
        return _dehydrated_note("Изображение", file_path), True
    if block.get("type") == "document" and source and source.get("type") == "base64" and isinstance(source.get("data"), str):
        file_path = _write_dehydrated_file(workspace_dir, source.get("media_type"), source["data"])
        return _dehydrated_note("Документ", file_path), True
    tool_result_content = block.get("content")
    if block.get("type") == "tool_result" and isinstance(tool_result_content, list):
        any_changed = False
        new_inner = []
        for inner in tool_result_content:
            new_block, changed = _dehydrate_block(inner, workspace_dir)
            if changed:
                any_changed = True
            new_inner.append(new_block)
        if not any_changed:
            return block, False
        return {**block, "content": new_inner}, True
    return block, False


def _dehydrate_entry(entry: dict[str, Any], workspace_dir: str) -> tuple[dict[str, Any], bool]:
    """Replaces raw image/document bytes with an on-disk reference, per turn.

    Bug fix (2026-09-10): this USED to also strip every `thinking` block
    ("scratch work, the outcome is what matters going forward"). That was
    the other half of a chronic "API Error: 400 due to tool use
    concurrency issues." on resume (262 hits in one prod-log window):
    with interleaved extended thinking on (CLI 2.1.x default), a tool-call
    assistant turn MUST still carry its leading `thinking` block when the
    session is resumed -- stripping it (or, see _stamp_timestamp_if_missing,
    shoving a text block in front of it) makes the API reject the whole
    replayed transcript. Genuinely old thinking still gets reclaimed
    wholesale by age_previous_turns_in_place once the transcript grows
    past the recent-content byte budget; it just isn't picked apart
    block-by-block on the live tail anymore."""
    if entry.get("type") not in ("user", "assistant"):
        return entry, False
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return entry, False
    any_changed = False
    new_content: list[dict[str, Any]] = []
    for block in content:
        new_block, changed = _dehydrate_block(block, workspace_dir)
        if changed:
            any_changed = True
        new_content.append(new_block)
    final_content = new_content if new_content else [{"type": "text", "text": "[пустой ход]"}]
    stamped_content = _stamp_timestamp_if_missing(entry, final_content)
    if stamped_content is not final_content:
        any_changed = True
    if not any_changed:
        return entry, False
    return {**entry, "message": {**(entry.get("message") or {}), "content": stamped_content}}, True


@dataclass
class DehydrationOutcome:
    changed: bool
    entries_changed: int
    lines_rescanned: int
    new_through_line: int  # pass back in as already_through_line on the NEXT call for this session id


async def dehydrate_previous_turns(workspace_dir: str, session_id: str, already_through_line: int) -> DehydrationOutcome:
    """Rewrites lines [already_through_line, EOF) of session `session_id`'s
    live .jsonl in place. already_through_line lets the caller skip
    re-parsing lines it already confirmed clean in an earlier call THIS
    session lifetime -- the CLI only ever APPENDS to this file, never
    rewrites past entries. Pass 0 for a session id this ChatSession hasn't
    dehydrated yet. Never raises -- any failure is logged and treated as
    "nothing dehydrated this pass"."""
    file_path = claude_project_dir(workspace_dir) / f"{session_id}.jsonl"
    try:
        raw = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        log_event("engine", "dehydrate_read_failed", session_id=session_id, error=str(exc))
        return DehydrationOutcome(changed=False, entries_changed=0, lines_rescanned=0, new_through_line=already_through_line)

    lines = [l for l in raw.split("\n") if l]
    if len(lines) <= already_through_line:
        return DehydrationOutcome(changed=False, entries_changed=0, lines_rescanned=0, new_through_line=len(lines))

    entries_changed = 0
    any_changed = False
    rewritten_tail: list[str] = []
    for i in range(already_through_line, len(lines)):
        line = lines[i]
        try:
            entry = json.loads(line)
            new_entry, changed = _dehydrate_entry(entry, workspace_dir)
            if changed:
                any_changed = True
                entries_changed += 1
                rewritten_tail.append(json.dumps(new_entry, ensure_ascii=False))
            else:
                rewritten_tail.append(line)
        except Exception as exc:
            log_event("engine", "dehydrate_line_failed", session_id=session_id, line=i, error=str(exc))
            rewritten_tail.append(line)

    if any_changed:
        full_lines = lines[:already_through_line] + rewritten_tail
        file_path.write_text("\n".join(full_lines) + "\n", encoding="utf-8")
        log_event("engine", "dehydrate_rewrote", session_id=session_id, from_line=already_through_line, to_line=len(lines), entries_changed=entries_changed)

    return DehydrationOutcome(changed=any_changed, entries_changed=entries_changed, lines_rescanned=len(lines) - already_through_line, new_through_line=len(lines))


@dataclass
class AgeBudgetOutcome:
    changed: bool
    lines_collapsed: int  # how many lines were collapsed into the one reference entry (0 if nothing was outside budget)


DEHYDRATED_NOTE_MARKER = "вытеснено из истории по завершении хода"


def _is_own_reference_entry(entry: dict[str, Any]) -> bool:
    """Lets a later split re-sweep an earlier call's own reference entry
    (and everything after it up to the new split point) into a fresh
    combined file without any special-casing."""
    content = (entry.get("message") or {}).get("content")
    return (
        isinstance(content, list) and len(content) == 1
        and content[0].get("type") == "text" and isinstance(content[0].get("text"), str)
        and DEHYDRATED_NOTE_MARKER in content[0]["text"]
    )


def _reference_note(extracted_path: str) -> dict[str, Any]:
    return {
        "type": "text",
        "text": f"[Более старая часть ЭТОГО ЖЕ, непрерывающегося разговора (НЕ прошлая сессия) {DEHYDRATED_NOTE_MARKER} "
        f"-- не передаётся повторно автоматически. Полностью сохранена в файле: {extracted_path}. Если нужен более "
        f"ранний контекст -- прочитай файл сам(а) через Read; не спрашивай пользователя, не проси прислать это "
        f"заново и не называй это \"прошлой сессией\".]",
    }


async def age_previous_turns_in_place(workspace_dir: str, session_id: str) -> AgeBudgetOutcome:
    """In-place, per-turn equivalent of compaction.py's recent-content
    byte budget. Call AFTER dehydrate_previous_turns in the same pass --
    raw image/document bytes should already be off the live entries by
    the time this runs (thinking blocks are deliberately left intact on
    the tail now -- see _dehydrate_entry -- and get reclaimed here,
    wholesale, along with everything else past the budget)."""
    from app.compaction import RECENT_CONTENT_BUDGET_BYTES

    file_path = claude_project_dir(workspace_dir) / f"{session_id}.jsonl"
    try:
        raw = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        log_event("engine", "age_budget_read_failed", session_id=session_id, error=str(exc))
        return AgeBudgetOutcome(changed=False, lines_collapsed=0)

    lines = [l for l in raw.split("\n") if l]
    entries: list[dict[str, Any]] = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except Exception as exc:
            # Splitting requires a coherent view of the whole file -- a
            # single malformed line makes the split point unreliable, so
            # skip this pass entirely rather than risk collapsing the
            # wrong range. Will retry next turn.
            log_event("engine", "age_budget_parse_failed", session_id=session_id, error=str(exc))
            return AgeBudgetOutcome(changed=False, lines_collapsed=0)

    # Walk backward, accumulating real content bytes, to find the split
    # index -- the first (oldest) entry that's still within budget.
    # Everything before it (0..split_index) is the prefix to collapse.
    budget_remaining = RECENT_CONTENT_BUDGET_BYTES
    split_index = 0
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        content = (entry.get("message") or {}).get("content")
        is_budget_eligible = entry.get("type") in ("user", "assistant") and isinstance(content, list)
        if is_budget_eligible and budget_remaining > 0:
            budget_remaining -= len(json.dumps(content, ensure_ascii=False).encode("utf-8"))
        if budget_remaining <= 0:
            split_index = i
            break

    # A split landing between a tool_use-ending assistant entry and its
    # own tool_result reply (always the PHYSICALLY NEXT entry) orphans
    # that tool_result -- the API rejects the resumed session outright
    # the moment it's next resumed ("tool use concurrency issues").
    # Pull the tool_use entry (and transitively whatever's ahead of it)
    # into the live tail whenever this would happen.
    while split_index > 0:
        boundary = entries[split_index - 1]
        boundary_content = (boundary.get("message") or {}).get("content")
        next_content = (entries[split_index].get("message") or {}).get("content") if split_index < len(entries) else None
        if boundary.get("type") != "assistant" or not isinstance(boundary_content, list) or not isinstance(next_content, list):
            break
        tool_use_ids = {b["id"] for b in boundary_content if b.get("type") == "tool_use" and isinstance(b.get("id"), str)}
        if not tool_use_ids:
            break
        next_has_matching_result = any(b.get("type") == "tool_result" and b.get("tool_use_id") in tool_use_ids for b in next_content)
        if not next_has_matching_result:
            break
        split_index -= 1

    nothing_to_do = split_index == 0 or (split_index == 1 and _is_own_reference_entry(entries[0]))
    if nothing_to_do:
        return AgeBudgetOutcome(changed=False, lines_collapsed=0)

    prefix_lines = lines[:split_index]
    extracted_path = _write_dehydrated_text_file(workspace_dir, "\n".join(prefix_lines) + "\n")

    # The conversation tree is NOT strictly linear -- branches/retries can
    # share a common ancestor several lines back. Find EVERY dangling
    # parentUuid reference across the whole live tail (not just the one
    # immediately after the split) and redirect all of them.
    collapsed_uuids = {e["uuid"] for e in entries[:split_index] if isinstance(e.get("uuid"), str)}

    boundary_entry = entries[split_index - 1]
    reference_entry: dict[str, Any] = {
        **boundary_entry,
        "parentUuid": None,
        "message": {**(boundary_entry.get("message") or {}), "content": [_reference_note(extracted_path)]},
    }
    if reference_entry.get("type") == "assistant" and reference_entry.get("message", {}).get("stop_reason") == "tool_use":
        reference_entry["message"]["stop_reason"] = "end_turn"

    redirected = 0
    live_tail: list[str] = []
    for idx, entry in enumerate(entries[split_index:]):
        if isinstance(entry.get("parentUuid"), str) and entry["parentUuid"] in collapsed_uuids:
            redirected += 1
            live_tail.append(json.dumps({**entry, "parentUuid": reference_entry.get("uuid")}, ensure_ascii=False))
        else:
            live_tail.append(lines[split_index + idx])

    new_lines = [json.dumps(reference_entry, ensure_ascii=False), *live_tail]
    file_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    log_event(
        "engine", "age_budget_collapsed", session_id=session_id, lines_collapsed=split_index,
        extracted_path=extracted_path, redirected=redirected, remaining_lines=len(new_lines),
    )

    return AgeBudgetOutcome(changed=True, lines_collapsed=split_index)
