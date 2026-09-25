"""companion_sms_store -- Caroline's own local, persistent copy of the
paired phone's SMS. Exists because a phone is NOT reliably reachable the
way an IMAP server is (asleep, no signal, the companion feature briefly
disabled) -- per explicit instruction (2026-09-25), Caroline must never
make a live round-trip to the phone just to answer "what did I get?".
Instead, a background loop (companion_api.py's sms sync loop) periodically
pulls whatever's new into this file, and companion_plugin.py's
companion_list_sms_threads/companion_read_sms_thread read ONLY from here
-- instant, and correct as of the last successful sync regardless of
whether the phone happens to be reachable RIGHT NOW.

File: <workspace>/sms-store.json. Plain JSON, not a database -- matches
this codebase's own convention for small/medium local state (companion-
history-cursor.json, companion-operations.json, schedule.json, ...); a
real phone's SMS history, even over years, is small compared to what
those files already tolerate.

Dedup key: (threadId, date, type, body) -- content://sms has no id this
backend can otherwise correlate across syncs without extra device-side
plumbing; a genuine collision (two distinct messages sharing all four)
is not realistically distinguishable anyway.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.logging_setup import log_event


def _store_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "sms-store.json"


def _empty_store() -> dict[str, Any]:
    return {"lastSyncedAt": None, "lastMessageDateMs": 0, "messages": []}


def load_store(workspace_dir: str) -> dict[str, Any]:
    path = _store_path(workspace_dir)
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "messages" not in data:
            return _empty_store()
        return data
    except Exception as exc:
        log_event("plugin:companion", "sms_store_load_failed", error=str(exc))
        return _empty_store()


def save_store(workspace_dir: str, store: dict[str, Any]) -> None:
    try:
        _store_path(workspace_dir).write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log_event("plugin:companion", "sms_store_save_failed", error=str(exc))


def since_cursor(store: dict[str, Any]) -> int | None:
    """The phone-side query cursor for the NEXT sync -- None means "never
    synced yet, send a bootstrap batch" (see the phone's own dump handler
    for the bootstrap cap); otherwise the newest message date this store
    has already seen, so the phone only needs to send what's actually new."""
    value = store.get("lastMessageDateMs")
    return int(value) if value else None


def merge_messages(store: dict[str, Any], incoming: list[dict[str, Any]]) -> int:
    """Merges `incoming` (the phone dump response's own message list) into
    `store` in place, deduped. Returns how many were genuinely new (for
    logging) -- store itself is what callers persist via save_store."""
    seen = {(m.get("threadId"), m.get("date"), m.get("type"), m.get("body")) for m in store["messages"]}
    added = 0
    max_date = store.get("lastMessageDateMs") or 0
    for msg in incoming:
        key = (msg.get("threadId"), msg.get("date"), msg.get("type"), msg.get("body"))
        if key in seen:
            continue
        seen.add(key)
        store["messages"].append(msg)
        added += 1
        date = msg.get("date")
        if isinstance(date, (int, float)) and date > max_date:
            max_date = date
    if added:
        store["messages"].sort(key=lambda m: m.get("date") or 0)
        store["lastMessageDateMs"] = max_date
    store["lastSyncedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return added


def list_threads(store: dict[str, Any], unread_only: bool = False) -> list[dict[str, Any]]:
    by_thread: dict[Any, dict[str, Any]] = {}
    for msg in store["messages"]:
        thread_id = msg.get("threadId")
        entry = by_thread.setdefault(
            thread_id, {"threadId": thread_id, "address": msg.get("address"), "snippet": None, "date": 0, "unreadCount": 0},
        )
        if not msg.get("read", True):
            entry["unreadCount"] += 1
        date = msg.get("date") or 0
        if date >= entry["date"]:
            entry["date"] = date
            entry["snippet"] = (msg.get("body") or "")[:200]
            entry["address"] = msg.get("address") or entry["address"]
    threads = sorted(by_thread.values(), key=lambda t: t["date"], reverse=True)
    if unread_only:
        threads = [t for t in threads if t["unreadCount"] > 0]
    return threads


def list_messages(store: dict[str, Any], thread_id: str) -> list[dict[str, Any]]:
    return [
        {"from": m.get("address"), "body": m.get("body"), "date": m.get("date"), "type": m.get("type")}
        for m in store["messages"]
        if str(m.get("threadId")) == str(thread_id)
    ]


def last_synced_at(store: dict[str, Any]) -> str | None:
    return store.get("lastSyncedAt")
