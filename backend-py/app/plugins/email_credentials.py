"""A mailbox's credentials, kept by the email tools themselves.

Per explicit instruction (2026-10-22): "Креды ОБЯЗАТЕЛЬНО должны храниться в
памяти И В ЗАМЕТКАХ" -- on top of a local copy. Seen on the sw test box the
same day: a mailbox was connected and read in one turn, the model saved only
"connected, 16 messages" to memory, and the next turn ("check that mailbox")
had no password to work with. Until then (2026-09-10) the plugin stored
nothing on purpose and left saving to the model through notes_create; a
model that skipped it lost the mailbox.

Now, whatever the model does:
- a call that names only the address gets the rest filled in -- from the
  local copy first, then from the Notes vault note;
- a call that worked with credentials not yet kept saves them in all three
  places: the local encrypted copy (secret_store), the Notes vault note
  ("vault:email:<address>" in "Caroline:Vault", the format
  _credentials_convention_instruction has always described), and memory.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from app.logging_setup import log_event
from app.secret_store import decrypt_text_from_file, encrypt_text_to_file
from app.workspace_dir import WORKSPACE_DIR

VAULT_FOLDER = "Caroline:Vault"
FIELDS = ("password", "imapHost", "imapPort", "smtpHost", "smtpPort")
# What a call cannot run without; the ports have defaults on the server side.
REQUIRED = ("password", "imapHost")

_background: set[asyncio.Task[Any]] = set()   # strong references: a bare task may be collected mid-way


def vault_title(address: str) -> str:
    return f"vault:email:{address.strip().lower()}"


def _local_path(address: str) -> Path:
    safe = re.sub(r"[^a-z0-9._@+-]", "_", address.strip().lower())
    return Path(WORKSPACE_DIR) / "email-accounts" / f"{safe}.bin"


def _creds_of(args: dict[str, Any]) -> dict[str, Any]:
    return {k: args[k] for k in FIELDS if args.get(k) not in (None, "")}


def load_local(address: str) -> dict[str, Any] | None:
    try:
        text = decrypt_text_from_file(_local_path(address))
        return json.loads(text) if text else None
    except Exception as exc:  # noqa: BLE001 -- a damaged copy falls back to the vault note
        log_event("plugin:email", "credentials_local_read_failed", address=address, error=str(exc)[:200])
        return None


def _save_local(address: str, creds: dict[str, Any]) -> None:
    path = _local_path(address)
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypt_text_to_file(path, json.dumps(creds, ensure_ascii=False), f"Caroline mailbox {address}")


def _note_creds(text: str) -> dict[str, Any] | None:
    """The JSON object after the title line of a vault note."""
    body = text.split("\n", 1)[1] if "\n" in text else ""
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(body[start:end + 1])
    except ValueError:
        return None
    return {k: data[k] for k in FIELDS if data.get(k) not in (None, "")} if isinstance(data, dict) else None


async def _find_vault_note(address: str) -> tuple[str | None, dict[str, Any] | None]:
    """(note id, its credentials) of this mailbox's vault note, in any folder."""
    from app.plugins import notes_plugin

    title = vault_title(address)
    found = json.loads((await notes_plugin.notes_search({"query": title}, None))["text"])
    for entry in found if isinstance(found, list) else []:
        if str(entry.get("title") or "").strip().lower() != title:
            continue
        note = json.loads((await notes_plugin.notes_get({"id": entry["id"]}, None))["text"])
        return entry["id"], _note_creds(note.get("text") or "")
    return None, None


async def fill(args: dict[str, Any]) -> dict[str, Any]:
    """`args` with the mailbox's kept credentials filled in where the call left
    them out. Only for a call that names an address; one that gives everything
    itself is returned as it is."""
    address = (args.get("address") or "").strip()
    if not address or all(args.get(k) not in (None, "") for k in REQUIRED):
        return args
    kept = load_local(address)
    source = "local"
    if not kept or any(kept.get(k) in (None, "") for k in REQUIRED):
        try:
            _note_id, kept = await _find_vault_note(address)
            source = "notes"
        except Exception as exc:  # noqa: BLE001 -- Notes unavailable: say what is missing below
            log_event("plugin:email", "credentials_vault_read_failed", address=address, error=str(exc)[:200])
            kept = None
    if not kept or any(kept.get(k) in (None, "") for k in REQUIRED):
        raise ValueError(
            f"No saved credentials for {address} (none kept locally, no \"{vault_title(address)}\" note in Notes). "
            "Ask the user for this mailbox's password and IMAP/SMTP servers."
        )
    log_event("plugin:email", "credentials_filled", address=address, source=source)
    return {**{k: v for k, v in kept.items() if k in FIELDS}, **{k: v for k, v in args.items() if v not in (None, "")}}


def remember_after_success(args: dict[str, Any]) -> None:
    """Called once a call with these credentials worked. Credentials not kept
    yet (a new mailbox, or a changed password) are saved locally right away and
    to Notes and memory in the background."""
    address = (args.get("address") or "").strip()
    creds = _creds_of(args)
    if not address or any(creds.get(k) in (None, "") for k in REQUIRED):
        return
    if load_local(address) == creds:
        return
    try:
        _save_local(address, creds)
        log_event("plugin:email", "credentials_saved_local", address=address)
    except Exception as exc:  # noqa: BLE001 -- the other two copies are still made
        log_event("plugin:email", "credentials_save_local_failed", address=address, error=str(exc)[:200])
    task = asyncio.ensure_future(_save_to_notes_and_memory(address, creds))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _save_to_notes_and_memory(address: str, creds: dict[str, Any]) -> None:
    from app.plugins import memory_plugin, notes_plugin

    text = vault_title(address) + "\n" + json.dumps(creds, ensure_ascii=False)
    try:
        note_id, _old = await _find_vault_note(address)
        if note_id:
            await notes_plugin.notes_update({"id": note_id, "text": text, "folder": VAULT_FOLDER}, None)
        else:
            await notes_plugin.notes_create({"text": text, "folder": VAULT_FOLDER}, None)
        log_event("plugin:email", "credentials_saved_notes", address=address, updated=bool(note_id))
    except Exception as exc:  # noqa: BLE001 -- logged; memory is still written
        log_event("plugin:email", "credentials_save_notes_failed", address=address, error=str(exc)[:200])
    try:
        details = ", ".join(f"{k}: {v}" for k, v in creds.items())
        reply = await memory_plugin.save_info({"text": f"Mailbox {address} -- working login credentials: {details}."}, None)
        log_event("plugin:email", "credentials_saved_memory", address=address, error=bool(reply.get("is_error")))
    except Exception as exc:  # noqa: BLE001 -- logged
        log_event("plugin:email", "credentials_save_memory_failed", address=address, error=str(exc)[:200])
