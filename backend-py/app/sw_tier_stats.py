"""How each tier of the sw cascade does on each kind of task -- the data behind
the sw.tier.advice microagent (2026-10-22, explicit instruction; see
docs/SW_TOOL_ADVISOR_AND_TIER_STATS_PLAN.md).

Per free tag (app/microagents/sw_task_tags.py) and tier: how many tasks ran on
that tier, how many it closed itself (success) and how many had to go up from
it (fail). Kept locally -- the totals as of the last sync plus the delta since
-- and merged into one shared table note in the account's Notes every hour, on
the same schedule as the memory-to-Notes backup: read the note, add the local
delta, write it back, take its totals, zero the delta. Every machine of the
account adds its own delta, so they all converge on the same table.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.logging_setup import log_event
from app.task_supervisor import supervise

STATS_FILE = "sw-tier-stats.json"
NOTES_FOLDER = "Caroline:Stats"
NOTE_TITLE = "sw-tier-stats"
_COUNTERS = ("total", "success", "fail")

_file_lock = threading.Lock()
_sync_lock = asyncio.Lock()


def _path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / STATS_FILE


def _load(workspace_dir: str) -> dict[str, Any]:
    try:
        data = json.loads(_path(workspace_dir).read_text(encoding="utf-8"))
        return {"totals": data.get("totals") or {}, "delta": data.get("delta") or {}}
    except (OSError, ValueError):
        return {"totals": {}, "delta": {}}


def _save(workspace_dir: str, data: dict[str, Any]) -> None:
    path = _path(workspace_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _add(into: dict[str, Any], table: dict[str, Any]) -> None:
    """into[tag][tier][counter] += table[tag][tier][counter]"""
    for tag, tiers in (table or {}).items():
        for tier, counts in (tiers or {}).items():
            cell = into.setdefault(tag, {}).setdefault(str(tier), {c: 0 for c in _COUNTERS})
            for c in _COUNTERS:
                cell[c] = int(cell.get(c, 0)) + int((counts or {}).get(c, 0))


def record(workspace_dir: str, tags: list[str], tiers_ran: list[int], closed_on: int | None) -> None:
    """One finished cascade turn: every tier it ran on counts once per tag; the tier
    it was closed on is a success, every other one a fail (it had to go up)."""
    if not tags or not tiers_ran:
        return
    delta: dict[str, Any] = {}
    for tag in tags:
        for tier in dict.fromkeys(tiers_ran):
            cell = delta.setdefault(tag, {}).setdefault(str(tier), {c: 0 for c in _COUNTERS})
            cell["total"] += 1
            cell["success" if tier == closed_on else "fail"] += 1
    with _file_lock:
        data = _load(workspace_dir)
        _add(data["delta"], delta)
        _save(workspace_dir, data)
    log_event("engine", "sw_tier_stats_recorded", tags=tags, tiers=list(dict.fromkeys(tiers_ran)), closed_on=closed_on)


def rows(workspace_dir: str) -> list[dict[str, Any]]:
    """The table as sw.tier.advice reads it: synced totals plus the local delta."""
    with _file_lock:
        data = _load(workspace_dir)
    merged: dict[str, Any] = {}
    _add(merged, data["totals"])
    _add(merged, data["delta"])
    return [
        {"tag": tag, "tier": int(tier), **counts}
        for tag, tiers in sorted(merged.items()) for tier, counts in sorted(tiers.items(), key=lambda kv: int(kv[0]))
    ]


async def _find_note() -> tuple[str | None, dict[str, Any]]:
    from app.plugins import notes_plugin

    found = json.loads((await notes_plugin.notes_search({"query": NOTE_TITLE, "folder": NOTES_FOLDER}, None))["text"])
    for entry in found if isinstance(found, list) else []:
        if str(entry.get("title") or "").strip() == NOTE_TITLE:
            note = json.loads((await notes_plugin.notes_get({"id": entry["id"]}, None))["text"])
            body = (note.get("text") or "").split("\n", 1)
            try:
                return entry["id"], json.loads(body[1]) if len(body) > 1 and body[1].strip() else {}
            except ValueError:
                log_event("engine", "sw_tier_stats_note_unreadable", note_id=entry["id"])
                return entry["id"], {}
    return None, {}


async def sync(workspace_dir: str) -> None:
    """Merge the local delta into the shared Notes table (sum), then take its totals."""
    from app.plugins import notes_plugin

    async with _sync_lock:
        with _file_lock:
            delta = _load(workspace_dir)["delta"]
        try:
            note_id, table = await _find_note()
            merged: dict[str, Any] = {}
            _add(merged, table)
            _add(merged, delta)
            if delta:
                text = NOTE_TITLE + "\n" + json.dumps(merged, ensure_ascii=False)
                if note_id:
                    await notes_plugin.notes_update({"id": note_id, "text": text, "folder": NOTES_FOLDER}, None)
                else:
                    await notes_plugin.notes_create({"text": text, "folder": NOTES_FOLDER}, None)
        except Exception as exc:  # noqa: BLE001 -- the delta stays for the next sync
            log_event("engine", "sw_tier_stats_sync_failed", error=str(exc)[:200], error_type=type(exc).__name__)
            return
        with _file_lock:
            data = _load(workspace_dir)
            # Whatever was recorded while the note was being written is kept as the new delta.
            remaining: dict[str, Any] = {}
            _add(remaining, data["delta"])
            for tag, tiers in delta.items():
                for tier, counts in tiers.items():
                    for c in _COUNTERS:
                        remaining[tag][tier][c] -= int(counts.get(c, 0))
            remaining = {t: {k: v for k, v in tiers.items() if any(v.values())} for t, tiers in remaining.items()}
            _save(workspace_dir, {"totals": merged, "delta": {t: v for t, v in remaining.items() if v}})
        log_event("engine", "sw_tier_stats_synced", tags=len(merged), pushed=bool(delta))


def start_sync_loop(workspace_dir: str) -> "asyncio.Task[None]":
    """Once at startup, then at the top of every hour -- the memory-to-Notes backup's
    own schedule (vault-backup-hourly, cron 0 0 * * * *)."""

    async def _loop() -> None:
        await sync(workspace_dir)
        while True:
            now = datetime.now()
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            await asyncio.sleep((next_hour - now).total_seconds())
            await sync(workspace_dir)

    log_event("engine", "sw_tier_stats_sync_loop_starting")
    return supervise("sw_tier_stats_sync", _loop)
