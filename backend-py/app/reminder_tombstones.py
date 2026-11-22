"""Ids of reminders the user cancelled (workspace/schedule-cancelled.json).

Why: vault_backup.restore_missing merges every reminder of the cloud copy that is not in schedule.json back in
by id. A cancelled reminder is exactly that, so a restart (or a stale cloud copy) brought it back and its cron
fired again -- the cancelled LinkedIn/Wellfound vacancy check (2026-11-07). cancel_reminder records the id here;
the restore skips these ids."""

from __future__ import annotations

import json
from pathlib import Path

FILE = "schedule-cancelled.json"


def _path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / FILE


def cancelled_ids(workspace_dir: str) -> set[str]:
    try:
        data = json.loads(_path(workspace_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {i for i in data if isinstance(i, str)} if isinstance(data, list) else set()


def mark_cancelled(workspace_dir: str, reminder_id: str) -> None:
    ids = cancelled_ids(workspace_dir)
    if reminder_id in ids:
        return
    ids.add(reminder_id)
    path = _path(workspace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(ids), indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
