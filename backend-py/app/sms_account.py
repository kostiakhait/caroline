"""Ports backend/src/smsAccount.ts -- Settings' "SMS Account" section,
backing the sms:setAccount/getAccount/removeAccount v2 commands
(API/Api2SMSCommands.py). Per explicit instruction, every user sends/
receives SMS through their own SMTP2GO account (own API key, own number),
not a shared one. Unlike get_sw_status's wallet:getBalance call, these are
auth="user_role" commands -- session only, no scoped key needed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.login_api import get_v2_session
from app.logging_setup import log_event
from app.plugins.sw_api import API_URL


async def _call_v2(command: str, **extra: Any) -> dict[str, Any]:
    body = {"command": command, **extra}
    async with httpx.AsyncClient(timeout=30.0) as client:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                res = await client.post(API_URL, json=body)
                break
            except httpx.TransportError as exc:
                last_err = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        else:
            raise last_err  # type: ignore[misc]
        return res.json()


async def get_sms_account_status() -> dict[str, Any]:
    try:
        session = await get_v2_session()
        data = await _call_v2("sms:getAccount", session=session)
        if data.get(".status") != "ok":
            log_event("engine", "sms_account_status_failed", reason=data.get(".reason"))
            return {"hasAccount": False, "sender": None, "error": str(data.get(".reason") or "Status check failed")}
        return {"hasAccount": bool(data.get("has_account")), "sender": data.get("sender"), "error": None}
    except Exception as exc:
        log_event("engine", "sms_account_status_threw", error=str(exc))
        return {"hasAccount": False, "sender": None, "error": str(exc)}


async def set_sms_account(smtp2go_api_key: str, smtp2go_sender: str | None) -> dict[str, Any]:
    try:
        session = await get_v2_session()
        extra: dict[str, Any] = {"smtp2go_api_key": smtp2go_api_key}
        if smtp2go_sender:
            extra["smtp2go_sender"] = smtp2go_sender
        data = await _call_v2("sms:setAccount", session=session, **extra)
        if data.get(".status") != "ok":
            log_event("engine", "sms_account_set_failed", reason=data.get(".reason"))
            return {"ok": False, "error": str(data.get(".reason") or "Save failed")}
        log_event("engine", "sms_account_set_ok")
        return {"ok": True}
    except Exception as exc:
        log_event("engine", "sms_account_set_threw", error=str(exc))
        return {"ok": False, "error": str(exc)}


async def remove_sms_account() -> dict[str, Any]:
    try:
        session = await get_v2_session()
        data = await _call_v2("sms:removeAccount", session=session)
        if data.get(".status") != "ok":
            return {"ok": False, "error": str(data.get(".reason") or "Remove failed")}
        log_event("engine", "sms_account_remove_ok")
        return {"ok": True}
    except Exception as exc:
        log_event("engine", "sms_account_remove_threw", error=str(exc))
        return {"ok": False, "error": str(exc)}
