"""Rotation of huge Claude Code session transcripts.

A transcript (~/.claude/projects/<slug>/<sessionId>.jsonl) is append-only
and /compact never truncates it, so a long-lived tab's file grows without
bound (hundreds of MB) while the CLI only ever needs the part from the LAST
compact_boundary onward (the boundary record + its summary + what follows).
Everything before that point is dead weight the CLI still has to read on
every resume.

rotate_transcript() moves the bytes before the last compact_boundary into a
cold file OUTSIDE the CLI's projects dir (workspace/cold/<sessionId>.jsonl,
appended in order across rotations) and atomically rewrites the live file to
start at the boundary. Nothing is lost: history.iter_lines_reversed
continues into the cold file after the live one is exhausted, so every
reader that walks the transcript backwards still sees the full history.

Must only run while no CLI process has the session open (the session's
build step, before query()). Crash-safe via a tiny journal: if the process
dies between the cold append and the live replace, the next call truncates
the cold file back to its pre-append size before doing anything else.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.logging_setup import log_event

ROTATE_MIN_FILE_BYTES = 64 * 1024 * 1024
ROTATE_MIN_PREFIX_BYTES = 32 * 1024 * 1024
_COPY_CHUNK = 8 * 1024 * 1024
_BOUNDARY_RE = re.compile(rb'"subtype"\s*:\s*"compact_boundary"')


def cold_dir(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "cold"


def cold_path_for(live_path: str | Path, workspace_dir: str | None = None) -> Path:
    """Where a live transcript's cold prefix lives."""
    if workspace_dir is None:
        from app.workspace_dir import WORKSPACE_DIR as workspace_dir
    return cold_dir(workspace_dir) / Path(live_path).name


@dataclass
class RotateReport:
    rotated: bool
    reason: str = ""
    moved_bytes: int = 0
    kept_bytes: int = 0


def _journal_path(cold: Path) -> Path:
    return cold.with_name(cold.name + ".journal")


def _recover_interrupted(live: Path, cold: Path) -> None:
    journal = _journal_path(cold)
    if not journal.exists():
        return
    try:
        info = json.loads(journal.read_text(encoding="utf-8"))
        if live.stat().st_size == info["live_size"] and cold.exists():
            # The live file was never replaced: the cold append is a stale duplicate.
            with open(cold, "r+b") as f:
                f.truncate(info["cold_size_before"])
            log_event("engine", "transcript_rotate_recovered", path=str(live), cold_size=info["cold_size_before"])
    finally:
        journal.unlink(missing_ok=True)


def _find_last_boundary_offset(path: Path, size: int) -> int | None:
    """Byte offset of the start of the newest compact_boundary line, found by
    reading backwards; None if the file has none."""
    from app.history import iter_lines_reversed

    consumed = 0  # sum(len+1) over ALL pieces of a file == size + 1
    for raw in iter_lines_reversed(path, cold=False):
        consumed += len(raw) + 1
        if not _BOUNDARY_RE.search(raw):
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
            offset = size + 1 - consumed
            return offset if offset >= 0 else None
    return None


def rotate_transcript(
    live_path: str | Path,
    workspace_dir: str,
    min_file_bytes: int = ROTATE_MIN_FILE_BYTES,
    min_prefix_bytes: int = ROTATE_MIN_PREFIX_BYTES,
) -> RotateReport:
    live = Path(live_path)
    cold = cold_path_for(live, workspace_dir)
    try:
        cold.parent.mkdir(parents=True, exist_ok=True)
        _recover_interrupted(live, cold)
        size = live.stat().st_size
        if size < min_file_bytes:
            return RotateReport(False, "small")
        cut = _find_last_boundary_offset(live, size)
        if cut is None:
            return RotateReport(False, "no_boundary")
        if cut < min_prefix_bytes:
            return RotateReport(False, "prefix_small")
        with open(live, "rb") as f:
            if cut > 0:
                f.seek(cut - 1)
                if f.read(1) != b"\n":
                    return RotateReport(False, "cut_not_on_line_start")
            f.seek(cut)
            if not _BOUNDARY_RE.search(f.readline()):
                return RotateReport(False, "cut_verify_failed")

        cold_before = cold.stat().st_size if cold.exists() else 0
        journal = _journal_path(cold)
        journal.write_text(json.dumps({"live_size": size, "cold_size_before": cold_before}), encoding="utf-8")
        tmp = live.with_name(live.name + ".rotating")
        try:
            with open(live, "rb") as src, open(cold, "ab") as dst:
                remaining = cut
                while remaining > 0:
                    data = src.read(min(_COPY_CHUNK, remaining))
                    if not data:
                        raise OSError("live file shorter than expected during rotation")
                    dst.write(data)
                    remaining -= len(data)
                dst.flush()
                os.fsync(dst.fileno())
            with open(live, "rb") as src, open(tmp, "wb") as out:
                src.seek(cut)
                while True:
                    data = src.read(_COPY_CHUNK)
                    if not data:
                        break
                    out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, live)
        except BaseException:
            tmp.unlink(missing_ok=True)
            try:
                with open(cold, "r+b") as f:
                    f.truncate(cold_before)
            except OSError:
                pass
            journal.unlink(missing_ok=True)
            raise
        journal.unlink(missing_ok=True)
        log_event("engine", "transcript_rotated", path=str(live), moved_bytes=cut, kept_bytes=size - cut)
        return RotateReport(True, "ok", cut, size - cut)
    except Exception as exc:
        log_event("engine", "transcript_rotate_failed", path=str(live), error=repr(exc))
        return RotateReport(False, f"error:{exc!r}")
