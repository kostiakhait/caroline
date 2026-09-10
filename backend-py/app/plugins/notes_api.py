"""Ports mcp-servers-src/notes/src/{api,session}.ts -- a DIFFERENT, OLDER
Camerlengo protocol than sw_api.py's v2 (dot-prefixed ".status"/".reason"
envelope fields, a "plugins:call"/plugin="Notes" wrapper around every real
action, its own APP_KEY, a "verifyPassword" login command instead of
"user:verify") -- NOT interchangeable with sw_api.py despite both talking
to the same squirrelwisdom.com backend. Shares the exact same credentials
file (~/.mcp-notes/credentials.json) with sms/sw_api.py -- same account, so
logging in via either tool family covers both (reuses sw_api.py's
load_credentials/save_credentials directly rather than duplicating them).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import string
from typing import Any

import httpx

from app.plugins.sw_api import load_credentials, save_credentials

BASE_URL = "https://squirrelwisdom.com"
APP_KEY = "01Az8nB8mB4cCV"
ID_ALPHABET = string.ascii_letters + string.digits

# nginx caps the request body at 64MB; base64 inflates raw bytes by ~33%, so
# the real ceiling for a single attachment's raw file size is ~47MB.
MAX_ATTACHMENT_BYTES = 47 * 1024 * 1024


class SessionExpiredError(Exception):
    pass


class NotesApiError(Exception):
    pass


async def _post_json(body: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=30.0) as client:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                res = await client.post(BASE_URL + "/", json=body)
                break
            except httpx.TransportError as exc:
                last_err = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        else:
            raise last_err  # type: ignore[misc]
        if res.status_code >= 400:
            raise NotesApiError(f'Squirrel Wisdom API HTTP {res.status_code} for command "{body.get(".command")}"')
        return res.json()


async def verify_password(email: str, password: str) -> str:
    result = await _post_json({".command": "verifyPassword", "key": APP_KEY, "path": "/users", "user": email, "password": password})
    session = result.get("session") if isinstance(result, dict) else None
    if not session:
        raise NotesApiError(f'Login failed for "{email}": {json.dumps(result)}')
    return session


async def call_plugin(action: str, session: str, **extra: Any) -> Any:
    """Every Notes action goes through the generic plugins:call envelope,
    authorized by the per-request session token. `query` duplicates
    `action`: it's a mandatory field on the outer envelope, unrelated to
    which Notes action is being invoked -- ported as-is from api.ts."""
    body = {".command": "plugins:call", "plugin": "Notes", "query": action, "action": action, "key": APP_KEY, "session": session, **extra}
    envelope = await _post_json(body)
    if not isinstance(envelope, dict) or envelope.get(".status") != "ok":
        reason = envelope.get(".reason", json.dumps(envelope)) if isinstance(envelope, dict) else json.dumps(envelope)
        if isinstance(reason, str) and "session" in reason.lower():
            raise SessionExpiredError(reason)
        raise NotesApiError(f'Notes plugin action "{action}" failed: {reason}')
    return envelope.get("result")


def hash16(login: str) -> str:
    return hashlib.sha256(login.encode("utf-8")).hexdigest()[:16]


def _random_alphabet_string(length: int) -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(length))


def gen_note_id() -> str:
    return _random_alphabet_string(12)


def gen_attachment_filename() -> str:
    return _random_alphabet_string(16)


class SessionManager:
    """Mirrors session.ts's module-level `active` + login/ensureSession/
    withSession -- one instance shared by every notes_plugin.py handler.
    Never persists the session TOKEN to disk (dies after 24h idle
    server-side, this process may live longer or restart) -- only the
    credentials; re-logs in lazily on first use, same as sw_api.py's own
    SessionManager but against this older protocol, and also caching
    hash16 (notes_whoami reports it)."""

    def __init__(self) -> None:
        self._email: str | None = None
        self._hash16: str | None = None
        self._session: str | None = None

    async def _do_login(self, email: str, password: str) -> tuple[str, str, str]:
        session = await verify_password(email, password)
        self._email, self._hash16, self._session = email, hash16(email), session
        return self._email, self._hash16, self._session

    async def login(self, email: str, password: str) -> tuple[str, str, str]:
        result = await self._do_login(email, password)
        save_credentials(email, password)
        return result

    async def ensure_session(self) -> tuple[str, str, str]:
        if self._session and self._email and self._hash16:
            return self._email, self._hash16, self._session
        creds = load_credentials()
        if not creds:
            raise NotesApiError("Not logged in yet. Call ensure_squirrelwisdom_login first (notes_login is disabled -- credentials must go through the native login window, never a tool argument).")
        return await self._do_login(creds["email"], creds["password"])

    async def with_session(self, fn: Any) -> Any:
        _email, _h16, session = await self.ensure_session()
        try:
            return await fn(session)
        except SessionExpiredError:
            self._session = None
            _email, _h16, fresh = await self.ensure_session()
            return await fn(fresh)
