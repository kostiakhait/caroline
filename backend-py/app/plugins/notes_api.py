"""Notes actions for Caroline, over the Camerlengo v2 protocol: login is
user:verify, every Notes action is a plugin:call (plugin="Notes") carrying the
session token. Shares the credentials file (~/.mcp-notes/credentials.json)
with sms/sw_api.py -- same account, so logging in via either covers both.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import string
from typing import Any

from app.plugins.sw_api import load_credentials, save_credentials
from app.reforce_v2 import ReforceError as NotesApiError
from app.reforce_v2 import ReforceSessionExpired as SessionExpiredError
from app.reforce_v2 import call as reforce_call

ID_ALPHABET = string.ascii_letters + string.digits

# nginx caps the request body at 64MB; base64 inflates raw bytes by ~33%, so
# the real ceiling for a single attachment's raw file size is ~47MB.
MAX_ATTACHMENT_BYTES = 47 * 1024 * 1024


async def verify_password(email: str, password: str) -> str:
    result = await reforce_call("user:verify", {"path": "/users", "user": email, "password": password})
    session = result.get("session")
    if not session:
        raise NotesApiError(f'Login failed for "{email}": {json.dumps(result)}')
    return session


async def call_plugin(action: str, session: str, **extra: Any) -> Any:
    """Every Notes action goes through the v2 plugin:call command, authorized
    by the per-request session token. `query` duplicates `action`: it's a
    mandatory field on the envelope, unrelated to which Notes action is being
    invoked -- ported as-is from api.ts."""
    envelope = await reforce_call("plugin:call", {"plugin": "Notes", "query": action, "action": action, "session": session, **extra})
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
