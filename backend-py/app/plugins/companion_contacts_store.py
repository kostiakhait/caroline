"""companion_contacts_store -- Caroline's own local, persistent copy of
every paired phone's contacts (name + numbers). Companion to
companion_sms_store.py -- same reasoning (a phone is not reliably
reachable the way an IMAP server is), same per-device partitioning, but a
simpler full-replace shape instead of an append-only merge: a phone's
contact list is a complete snapshot each time it's asked for ("list"),
not a growing history like SMS, so each sync just replaces a device's
bucket wholesale rather than deduping into it.

Per a real incident (2026-10-07): companion_search_contacts only ever
looked up the phone's address book live, on the model's own initiative.
A first fix tried auto-saving a found contact into working_memory's
"contacts" category from inside that tool call -- correctly rejected as
"another crutch" (it still only fires if/when the model happens to call
that specific tool, same failure shape as relying on the model to call
working_memory_remember itself, which, confirmed live, had never once
actually happened in this workspace's whole history). Contacts need to
sync automatically the same way SMS already does (companion_api.py's
start_sms_sync_loop), independent of any tool call or model behavior.
This module is the storage half of that; companion_api.py's
start_contacts_sync_loop keeps it fresh, and companion_plugin.py's
companion_search_contacts_local reads it.

File: <workspace>/contacts-store.json.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.logging_setup import log_event


def _store_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "contacts-store.json"


def _empty_store() -> dict[str, Any]:
    return {"devices": {}}


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
        log_event("plugin:companion", "contacts_store_load_failed", error=str(exc))
        return _empty_store()


def save_store(workspace_dir: str, store: dict[str, Any]) -> None:
    try:
        _store_path(workspace_dir).write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log_event("plugin:companion", "contacts_store_save_failed", error=str(exc))


def replace_device_contacts(store: dict[str, Any], device_id: str, phone_number: str | None, contacts: list[dict[str, Any]]) -> None:
    """Full replace, not a merge -- see this module's own doc comment for why."""
    store["devices"][device_id] = {
        "phoneNumber": phone_number,
        "lastSyncedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "contacts": [c for c in contacts if isinstance(c, dict) and c.get("name")],
    }


def search_contacts(store: dict[str, Any], query: str, device_id: str | None = None) -> list[dict[str, Any]]:
    """Case-insensitive substring match against name OR any number. Across
    every paired device unless device_id narrows it, each hit tagged with
    its own phoneNumber (mirrors companion_sms_store's thread-key tagging)."""
    q = query.strip().lower()
    results: list[dict[str, Any]] = []
    devices = {device_id: store["devices"][device_id]} if device_id and device_id in store["devices"] else store["devices"]
    for bucket in devices.values():
        for contact in bucket.get("contacts", []):
            name = str(contact.get("name") or "")
            numbers = contact.get("numbers") or []
            if q in name.lower() or any(q in str(n).lower() for n in numbers):
                results.append({"name": name, "numbers": numbers, "phoneNumber": bucket.get("phoneNumber")})
    return results


def list_all_contacts(store: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for bucket in store["devices"].values():
        for contact in bucket.get("contacts", []):
            results.append({"name": contact.get("name"), "numbers": contact.get("numbers"), "phoneNumber": bucket.get("phoneNumber")})
    return results


def last_synced_at(store: dict[str, Any], device_id: str | None = None) -> str | None:
    if device_id is not None:
        bucket = store["devices"].get(device_id)
        return bucket.get("lastSyncedAt") if bucket else None
    timestamps = [b.get("lastSyncedAt") for b in store["devices"].values() if b.get("lastSyncedAt")]
    return min(timestamps) if timestamps else None
