"""Splits a dehydrated session transcript into addressable chunk files, with
an index the model can read first -- the "hard fix" half of the 2026-10-16
context-overflow pass (see app/context_cap.py's own docstring for the
preventive half).

Why this exists: chat_session.py's _reset_unrecoverable_session() already
archives the WHOLE pre-reset transcript as one flat .txt file and tells the
fresh session, in a system note, "Read that file to find it" (see that
method's own archive_note). Confirmed live: that file IS the same
multi-hundred-thousand-token block that just overflowed the session in the
first place -- a model that actually follows the note and Reads the whole
thing straight back in would overflow again immediately. The note existed,
but following it safely was never actually possible.

This module is purely additive -- it runs AFTER the existing shutil.copyfile
(never instead of it: that single flat file is also what main.py's
expand_dehydrated_ref/history.read_archived_entries serve back to a HUMAN
viewing an archived conversation in the UI, and that path must keep working
exactly as it does today). The chunked copy is a second, model-facing-only
artifact in a sibling directory.

Chunking never parses transcript lines as JSON or re-serializes them --
chunks are built by grouping whole, unmodified lines up to CHUNK_MAX_CHARS,
so every chunk file is itself a byte-for-byte valid JSONL transcript (a
human could later point expand_dehydrated_ref at one, same as the full
file). CHUNK_MAX_CHARS reuses context_cap.BATCH_MAX_CHARS -- the same
"how much text is reasonable to hand the model in one call" budget already
vetted for the preventive half of this pass, not a freshly-picked number.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.context_cap import BATCH_MAX_CHARS
from app.history import _raw_line_ts_ms

CHUNK_MAX_CHARS = BATCH_MAX_CHARS

INDEX_FILENAME = "index.txt"


def split_into_chunks(transcript_path: Path, chunk_dir: Path, max_chars: int = CHUNK_MAX_CHARS) -> list[dict[str, Any]]:
    """Groups transcript_path's lines into chunk-0001.txt, chunk-0002.txt,
    ... under chunk_dir, each up to max_chars (a line that alone exceeds
    max_chars still gets its own whole chunk rather than being split --
    preserving line integrity matters more than the exact cap). Returns one
    descriptor per chunk: {"file", "chars", "lines", "first_ts_iso",
    "last_ts_iso"} (timestamps are None where the chunk's lines carry none --
    best-effort, never required). Returns [] if the transcript is empty or
    already fits in one chunk under max_chars (the model should just read
    the single archived file directly in that case -- no index needed for
    something that small)."""
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[dict[str, Any]] = []
    buf_lines: list[bytes] = []
    buf_chars = 0

    def _flush() -> None:
        nonlocal buf_lines, buf_chars
        if not buf_lines:
            return
        index = len(chunks) + 1
        chunk_path = chunk_dir / f"chunk-{index:04d}.txt"
        chunk_path.write_bytes(b"".join(buf_lines))
        timestamps = [t for t in (_raw_line_ts_ms(ln) for ln in buf_lines) if t is not None]
        chunks.append({
            "file": chunk_path.name,
            "chars": buf_chars,
            "lines": len(buf_lines),
            "first_ts_ms": min(timestamps) if timestamps else None,
            "last_ts_ms": max(timestamps) if timestamps else None,
        })
        buf_lines = []
        buf_chars = 0

    with open(transcript_path, "rb") as f:
        for raw_line in f:
            line_chars = len(raw_line)
            if buf_lines and buf_chars + line_chars > max_chars:
                _flush()
            buf_lines.append(raw_line)
            buf_chars += line_chars
    _flush()

    if len(chunks) <= 1:
        # Small enough that the existing single-file archive is already
        # safe to Read directly -- no index needed, and no point leaving a
        # redundant one-chunk copy on disk.
        for chunk in chunks:
            (chunk_dir / chunk["file"]).unlink(missing_ok=True)
        return []
    return chunks


def _fmt_ts(ts_ms: float | None) -> str:
    if ts_ms is None:
        return "unknown time"
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def write_index(chunk_dir: Path, chunks: list[dict[str, Any]]) -> Path:
    """index.txt: one line per chunk (filename, size, time range) plus the
    instruction every chunk-reading model turn needs -- read this index
    first (it's always small), then Read ONLY the specific chunk(s) that
    look relevant, never every chunk and never the original un-chunked
    archive file."""
    lines = [
        f"This earlier context was split into {len(chunks)} chunk(s), newest last, each a valid JSONL "
        "transcript on its own. Read ONE chunk at a time (Read tool, path below) -- never all of them, "
        "and never the original un-chunked archive file in the parent directory (it's the same "
        "oversized block that caused this reset in the first place and will overflow you again).",
        "",
    ]
    for chunk in chunks:
        lines.append(
            f"{chunk['file']}  ({chunk['chars']} chars, {chunk['lines']} messages)  "
            f"{_fmt_ts(chunk['first_ts_ms'])} .. {_fmt_ts(chunk['last_ts_ms'])}"
        )
    index_path = chunk_dir / INDEX_FILENAME
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path


def chunk_archive(transcript_path: Path, chunk_dir: Path) -> Path | None:
    """The one entry point _reset_unrecoverable_session calls: splits +
    writes the index, returns the index path, or None if the transcript was
    small enough that chunking wasn't worth it (see split_into_chunks)."""
    chunks = split_into_chunks(transcript_path, chunk_dir)
    if not chunks:
        return None
    return write_index(chunk_dir, chunks)
