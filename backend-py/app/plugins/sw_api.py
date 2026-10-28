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
from app.http_tls import ssl_context
from app import external_session

API_URL = "https://www.squirrelwisdom.com/"

# Real values come from sw_service_keys_local.py (gitignored, maintainer-only
# -- see its template sw_service_keys_example.py). Previously hardcoded here
# in plaintext; rotated after that leaked into this repo's public history
# (2026-10). V2_LOGIN_SERVICE_KEY is scoped to exactly user:verify +
# wallet:getBalance; CAROLINE_SW_KEY is Caroline's general-purpose service
# key (ai:resolve/ai:tts/ai:stt/ai:describeImage/memory:*/contact:*/reply:*);
# V2_SESSION_LOOKUP_KEY is scoped to exactly user:getSessionUser, used solely
# by get_session_user below, main.py's remote-auth middleware's one dependency.
from app.sw_service_keys_local import CAROLINE_SW_KEY, V2_LOGIN_SERVICE_KEY, V2_SESSION_LOOKUP_KEY  # noqa: E402

CREDENTIALS_PATH = Path.home() / ".mcp-notes" / "credentials.json"


class SessionExpiredError(Exception):
    pass


class SwApiError(Exception):
    pass


# 2026-10-28, confirmed live: the SW server's nginx answered 502 (and a proxy 501) for about a minute, and
# every caller gave up at once -- all four model tiers failed in two seconds and the user got a bare
# "can't answer". A gateway that is down is the textbook transient failure: retried here, in the one place
# every SW call goes through (see also microagent_client._post_json_sync and memory_plugin._post, which
# share these numbers). 500 is left out (the app itself failed, retrying repeats it) and so is 504 (the
# request may have run, and a long ai call would be waited for again). A caller that asked for a single
# attempt (`retries=1`, the narrator's short-timeout calls) gets no status retries either.
RETRY_HTTP_STATUSES = frozenset({429, 501, 502, 503})
RETRY_PAUSES_S = (1.0, 2.0, 4.0, 8.0)


def status_retry_pauses(retries: int) -> tuple[float, ...]:
    return RETRY_PAUSES_S if retries > 1 else ()


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


def mark_funds_exhausted(reason: str) -> None:
    """Same account-wide global as _check_funds_exhaustion below -- exposed
    (2026-10-03) for small_model_engine.py's OWN direct OpenRouter calls (via
    camerlengo_ai's adapter, not this module's _post_json) to report into, since
    it's the exact same OpenRouter balance either way -- see this module's own
    comment above for why this is tracked account-wide, not per-caller."""
    global _funds_exhausted_reason
    _funds_exhausted_reason = reason
    _ensure_funds_probe_running()


def clear_funds_exhausted() -> None:
    global _funds_exhausted_reason
    _funds_exhausted_reason = None


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
        _ensure_funds_probe_running()


# Per explicit instruction (2026-10-15), a real incident: chat_session.py's
# submit() now refuses to start ANY new turn while _funds_exhausted_reason is
# set (see its own funds-gate comment) -- "absolutely all" of Caroline's
# activity pauses until the balance is back. But the flag is only ever
# CLEARED as a side effect of some real call happening to succeed (see
# _check_funds_exhaustion above) -- if nothing is allowed to run, nothing
# would ever notice recovery, and the app would stay blocked forever even
# after the provider's balance was topped up. This is the one thing still
# allowed to run while blocked: a minimal, cheap ai:resolve/SMALL probe,
# repeated on a flat interval, whose own success (through this module's
# shared _post_json/_check_funds_exhaustion chokepoint, same as every real
# call) is what actually clears the flag and lets chat_session.py's queued
# turns drain. Self-starting (from mark_funds_exhausted/_check_funds_
# exhaustion, not a loop main.py has to remember to launch) and self-
# stopping (its own while condition exits once the flag clears).
FUNDS_PROBE_INTERVAL_S = 30.0
_funds_probe_task: Any = None


def _ensure_funds_probe_running() -> None:
    global _funds_probe_task
    if _funds_probe_task is not None and not _funds_probe_task.done():
        return
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # called from sync/startup context with no loop yet -- nothing to probe for right now
    from app.task_supervisor import supervise
    from app.logging_setup import log_event
    log_event("plugin:sw_api", "funds_probe_starting", reason=_funds_exhausted_reason)
    _funds_probe_task = supervise("sw_funds_probe", _funds_probe_loop)


async def _funds_probe_loop() -> None:
    import asyncio
    from app.logging_setup import log_event
    while _funds_exhausted_reason is not None:
        await asyncio.sleep(FUNDS_PROBE_INTERVAL_S)
        if _funds_exhausted_reason is None:
            break  # cleared by some other real call in the meantime
        try:
            await _post_json({"command": "ai:resolve", "key": CAROLINE_SW_KEY, "question": "ok", "model": "SMALL"}, timeout=15.0)
        except Exception as exc:  # noqa: BLE001 -- still exhausted or a transient hiccup either way, keep probing
            log_event("plugin:sw_api", "funds_probe_attempt_failed", error=str(exc))
            continue
    log_event("plugin:sw_api", "funds_probe_stopped", recovered=_funds_exhausted_reason is None)


async def _post_json(body: dict[str, Any], timeout: float = 30.0, retries: int = 3) -> Any:
    """`timeout` is per-attempt (up to `retries` attempts on a transport-
    level failure, see below -- httpx.TimeoutException is itself a
    TransportError, so a short timeout here also bounds how long a single
    attempt can hang on a slow-but-not-actually-dead response). Defaults
    to 30.0/3 (unchanged behavior for every existing caller); progress
    narration (chat_session.py's _check_progress_narration, via voice_api.
    py's generate_progress_comment/translate_text) passes a much shorter
    `timeout` -- per explicit instruction (2026-09-22), narration is
    cosmetic filler under a 60s promise, not worth the same patience a
    real user-facing call deserves.

    `retries` (2026-10-15), per explicit instruction, a real incident: this
    function's OWN 3x internal retry-on-timeout was never accounted for by
    narration's "short per-attempt timeout" design above -- confirmed live,
    a stuck/overloaded endpoint made ONE call here (3 attempts x the given
    timeout each) take up to 3x as long as that timeout implied, which
    _check_progress_narration's OWN outer retry loop then multiplied
    AGAIN (its own several attempts, each secretly 3x longer than it
    thought) -- up to several hundred seconds of total silence for what
    was meant to be a handful of ~45s attempts. Narration passes retries=1
    here specifically so its short timeout means what it says: one real
    network attempt, no hidden multiplication -- the OUTER loop (chat_
    session.py) is where narration's own retrying already happens, on
    purpose, one layer up. Every other caller keeps the default 3."""
    import asyncio

    status_pauses = status_retry_pauses(retries)
    async with httpx.AsyncClient(verify=ssl_context(), timeout=timeout) as client:
        status_retry = 0
        while True:
            last_err: Exception | None = None
            for attempt in range(retries):
                try:
                    res = await client.post(API_URL, json=body)
                    break
                except httpx.TransportError as exc:
                    last_err = exc
                    if attempt < retries - 1:
                        await asyncio.sleep(0.5 * (attempt + 1))
            else:
                raise last_err  # type: ignore[misc]
            if res.status_code in RETRY_HTTP_STATUSES and status_retry < len(status_pauses):
                from app.logging_setup import log_event

                log_event("plugin:sw_api", "http_status_retry", command=body.get("command"), status=res.status_code, retry=status_retry + 1)
                await asyncio.sleep(status_pauses[status_retry])
                status_retry += 1
                continue
            break
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
            external_session.on_session_error(extra.get("session"))
            raise SessionExpiredError(reason)
        raise SwApiError(f'command "{command}" failed: {reason}')
    return envelope


async def get_session_user(session: str) -> str | None:
    """Resolves an arbitrary SW v2 session token to the email it belongs to
    (API/Api2UserCommands.py's `user:getSessionUser`, same auth scope as
    `user:verify`) -- the one way to validate a session presented by someone
    else, since there is no local/offline way to verify a v2 token (see
    main.py's remote-auth middleware, the only caller of this). Returns None
    for an invalid/expired/unknown session rather than raising, so a caller
    can treat "not a real session" and "session valid but somehow userless"
    the same way: reject."""
    data = await _post_json({"command": "user:getSessionUser", "key": V2_SESSION_LOOKUP_KEY, "session": session})
    if data.get(".status") != "ok":
        return None
    return data.get("user") or None


async def mint_v2_session(email: str, password: str) -> str:
    # Signed in with a session handed over by the host (external_session.py): a credential-less call gets that session.
    if not password and external_session.matches(email):
        return external_session.session() or ""
    data = await _post_json({"command": "user:verify", "key": V2_LOGIN_SERVICE_KEY, "path": "/users", "user": email, "password": password})
    if data.get(".status") != "ok" or not data.get("session"):
        raise SwApiError(f'SquirrelWisdom v2 login failed for "{email}": {data.get(".reason", json.dumps(data))}')
    return data["session"]


def load_credentials() -> dict[str, str] | None:
    if external_session.active():
        return {"email": external_session.user() or "", "password": ""}
    try:
        return json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def save_credentials(email: str, password: str) -> None:
    external_session.leave("signed in with a password")
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
