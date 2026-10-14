"""memory_save_queue -- durable, restart-safe queue for save_info.

Per explicit instruction (2026-10-13), a real incident: save_info's actual
server-side completion takes 90-270+ seconds (confirmed live, across 118
saves) and used to be tracked only by a bare in-process asyncio.Task -- a
backend crash or restart mid-save silently dropped the whole follow-up (the
local topic-cache update, the failure notification), even though the
server's own save job may have still been running or already finished.

This is the record of what the user believes is already being saved -- the
opposite of memory_topics.py's own cache (which is freely re-derivable and
self-heals on the next refresh). A crash mid-write truncating this file is
exactly the durability bug being fixed here, so writes are atomic
(temp-file + fsync + os.replace, not a plain write_text) rather than reusing
memory_topics.py's simpler "it's just a cache" save pattern.

Storage: <workspace_dir>/memory_save_queue.json, shape:
{"queue": [{"id", "params", "job_id", "enqueued_at", "attempts",
"notified_failure_at"}, ...]}

`params` is exactly the dict memory_plugin.py's save_info builds (text,
traceId, language, materials?) -- already JSON-serializable. `job_id` is
filled in by memory_plugin.py's _drain_one the moment memory:saveStart
returns one, BEFORE polling begins -- so a drain after a restart resumes
polling the SAME server job instead of starting a new one (the fix for the
"saved the same text four times" duplicate-save bug a stale restart used to
risk).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from app.logging_setup import log_event


def _path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "memory_save_queue.json"


def _load(workspace_dir: str) -> list[dict[str, Any]]:
    try:
        stored = json.loads(_path(workspace_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    queue = stored.get("queue") if isinstance(stored, dict) else None
    return queue if isinstance(queue, list) else []


def _save(workspace_dir: str, entries: list[dict[str, Any]]) -> None:
    """Atomic write: temp file + fsync + os.replace (atomic on both Windows'
    MoveFileExW and POSIX rename(2)). A single fixed temp name is safe here
    -- this backend is single-process/single-event-loop for this file, no
    concurrent writers to race."""
    path = _path(workspace_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"queue": entries}, indent=2, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as exc:
        log_event("memory_save_queue", "save_failed", error=str(exc))


def enqueue(workspace_dir: str, params: dict[str, Any]) -> str:
    """Durably records a save BEFORE save_info returns to the model -- see
    this module's own docstring for why. Returns the new entry's id."""
    entries = _load(workspace_dir)
    entry_id = uuid.uuid4().hex
    entries.append({
        "id": entry_id, "params": params, "job_id": None,
        "enqueued_at": time.time(), "attempts": 0, "notified_failure_at": None,
    })
    _save(workspace_dir, entries)
    return entry_id


def all_entries(workspace_dir: str) -> list[dict[str, Any]]:
    return _load(workspace_dir)


def get(workspace_dir: str, entry_id: str) -> dict[str, Any] | None:
    return next((e for e in _load(workspace_dir) if e.get("id") == entry_id), None)


def remove(workspace_dir: str, entry_id: str) -> None:
    """Only once a save is genuinely done -- success OR a definitive
    failure. An OUTCOME_UNKNOWN result must NOT call this -- it stays
    queued for the next drain tick to retry, same semantics the old
    single-attempt code already followed for that case."""
    entries = _load(workspace_dir)
    kept = [e for e in entries if e.get("id") != entry_id]
    if len(kept) != len(entries):
        _save(workspace_dir, kept)


def _update(workspace_dir: str, entry_id: str, **fields: Any) -> None:
    entries = _load(workspace_dir)
    changed = False
    for entry in entries:
        if entry.get("id") == entry_id:
            entry.update(fields)
            changed = True
            break
    if changed:
        _save(workspace_dir, entries)


def set_job_id(workspace_dir: str, entry_id: str, job_id: str) -> None:
    _update(workspace_dir, entry_id, job_id=job_id)


def clear_job_id(workspace_dir: str, entry_id: str) -> None:
    """The server confirmed it no longer knows this job (e.g. restarted
    itself) -- the next drain attempt must start a genuinely fresh save,
    not keep polling a job_id that will never resolve."""
    _update(workspace_dir, entry_id, job_id=None)


def mark_attempt(workspace_dir: str, entry_id: str) -> None:
    entries = _load(workspace_dir)
    for entry in entries:
        if entry.get("id") == entry_id:
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            _save(workspace_dir, entries)
            return


def mark_notified(workspace_dir: str, entry_id: str, when: float) -> None:
    _update(workspace_dir, entry_id, notified_failure_at=when)
