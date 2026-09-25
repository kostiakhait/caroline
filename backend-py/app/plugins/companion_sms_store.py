"""companion_sms_store -- Caroline's own local, persistent copy of every
paired phone's SMS. Exists because a phone is NOT reliably reachable the
way an IMAP server is (asleep, no signal, the companion feature briefly
disabled) -- per explicit instruction (2026-09-25), Caroline must never
make a live round-trip to a phone just to answer "what did I get?".
Instead, a background loop (companion_api.py's sms sync loop) periodically
pulls whatever's new into this file, and companion_plugin.py's
companion_list_sms_threads/companion_read_sms_thread read ONLY from here
-- instant, and correct as of the last successful sync regardless of
whether a phone happens to be reachable RIGHT NOW.

File: <workspace>/sms-store.json. Plain JSON, not a database -- matches
this codebase's own convention for small/medium local state (companion-
history-cursor.json, companion-operations.json, schedule.json, ...); a
real phone's SMS history, even over years, is small compared to what
those files already tolerate.

=== Per-device partitioning (explicit instruction, 2026-09-26) ============

Multiple phones can now be paired to the same account (see companion_api.
py's device registry). Messages are partitioned per deviceId, NOT merged
into one flat list, for a correctness reason, not just tidiness:
content://sms's threadId is a small integer LOCAL to one phone's own
database -- phone A's thread "5" and phone B's thread "5" are unrelated
conversations that happen to share a number. Mixing them into one
namespace would silently conflate two different threads. Store shape:

    {"devices": {"<deviceId>": {"phoneNumber": "...", "lastSyncedAt": "...",
                                 "lastMessageDateMs": 0, "messages": [...]}}}

Every thread/message-facing read that crosses device boundaries (the
"list everything across every paired phone" case) tags each result with
its own deviceId/phoneNumber and returns a composite thread key
(`"<deviceId>:<rawThreadId>"`) instead of the phone-local raw id alone, so
callers (companion_plugin.py) never need to separately track "which
device is this threadId even from" -- the id itself carries it.

Dedup key (within one device's own bucket): (threadId, date, type, body)
-- content://sms has no id this backend can otherwise correlate across
syncs without extra device-side plumbing; a genuine collision (two
distinct messages on the SAME phone sharing all four) is not realistically
distinguishable anyway.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.logging_setup import log_event

THREAD_KEY_SEP = ":"


def _store_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "sms-store.json"


def _empty_store() -> dict[str, Any]:
    return {"devices": {}}


def _empty_device_bucket(phone_number: str | None) -> dict[str, Any]:
    return {"phoneNumber": phone_number, "lastSyncedAt": None, "lastMessageDateMs": 0, "messages": []}


def load_store(workspace_dir: str) -> dict[str, Any]:
    path = _store_path(workspace_dir)
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "devices" not in data or not isinstance(data["devices"], dict):
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


def _bucket(store: dict[str, Any], device_id: str, phone_number: str | None = None) -> dict[str, Any]:
    bucket = store["devices"].get(device_id)
    if bucket is None:
        bucket = _empty_device_bucket(phone_number)
        store["devices"][device_id] = bucket
    elif phone_number and bucket.get("phoneNumber") != phone_number:
        # The user can re-confirm/change a phone's own number in the setup
        # screen after it's already synced once -- keep the bucket's
        # messages, just refresh the label.
        bucket["phoneNumber"] = phone_number
    return bucket


def since_cursor(store: dict[str, Any], device_id: str) -> int | None:
    """The phone-side query cursor for the NEXT sync of ONE device -- None
    means "never synced this device yet, send a bootstrap batch"."""
    bucket = store["devices"].get(device_id)
    if not bucket:
        return None
    value = bucket.get("lastMessageDateMs")
    return int(value) if value else None


def merge_messages(store: dict[str, Any], device_id: str, phone_number: str | None, incoming: list[dict[str, Any]]) -> int:
    """Merges `incoming` (one device's dump-response message list) into
    that device's own bucket, deduped. Returns how many were genuinely new
    (for logging) -- store itself is what callers persist via save_store."""
    bucket = _bucket(store, device_id, phone_number)
    seen = {(m.get("threadId"), m.get("date"), m.get("type"), m.get("body")) for m in bucket["messages"]}
    added = 0
    max_date = bucket.get("lastMessageDateMs") or 0
    for msg in incoming:
        key = (msg.get("threadId"), msg.get("date"), msg.get("type"), msg.get("body"))
        if key in seen:
            continue
        seen.add(key)
        bucket["messages"].append(msg)
        added += 1
        date = msg.get("date")
        if isinstance(date, (int, float)) and date > max_date:
            max_date = date
    if added:
        bucket["messages"].sort(key=lambda m: m.get("date") or 0)
        bucket["lastMessageDateMs"] = max_date
    bucket["lastSyncedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return added


def _thread_key(device_id: str, raw_thread_id: Any) -> str:
    return f"{device_id}{THREAD_KEY_SEP}{raw_thread_id}"


def split_thread_key(thread_key: str) -> tuple[str, str] | None:
    """Reverses _thread_key. None if it isn't a well-formed composite key
    (e.g. a stale/hand-typed value) -- callers treat that as "thread not
    found", not a crash."""
    if THREAD_KEY_SEP not in thread_key:
        return None
    device_id, _, raw_id = thread_key.partition(THREAD_KEY_SEP)
    if not device_id or not raw_id:
        return None
    return device_id, raw_id


def _threads_for_device(bucket: dict[str, Any], device_id: str, unread_only: bool) -> list[dict[str, Any]]:
    by_thread: dict[Any, dict[str, Any]] = {}
    for msg in bucket["messages"]:
        raw_thread_id = msg.get("threadId")
        entry = by_thread.setdefault(
            raw_thread_id,
            {
                "threadId": _thread_key(device_id, raw_thread_id),
                "phoneNumber": bucket.get("phoneNumber"),
                "address": msg.get("address"),
                "snippet": None,
                "date": 0,
                "unreadCount": 0,
            },
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


def list_threads(store: dict[str, Any], device_id: str, unread_only: bool = False) -> list[dict[str, Any]]:
    """Threads for ONE device only -- each entry's threadId is already the
    composite key (device-scoped), ready to hand straight to
    list_messages/companion_read_sms_thread."""
    bucket = store["devices"].get(device_id)
    if not bucket:
        return []
    return _threads_for_device(bucket, device_id, unread_only)


def list_all_threads(store: dict[str, Any], unread_only: bool = False) -> list[dict[str, Any]]:
    """Threads across EVERY paired device, merged into one list (each
    entry still carries its own phoneNumber and a device-scoped
    threadId) -- the default when a caller doesn't name one phone."""
    threads: list[dict[str, Any]] = []
    for device_id in store["devices"]:
        threads.extend(_threads_for_device(store["devices"][device_id], device_id, unread_only))
    threads.sort(key=lambda t: t["date"], reverse=True)
    return threads


def list_messages(store: dict[str, Any], thread_key: str) -> list[dict[str, Any]] | None:
    """None if thread_key isn't a well-formed composite key or names a
    device this store has no bucket for -- distinct from "empty thread"
    (an empty list), so callers can report "not found" accurately."""
    split = split_thread_key(thread_key)
    if split is None:
        return None
    device_id, raw_thread_id = split
    bucket = store["devices"].get(device_id)
    if not bucket:
        return None
    return [
        {"from": m.get("address"), "body": m.get("body"), "date": m.get("date"), "type": m.get("type")}
        for m in bucket["messages"]
        if str(m.get("threadId")) == str(raw_thread_id)
    ]


def last_synced_at(store: dict[str, Any], device_id: str | None = None) -> str | None:
    """With a device_id, that device's own last sync time. Without one,
    the OLDEST of every paired device's last sync (the honest answer to
    "is everything I'm about to show you fresh" when aggregating -- one
    stale device shouldn't be hidden behind another device's fresh
    timestamp)."""
    if device_id is not None:
        bucket = store["devices"].get(device_id)
        return bucket.get("lastSyncedAt") if bucket else None
    timestamps = [b.get("lastSyncedAt") for b in store["devices"].values() if b.get("lastSyncedAt")]
    return min(timestamps) if timestamps else None
