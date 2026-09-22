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


# Bug fix (2026-09-16), per explicit instruction: "при исчерпании баланса
# на клоде или опенроутере эта информация явно прокидывалась в кэролайн и
# высвечивалась на статус-баре" -- Camerlengo proxies several tiers
# (narration/translation/consult/SW-mode chat, ...) through OpenRouter;
# confirmed live it can return a real, structured "insufficient funds"
# envelope (`{'.status': 'error', '.errcode': '998', '.reason':
# 'OpenRouter returned 402 Insufficient funds'}`) with a normal HTTP 200 --
# _post_json is the ONE choke point every such call already goes through
# (voice_api.py imports this exact function directly, not a duplicate), so
# this is the one place that can reliably detect it regardless of which
# higher-level feature triggered the call. Tracked as simple module state
# (this balance is a single account-wide resource, not per-tab) --
# get_funds_exhausted_reason() lets chat_session.py surface it without
# this module needing to know anything about tabs/sessions/WS. Cleared by
# the next genuinely successful call (proves the balance recovered), not
# by any other kind of failure -- an unrelated transient error must never
# silently erase a real "still exhausted" signal.
_funds_exhausted_reason: str | None = None


def get_funds_exhausted_reason() -> str | None:
    return _funds_exhausted_reason


def _check_funds_exhaustion(envelope: Any) -> None:
    global _funds_exhausted_reason
    if not isinstance(envelope, dict):
        return
    if envelope.get(".status") == "ok":
        _funds_exhausted_reason = None
        return
    reason = envelope.get(".reason")
    if isinstance(reason, str) and ("insufficient funds" in reason.lower() or " 402" in reason):
        _funds_exhausted_reason = reason


async def _post_json(body: dict[str, Any], timeout: float = 30.0) -> Any:
    """`timeout` is per-attempt (up to 3 attempts on a transport-level
    failure, see below -- httpx.TimeoutException is itself a
    TransportError, so a short timeout here also bounds how long a single
    attempt can hang on a slow-but-not-actually-dead response). Defaults
    to 30.0 (unchanged behavior for every existing caller); progress
    narration (chat_session.py's _check_progress_narration, via voice_api.
    py's generate_progress_comment/translate_text) passes a much shorter
    value -- per explicit instruction (2026-09-22), narration is cosmetic
    filler under a 60s promise, not worth the same patience a real
    user-facing call deserves."""
    async with httpx.AsyncClient(timeout=timeout) as client:
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
        result = res.json()
        _check_funds_exhaustion(result)
        return result


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
