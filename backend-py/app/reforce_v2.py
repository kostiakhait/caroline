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

REFORCE_URL = "https://beautysqrl.com"
REFORCE_KEY = "EaYW2x8-oi7qjz4cl9cZWj7Udg6U8RcotHLs0B9xZUM"


class ReforceError(Exception):
    pass


class ReforceSessionExpired(ReforceError):
    pass


async def call(command: str, body: dict[str, Any] | None = None, *, timeout: float = 180.0) -> dict[str, Any]:
    payload: dict[str, Any] = {"command": command, "key": REFORCE_KEY, **(body or {})}
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
