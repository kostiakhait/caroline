"""Ports backend/src/history.ts -- rebuilds the visible chat transcript from
the real Claude Code CLI session transcript (~/.claude/projects/<sanitized-
workspace-path>/*.jsonl), independent of the chat UI's own localStorage-based
echo. Emergency recovery path: the chat UI calls "get_history" once on
startup if its own localStorage transcript is empty (see chat.js), and
"expand_dehydrated_ref" reuses read_archived_entries to turn one of
dehydrate.py's archive files back into the same shape for a human to read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Iterator

from app.durability import claude_project_dir, session_transcript_path
from app.logging_setup import log_event

_ATTACHMENT_NOTE_PREFIXES = [
    "[This image is also saved at ",
    "[This document is also saved at ",
    "[Attached file saved to ",
]

# Bug fix (2026-09-26), confirmed live: Caroline's own auto-generated attachment notes
# ("[Attached PDF, 6 page(s)... <22K chars of extracted text>", "[This image is also saved
# at ...]", ...) were read back as if the user had typed them, and one English 22K-char
# PDF dump outweighed ~300 chars of the user's real Russian in language detection. A
# prefix list per note shape kept missing the next shape, so chat_session.py's
# _attachment_to_blocks now tags EVERY note block it generates with this marker at the
# single place that creates them, and _extract_text_and_attachments -- the one choke point
# every history/dialogue reader goes through -- drops any marked block from the user's
# text. A note shape added later is covered automatically. U+2063 (invisible separator),
# same convention as chat_session.py's _SYNTHETIC_TURN_MARKER.
ATTACHMENT_NOTE_MARKER = "\u2063[[caroline-attachment-note]]\u2063"

_SAVED_PATH_RE = re.compile(r"saved (?:to|at) (.+?)(?: -- |\]|$)")

_UUID_PREFIX_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-", re.IGNORECASE,
)

# Moved here from chat_session.py (2026-09-10) so this module -- the one
# that actually knows the raw content-block shape _push_message writes --
# can use it to find submit()-boundary stamps itself (see
# _split_into_submessages below). chat_session.py still imports this same
# instance for its own stamp-stripping needs.
_HISTORY_STAMP_PATTERN = re.compile(r"^\[(Sent: |(Sun|Mon|Tue|Wed|Thu|Fri|Sat), )[^\]]*\]\s*", re.IGNORECASE)


def _sanitize_project_dir_name(path: str) -> str:
    return re.sub(r"[\\:]", "-", path)


def _extract_attachment_note(block_text: str) -> dict[str, str] | None:
    block_text = block_text.removeprefix(ATTACHMENT_NOTE_MARKER)
    for prefix in _ATTACHMENT_NOTE_PREFIXES:
        if not block_text.startswith(prefix):
            continue
        rest = block_text[len(prefix):]
        dash_idx = rest.find(" -- ")
        saved_path = (rest[:dash_idx] if dash_idx >= 0 else re.sub(r"\.?\]\s*$", "", rest)).strip()
        base = re.split(r"[\\/]", saved_path)[-1] or saved_path
        name = _UUID_PREFIX_RE.sub("", base)
        # "path" points at the real on-disk file (workspace/uploads/... --
        # see chat_session.py's _save_attachment_to_uploads) so a consumer
        # that actually needs the bytes (companion_api.py's history sync,
        # for the Android app) can read and embed them. Desktop's own
        # chat.js never needs this (same machine, reads the WS
        # find_attachment op or the file directly) so this is additive,
        # not a behavior change for it.
        return {"name": name, "path": saved_path}
    return None


def _latest_session_file(workspace_dir: str) -> Path | None:
    project_dir = Path.home() / ".claude" / "projects" / _sanitize_project_dir_name(workspace_dir)
    if not project_dir.exists():
        return None
    files = [f for f in project_dir.iterdir() if f.suffix == ".jsonl"]
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_mtime)


def _extract_text_and_attachments(content: Any) -> tuple[str, list[dict[str, str]]]:
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []
    text_parts: list[str] = []
    attachments: list[dict[str, str]] = []
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "text" or not isinstance(b.get("text"), str):
            continue
        note = _extract_attachment_note(b["text"])
        if note:
            attachments.append(note)
        elif b["text"].startswith(ATTACHMENT_NOTE_MARKER):
            # A marked note whose shape has no entry in _ATTACHMENT_NOTE_PREFIXES (e.g. the
            # extracted-PDF-text note): never the user's own words, so never text -- keep
            # the file itself as an attachment when its saved path can be read out of it.
            m = _SAVED_PATH_RE.search(b["text"][:600])
            if m:
                saved_path = m.group(1).strip()
                base = re.split(r"[\\/]", saved_path)[-1] or saved_path
                attachments.append({"name": _UUID_PREFIX_RE.sub("", base), "path": saved_path})
        else:
            text_parts.append(b["text"])
    return "\n\n".join(text_parts), attachments


def _split_user_submessages(content: Any) -> list[Any]:
    """Bug fix (2026-09-10): confirmed live -- when multiple submit() calls
    (chat_session.py) queue up faster than the CLI drains the queue (always
    true right after a WS reconnect, when a startup greeting and a
    crash-resume nudge both fire back-to-back), the CLI can combine them
    into ONE "user" JSONL entry whose content list is just every
    submit()'s own blocks concatenated. _push_message always starts one
    logical submit() with a "[Sent: ...]"/weekday-stamp text block, so that
    stamp is a reliable per-submit() boundary -- split back into the
    original groups here, BEFORE text extraction, so each can be judged
    synthetic-vs-real independently. Without this, one flattened blob
    inherited whichever verdict its FIRST part deserved: a real user
    message that happened to queue behind a synthetic nudge (an edge case,
    but a real one -- confirmed by a live test where a message sent right
    after reconnect merged with the startup-greeting nudge) was being
    silently dropped along with the nudge it got stuck behind."""
    if not isinstance(content, list):
        return [content]
    groups: list[list[Any]] = []
    current: list[Any] = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) and _HISTORY_STAMP_PATTERN.match(b["text"]):
            if current:
                groups.append(current)
            current = [b]
        else:
            current.append(b)
    if current:
        groups.append(current)
    return groups or [content]


def _extract_entries_from_jsonl(raw: str, source_label: str) -> list[dict[str, Any]]:
    return _extract_entries_from_lines(raw.split("\n"), source_label)


def _extract_entries_from_lines(lines: Iterable[str], source_label: str) -> list[dict[str, Any]]:
    """The one real parser -- takes lines from ANY source (a whole file split
    in memory, a lazily-iterated file object, a single line handed over by
    iter_entries_reversed below) so a caller never has to materialize a
    whole multi-hundred-MB transcript just to reach its last few lines."""
    import time as _time

    entries: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if obj.get("type") not in ("user", "assistant"):
                continue
            content = (obj.get("message") or {}).get("content")
            # Bug fix (2026-09-10): for "user" entries only, undo a possible
            # multi-submit() merge before doing anything else -- see
            # _split_user_submessages's own docstring. Assistant entries
            # come straight from the SDK's own single response stream, one
            # per API round-trip, and are never merged this way.
            content_groups = _split_user_submessages(content) if obj.get("type") == "user" else [content]
            for group in content_groups:
                # Bug fix (2026-09-10): a message with a tool_use block
                # alongside text is, by construction, NOT the turn's final
                # answer -- one SDK "assistant" message is one API
                # round-trip with a single stop_reason, so any text
                # alongside a tool_use is pre-tool narration ("let me check
                # X", agentic self-talk), never something addressed to the
                # user. This mirrors chat.js's own live-rendering rule (its
                # hasToolUse check) exactly -- confirmed that rule already
                # exists client-side (2026-09-08 incident), but this
                # rebuild-from-disk path never replicated it, so a
                # reconnect/get_history replay (and the phone companion
                # mirror, which draws from this same function) could still
                # show internal tool narration as if it were a real chat
                # bubble.
                if obj.get("type") == "assistant" and isinstance(group, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_use" for b in group
                ):
                    continue
                text, attachments = _extract_text_and_attachments(group)
                if not text.strip() and not attachments:
                    continue
                # Bug fix (2026-09-09): this rebuild path is entirely
                # separate from the live sdk_message stream's own
                # [[NO_UPDATE]] suppression (see chat.js's assistant-message
                # handler) -- confirmed live, a no-update turn's full text
                # (explanation + trailing sentinel) was leaking into the
                # visible chat every time a tab reconnected and replayed
                # history via get_history, even after the live-path fix.
                # Checked as a substring, not exact equality, same reasoning
                # as the client-side fix: the model doesn't always reply
                # with ONLY the sentinel. Filtered here (server side, the
                # single source both read_recent_history and
                # read_archived_entries draw from) rather than only in
                # chat.js's get_history handler, so a no-update turn never
                # even reaches the client as part of the user-visible
                # transcript.
                if obj.get("type") == "assistant" and "[[NO_UPDATE]]" in text:
                    continue
                ts_raw = obj.get("timestamp")
                ts_ms: float | None = None
                if ts_raw:
                    try:
                        from datetime import datetime
                        ts_ms = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp() * 1000
                    except Exception:
                        ts_ms = None
                entry: dict[str, Any] = {
                    "role": obj["type"], "text": text,
                    "ts": ts_ms if ts_ms is not None else _time.time() * 1000,
                }
                if attachments:
                    entry["attachments"] = attachments
                entries.append(entry)
        except Exception as exc:
            log_event("engine", "read_history_skipped_malformed_line", source=source_label, error=str(exc))
    return entries


# --- tail reading ------------------------------------------------------------
#
# Bug fix (2026-09-20), confirmed live: every "what's recent?" reader in this
# codebase (the 24h dialogue file, narration, language detection, get_history,
# the companion-app sync) used to `read_text()` the ENTIRE session transcript
# and parse every line just to use its last few entries -- fine while a
# session was small, a real outage once one grew to hundreds of MB (a
# 368 MB / 147 MB pair of tabs, read into memory as ~2 GB of str + a second
# copy from split(), simultaneously for every tab at startup, on the asyncio
# event loop itself: the backend stopped answering /api/status for minutes,
# its watchdog killed and restarted it, and it did the same thing again).
# A transcript is append-only and chronological -- everything asked of it here
# is "the newest N" or "everything since T" -- so the cost of answering must
# depend on how much is being ASKED for, never on how large the file has
# grown. iter_lines_reversed/iter_entries_reversed are the one shared way to
# do that; nothing should call read_text() on a session transcript again.

_REVERSE_READ_INITIAL_CHUNK_BYTES = 1 * 1024 * 1024
# Raw-line timestamp lookup: Claude Code writes the top-level "timestamp"
# field at the END of each JSONL record (after the message content, however
# large), so only the tail of a line needs scanning for it -- never the whole
# (possibly multi-MB) line. A nested "timestamp" inside message content is
# JSON-escaped (\"timestamp\") and can't match this pattern.
_RAW_TS_RE = re.compile(rb'"timestamp"\s*:\s*"([^"]+)"')
_RAW_TS_TAIL_BYTES = 4096
# A single old-looking line isn't proof everything before it is old too (a
# resumed/forked transcript can carry a stray out-of-order record) -- only
# stop scanning after this many CONSECUTIVE lines older than the cutoff.
_STALE_LINES_BEFORE_STOP = 8


def iter_lines_reversed(
    path: str | Path, initial_chunk_bytes: int = _REVERSE_READ_INITIAL_CHUNK_BYTES, cold: bool = True
) -> Iterator[bytes]:
    """Yields the transcript's lines newest first; once the live file is
    exhausted, continues into its cold prefix (transcript_rotate) if one
    exists, so a rotated transcript still reads as one continuous history.
    cold=False reads only the live file. See _iter_file_lines_reversed."""
    yield from _iter_file_lines_reversed(path, initial_chunk_bytes)
    if cold:
        from app.transcript_rotate import cold_path_for
        cold_file = cold_path_for(path)
        if cold_file.exists():
            # The cold file ends with a newline, so its reverse walk opens with
            # an empty piece that is NOT a record -- drop it so the joined
            # stream is exactly the original file's line sequence.
            first = True
            for piece in _iter_file_lines_reversed(cold_file, initial_chunk_bytes):
                if first and not piece:
                    first = False
                    continue
                first = False
                yield piece


def _iter_file_lines_reversed(path: str | Path, initial_chunk_bytes: int) -> Iterator[bytes]:
    """Yields the file's lines (as raw bytes, no trailing newline), NEWEST
    first, reading backwards in chunks -- never more of the file in memory
    than the chunk plus whatever single line straddles a chunk boundary. A
    line larger than one chunk (a multi-MB tool result) just doubles the
    chunk size until it fits. Splits on b"\\n" at the byte level, which is
    safe for UTF-8 (0x0A never appears inside a multi-byte sequence) and for
    JSONL (a raw newline can only ever separate records -- inside a JSON
    string it's escaped)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        chunk_bytes = max(1, initial_chunk_bytes)
        buf = b""
        while pos > 0:
            n = min(chunk_bytes, pos)
            pos -= n
            f.seek(pos)
            buf = f.read(n) + buf
            pieces = buf.split(b"\n")
            if len(pieces) == 1:
                # No newline anywhere in what's been read so far -- this one
                # line is bigger than the chunk. Read more per step.
                chunk_bytes *= 2
                continue
            buf = pieces[0]  # possibly-incomplete first line; completed by the next chunk back
            for piece in reversed(pieces[1:]):
                yield piece
        yield buf


def _raw_line_ts_ms(raw: bytes) -> float | None:
    m = _RAW_TS_RE.search(raw[-_RAW_TS_TAIL_BYTES:])
    if not m:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(m.group(1).decode("ascii", errors="ignore").replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return None


def iter_entries_reversed(path: str | Path, source_label: str, min_ts_ms: float | None = None) -> Iterator[dict[str, Any]]:
    """The entries _extract_entries_from_lines would produce for this file,
    NEWEST first, produced lazily -- the caller stops iterating the moment it
    has enough. With min_ts_ms, also stops on its own once it's read past
    everything that new (see _STALE_LINES_BEFORE_STOP); entries it still
    yields just before stopping can be slightly older than the cutoff, so a
    caller needing an exact cutoff must still filter by entry["ts"], same as
    it always did."""
    stale_run = 0
    for raw in iter_lines_reversed(path):
        if not raw.strip():
            continue
        if min_ts_ms is not None:
            ts = _raw_line_ts_ms(raw)
            if ts is not None:
                if ts < min_ts_ms:
                    stale_run += 1
                    if stale_run >= _STALE_LINES_BEFORE_STOP:
                        return
                else:
                    stale_run = 0
        line_entries = _extract_entries_from_lines([raw.decode("utf-8", errors="replace")], source_label)
        yield from reversed(line_entries)


def read_recent_entries(path: str | Path, limit: int, source_label: str | None = None) -> list[dict[str, Any]]:
    """The newest `limit` entries of a transcript, oldest-first -- what
    `_extract_entries_from_jsonl(whole_file)[-limit:]` used to compute by
    parsing all of it."""
    out: list[dict[str, Any]] = []
    for entry in iter_entries_reversed(path, source_label or str(path)):
        out.append(entry)
        if len(out) >= limit:
            break
    out.reverse()
    return out


_CONTEXT_TOKEN_USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_COMPACT_BOUNDARY_RE = re.compile(rb'"subtype"\s*:\s*"compact_boundary"')


def read_last_context_tokens(path: str | Path, max_lines: int = 5000) -> int | None:
    """How big the model's context was at the END of this transcript, in
    tokens -- what the last assistant message's usage reports (input + cache
    read + cache creation, the same sum chat_session.py already uses for
    live turns). Returns 0 when a compaction is the newest thing in the
    file (context is just the summary right now, whatever the older usage
    records say), and None when nothing usable is found within max_lines.
    Read from the END of the file (iter_lines_reversed) -- cheap however big
    the transcript is. Exists so a fresh process can tell whether a session
    actually NEEDS compacting without a live turn having reported usage yet
    (see ChatSession._check_forced_compaction)."""
    scanned = 0
    for raw in iter_lines_reversed(path):
        if not raw.strip():
            continue
        scanned += 1
        if scanned > max_lines:
            return None
        if _COMPACT_BOUNDARY_RE.search(raw[:4000]):
            return 0
        if b'"usage"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if obj.get("type") != "assistant":
            continue
        usage = (obj.get("message") or {}).get("usage")
        if isinstance(usage, dict):
            total = sum(v for k, v in usage.items() if k in _CONTEXT_TOKEN_USAGE_KEYS and isinstance(v, int))
            if total:
                return total
    return None


def read_recent_history(workspace_dir: str, limit: int = 200) -> list[dict[str, Any]]:
    file = _latest_session_file(workspace_dir)
    if not file:
        return []
    return read_recent_entries(file, limit)


def read_recent_history_for_session(workspace_dir: str, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Bug fix (2026-09-10): read_recent_history() above reads whichever
    session .jsonl was modified most recently across the WHOLE workspace,
    with no regard for which tab that belongs to -- fine for the single-tab
    get_history recovery path, but wrong for the Android companion app's
    per-tab history sync (companion_api.py's _sync_history), which was
    confirmed live to mislabel one tab's conversation as another's this
    way. This reads the EXACT session file for the tab whose session_id
    the caller already resolved (durability.py's load_tab_session_id) --
    no "most recent" guessing."""
    file = session_transcript_path(workspace_dir, session_id)
    if not file.exists():
        return []
    return read_recent_entries(file, limit)


def read_archived_entries(file_path: str) -> list[dict[str, Any]]:
    """EVERY entry of an archive file -- only for a caller that genuinely
    needs the whole thing (expand_dehydrated_ref showing one archive back to
    a human). Lazily iterates the file rather than read_text()-ing it, so
    memory is proportional to the entries produced, not to raw file size x2.
    A caller that only wants recent entries must use iter_entries_reversed/
    read_recent_entries instead."""
    with open(file_path, encoding="utf-8", errors="replace") as f:
        return _extract_entries_from_lines(f, file_path)
