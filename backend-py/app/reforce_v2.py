"""The single client for every Camerlengo v2 command Caroline makes.

Requests are JSON objects with "command" (no leading dot), "key", and the
command's own fields. Responses are flat objects carrying ".status" ("ok" or
"error"); on success the command's fields sit alongside it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from app.http_tls import ssl_context
from app.sw_service_keys_local import REFORCE_KEY

REFORCE_URL = "https://beautysqrl.com"


class ReforceError(Exception):
    pass


class ReforceSessionExpired(ReforceError):
    pass


async def call(command: str, body: dict[str, Any] | None = None, *, timeout: float = 180.0) -> dict[str, Any]:
    payload: dict[str, Any] = {"command": command, "key": REFORCE_KEY, **(body or {})}
    # Per explicit instruction (2026-10-12): this never threaded a session
    # through at all, which Camerlengo was treating as a free, unmetered
    # "internal" call for every ai:generateImage/wikipediaSearch/factExtract/
    # etc. here -- see login_api.get_cached_v2_session's own doc comment.
    if "session" not in payload:
        from app.login_api import get_cached_v2_session  # local: login_api -> notes_api -> reforce_v2 is a real import cycle
        session = await get_cached_v2_session()
        if session:
            payload["session"] = session
    async with httpx.AsyncClient(verify=ssl_context(), timeout=timeout) as client:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                res = await client.post(REFORCE_URL + "/", json=payload)
                break
            except httpx.TransportError as exc:
                last_err = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        else:
            raise last_err  # type: ignore[misc]
    if res.status_code >= 400:
        raise ReforceError(f"HTTP {res.status_code} for {command}")
    data = res.json()
    if not isinstance(data, dict) or data.get(".status") != "ok":
        reason = str(data.get(".reason", data)) if isinstance(data, dict) else str(data)
        if "session" in reason.lower():
            raise ReforceSessionExpired(reason)
        raise ReforceError(f"{command} failed: {reason}")
    return data
