"""Removes redundant full-transcript copies from workspace/dehydrated/.

Background (2026-09-20, measured live): every compaction used to copy the
tab's ENTIRE session transcript into workspace/dehydrated/ (see
ChatSession._pre_compact_hook's docstring). Compaction never touches the
live transcript -- it is append-only -- so each copy was a byte-for-byte
prefix of a file that still exists, and thousands of them piled up: 4,484
files, 892 GB on the machine where this was found. The hook no longer makes
them; this module removes the ones already there, on every install that
ever ran the old hook -- not just the one where it was found.

The rule is deliberately conservative -- a file is deleted ONLY when it is
PROVEN redundant, never on a guess from its name, age or size:

  - it is a .txt of at least MIN_ARCHIVE_BYTES (anything smaller is a
    dehydration reference -- an image/thinking stub a transcript still
    points at -- never a full transcript copy), and
  - a live session transcript exists whose first HEAD_BYTES are identical,
    which is at least as long, and whose bytes match the archive's at the
    end and at three points in the middle (a prefix copy of an append-only
    file matches everywhere; anything rewritten or diverged does not).

An archive with no live source (its session was deleted, e.g. by clear_tab)
is the only copy of that conversation and is KEPT, whatever its size.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.logging_setup import log_event

MIN_ARCHIVE_BYTES = 1 * 1024 * 1024
HEAD_BYTES = 1 * 1024 * 1024
SAMPLE_BYTES = 256 * 1024
# A file this fresh may still be mid-copy -- leave it for the next pass.
YOUNG_FILE_SECONDS = 120


@dataclass
class PruneReport:
    scanned: int = 0
    deleted: int = 0
    deleted_bytes: int = 0
    kept_small: int = 0
    kept_young: int = 0
    kept_unverified: int = 0
    kept_unverified_bytes: int = 0
    failed: int = 0
    deleted_paths: list[str] = field(default_factory=list)


def _head_hash(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha1(f.read(HEAD_BYTES)).hexdigest()


def _same_bytes_at(a: Path, b: Path, offset: int, length: int) -> bool:
    with open(a, "rb") as fa, open(b, "rb") as fb:
        fa.seek(offset)
        fb.seek(offset)
        return fa.read(length) == fb.read(length)


def _is_prefix_copy_of(archive: Path, live: Path, archive_size: int) -> bool:
    """True if `archive` looks byte-for-byte like the first archive_size
    bytes of `live` -- checked at the head (already matched by the caller),
    the tail and three points between."""
    if live.stat().st_size < archive_size:
        return False
    n = min(SAMPLE_BYTES, archive_size)
    offsets = {max(0, archive_size - n)}
    for fraction in (0.25, 0.5, 0.75):
        offsets.add(max(0, min(int(archive_size * fraction), archive_size - n)))
    return all(_same_bytes_at(archive, live, off, n) for off in sorted(offsets))


def prune_redundant_compaction_archives(
    dehydrated_dir: Path,
    sessions_dir: Path,
    *,
    dry_run: bool = False,
    throttle_s: float = 0.0,
    should_stop: Callable[[], bool] | None = None,
) -> PruneReport:
    report = PruneReport()
    if not dehydrated_dir.is_dir():
        return report

    # head-hash -> live transcripts starting with those exact bytes
    sources: dict[str, list[Path]] = {}
    if sessions_dir.is_dir():
        for live in sessions_dir.glob("*.jsonl"):
            try:
                if live.stat().st_size > 0:
                    sources.setdefault(_head_hash(live), []).append(live)
            except OSError:
                continue

    now = time.time()
    for archive in sorted(dehydrated_dir.iterdir()):
        if should_stop is not None and should_stop():
            break
        try:
            if not archive.is_file() or archive.suffix != ".txt":
                continue
            st = archive.stat()
            report.scanned += 1
            if st.st_size < MIN_ARCHIVE_BYTES:
                report.kept_small += 1
                continue
            if now - st.st_mtime < YOUNG_FILE_SECONDS:
                report.kept_young += 1
                continue
            candidates = sources.get(_head_hash(archive), [])
            if not any(_is_prefix_copy_of(archive, live, st.st_size) for live in candidates):
                report.kept_unverified += 1
                report.kept_unverified_bytes += st.st_size
                continue
            if not dry_run:
                archive.unlink()
            report.deleted += 1
            report.deleted_bytes += st.st_size
            report.deleted_paths.append(str(archive))
            if throttle_s:
                time.sleep(throttle_s)
        except OSError as exc:
            report.failed += 1
            log_event("engine", "archive_prune_failed", path=str(archive), error=str(exc))
    return report


def prune_workspace_archives(workspace_dir: str, *, dry_run: bool = False, throttle_s: float = 0.0,
                             should_stop: Callable[[], bool] | None = None) -> PruneReport:
    """The real entry point: resolves this workspace's own archive and
    session-transcript directories, prunes, then clears any per-tab
    continuity pointer whose archive no longer exists."""
    from app.durability import claude_project_dir, clear_tab_continuity_archive, dehydrated_dir

    report = prune_redundant_compaction_archives(
        dehydrated_dir(workspace_dir), claude_project_dir(workspace_dir),
        dry_run=dry_run, throttle_s=throttle_s, should_stop=should_stop,
    )
    if not dry_run:
        import json

        for pointer in Path(workspace_dir).glob("tab-continuity-*.json"):
            try:
                archive_path = json.loads(pointer.read_text(encoding="utf-8")).get("archivePath")
            except Exception:
                continue
            if archive_path and not Path(archive_path).exists():
                clear_tab_continuity_archive(workspace_dir, pointer.stem[len("tab-continuity-"):])
    log_event(
        "engine", "archive_prune_done", dry_run=dry_run, scanned=report.scanned, deleted=report.deleted,
        deleted_gb=round(report.deleted_bytes / 1e9, 1), kept_unverified=report.kept_unverified,
        kept_unverified_gb=round(report.kept_unverified_bytes / 1e9, 1), failed=report.failed,
    )
    return report
