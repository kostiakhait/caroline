"""Ports backend/src/ratatoskOwnAccount.ts -- Caroline's OWN SquirrelWisdom/
Ratatosk identity, deliberately separate from notes_api.py's shared
credentials file (the USER's own account, shared with Notes etc.). This is
a per-install account: every Caroline instance that opts into the Ratatosk
integration gets its own unique mailbox/account, not a hardcoded shared
identity.
"""

from __future__ import annotations

import base64
import json
import secrets
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.plugins.sw_api import API_URL, mint_v2_session

# Scoped key for the email:create v2 command (see reforce's
# API/Api2EmailCommands.py, cmdV2CreateMailbox) -- a SEPARATE key from
# sw_api's login key: least-privilege, a leaked email-provisioning key
# shouldn't also be able to verify arbitrary account passwords, and vice
# versa. Minted on the production server directly, scoped to
# "email:create" only, non-expiring.
EMAIL_CREATE_KEY = "SVdTcM0PwAm1qmZD-ucFUqHpcEiClVGTuJXdHbOEajg"


def _credentials_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "ratatosk-own-account.json"


def _load_own_credentials(workspace_dir: str) -> dict[str, str] | None:
    try:
        return json.loads(_credentials_path(workspace_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_own_credentials(workspace_dir: str, email: str, password: str) -> None:
    path = _credentials_path(workspace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"email": email, "password": password}, indent=2), encoding="utf-8")


def has_own_ratatosk_account(workspace_dir: str) -> bool:
    return _load_own_credentials(workspace_dir) is not None


def own_ratatosk_email(workspace_dir: str) -> str | None:
    creds = _load_own_credentials(workspace_dir)
    return creds["email"] if creds else None


async def _create_mailbox(address: str, password: str) -> tuple[bool, str | None]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(API_URL, json={
            "command": "email:create", "key": EMAIL_CREATE_KEY, "address": address, "password": password, ".msgid": uuid.uuid4().hex,
        })
        data = res.json()
    if data.get(".status") != "ok":
        return False, str(data.get(".reason") or "Mailbox creation failed")
    return True, None


async def _register_account_only(email: str, password: str) -> tuple[bool, str | None]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(API_URL, json={"command": "user:add", "path": "/users", "user": email, "password": password})
        data = res.json()
    if data.get(".status") != "ok" or not data.get("session"):
        return False, str(data.get(".reason") or "Registration failed")
    return True, None


def _random_local_part() -> str:
    return f"caroline-{secrets.token_hex(4)}"


def _random_password() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(24)).decode("ascii").rstrip("=")


async def ensure_own_ratatosk_account(workspace_dir: str) -> dict[str, Any]:
    """Idempotent: if Caroline already has her own account, returns it
    immediately -- registration (mailbox creation + Ratatosk/SquirrelWisdom
    account signup) only ever runs once, the first time this is called
    with no stored credentials yet."""
    existing = _load_own_credentials(workspace_dir)
    if existing:
        return {"ok": True, "email": existing["email"]}

    email = f"{_random_local_part()}@navlink.net"
    password = _random_password()

    mailbox_ok, mailbox_err = await _create_mailbox(email, password)
    if not mailbox_ok:
        return {"ok": False, "error": f"Could not create mailbox: {mailbox_err}"}

    reg_ok, reg_err = await _register_account_only(email, password)
    if not reg_ok:
        return {"ok": False, "error": f"Mailbox created, but SquirrelWisdom registration failed: {reg_err}"}

    _save_own_credentials(workspace_dir, email, password)
    return {"ok": True, "email": email}


async def get_own_v2_session(workspace_dir: str) -> str:
    """Same shape as sw_api.py's session minting, but for Caroline's OWN
    account -- mints fresh every call, no caching, matching the original."""
    creds = _load_own_credentials(workspace_dir)
    if not creds:
        raise RuntimeError("Caroline has no Ratatosk account yet -- call ensure_ratatosk_own_account first.")
    return await mint_v2_session(creds["email"], creds["password"])
