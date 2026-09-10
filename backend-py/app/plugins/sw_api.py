"""Shared Camerlengo/SquirrelWisdom v2 API client -- ports the identical
callV2/session pattern repeated across mcp-servers-src/sms/src/api.ts and
mcp-servers-src/notes/src/api.ts (both plain HTTPS JSON clients, no
Node-specific logic at all, per the migration plan's own file inventory).

Credentials are read/written to the EXACT SAME file the current Node-based
tools already use (~/.mcp-notes/credentials.json) -- one shared
SquirrelWisdom login across every SW-backed tool on this machine, Node or
Python, so switching backends doesn't force a re-login.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

API_URL = "https://www.squirrelwisdom.com/"
V2_LOGIN_SERVICE_KEY = "fytZDwOTaBo8I173IS2DaY_qgzm0IFvqvnxJGvC5QrE"

# Caroline's own general-purpose SquirrelWisdom service key -- ai:resolve/
# ai:tts/ai:stt/ai:describeImage/... (matches backend/src/voice.ts's own
# hardcoded API_KEY). Shared here rather than duplicated per plugin.
CAROLINE_SW_KEY = "QvR-sujLOgpKWZ-yhSOK5ZNgEe4sgF0EUU7GexQqr4M"

CREDENTIALS_PATH = Path.home() / ".mcp-notes" / "credentials.json"


class SessionExpiredError(Exception):
    pass


class SwApiError(Exception):
    pass


async def _post_json(body: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=30.0) as client:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                res = await client.post(API_URL, json=body)
                break
            except httpx.TransportError as exc:
                last_err = exc
                if attempt < 2:
                    import asyncio
                    await asyncio.sleep(0.5 * (attempt + 1))
        else:
            raise last_err  # type: ignore[misc]
        if res.status_code >= 400:
            raise SwApiError(f'SquirrelWisdom API HTTP {res.status_code} for command "{body.get("command")}"')
        return res.json()


async def call_v2(command: str, **extra: Any) -> dict[str, Any]:
    envelope = await _post_json({"command": command, **extra})
    if envelope.get(".status") != "ok":
        reason = envelope.get(".reason", json.dumps(envelope))
        if isinstance(reason, str) and "session" in reason.lower():
            raise SessionExpiredError(reason)
        raise SwApiError(f'command "{command}" failed: {reason}')
    return envelope


async def mint_v2_session(email: str, password: str) -> str:
    data = await _post_json({"command": "user:verify", "key": V2_LOGIN_SERVICE_KEY, "path": "/users", "user": email, "password": password})
    if data.get(".status") != "ok" or not data.get("session"):
        raise SwApiError(f'SquirrelWisdom v2 login failed for "{email}": {data.get(".reason", json.dumps(data))}')
    return data["session"]


def load_credentials() -> dict[str, str] | None:
    try:
        return json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def save_credentials(email: str, password: str) -> None:
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_text(json.dumps({"email": email, "password": password}, indent=2), encoding="utf-8")


class SessionManager:
    """Per-tool-family session cache (sms/notes each get their own instance)
    -- never persists the SESSION TOKEN to disk (it dies server-side after
    an idle timeout and this process may live longer or restart), only the
    credentials; re-logs in lazily on first use each process lifetime, same
    as the current TS session.ts modules."""

    def __init__(self) -> None:
        self._session: str | None = None

    async def _login(self, email: str, password: str) -> str:
        self._session = await mint_v2_session(email, password)
        return self._session

    async def login(self, email: str, password: str) -> str:
        session = await self._login(email, password)
        save_credentials(email, password)
        return session

    async def ensure_session(self) -> str:
        if self._session:
            return self._session
        creds = load_credentials()
        if not creds:
            raise SwApiError("Not logged in yet. Log in with your SquirrelWisdom email and password first.")
        return await self._login(creds["email"], creds["password"])

    async def with_session(self, fn: Any) -> Any:
        session = await self.ensure_session()
        try:
            return await fn(session)
        except SessionExpiredError:
            self._session = None
            fresh = await self.ensure_session()
            return await fn(fresh)
