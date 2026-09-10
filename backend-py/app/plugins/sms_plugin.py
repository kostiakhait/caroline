"""sms -- ports mcp-servers-src/sms/src/*.ts (pure HTTPS JSON client, no
Node-specific logic) to httpx, reusing sw_api.py's shared Camerlengo v2
client/session machinery."""

from __future__ import annotations

from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.plugins.sw_api import SessionManager, call_v2

SMS_SERVICE_KEY = "sms_zB7vIlt7R_ethw1JCp6IT0cXd3UZsaWf2UGiwoci6FY"

_sessions = SessionManager()


async def sms_login(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.login(args["email"], args["password"])
    return {"text": f"Logged in as {args['email']}."}


async def sms_set_account(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> None:
        extra: dict[str, Any] = {"smtp2go_api_key": args["smtp2go_api_key"]}
        if args.get("smtp2go_sender"):
            extra["smtp2go_sender"] = args["smtp2go_sender"]
        await call_v2("sms:setAccount", session=session, **extra)

    await _sessions.with_session(do)
    return {"text": "SMTP2GO account registered."}


async def sms_get_account_status(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> dict[str, Any]:
        result = await call_v2("sms:getAccount", session=session)
        return {"has_account": result.get("has_account"), "sender": result.get("sender")}

    status = await _sessions.with_session(do)
    return {"text": str(status)}


async def sms_remove_account(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda session: call_v2("sms:removeAccount", session=session))
    return {"text": "SMTP2GO account removed."}


async def sms_send(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> dict[str, Any]:
        extra: dict[str, Any] = {"key": SMS_SERVICE_KEY, "session": session, "destination": args["destination"], "content": args["content"]}
        if args.get("sender"):
            extra["sender"] = args["sender"]
        result = await call_v2("sms:send", **extra)
        return {"total_sent": result.get("total_sent"), "statuses": result.get("statuses"), "messages": result.get("messages")}

    result = await _sessions.with_session(do)
    return {"text": str(result)}


async def sms_view_received(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> list[Any]:
        extra: dict[str, Any] = {"key": SMS_SERVICE_KEY, "session": session}
        if args.get("start_date"):
            extra["start_date"] = args["start_date"]
        if args.get("end_date"):
            extra["end_date"] = args["end_date"]
        result = await call_v2("sms:viewReceived", **extra)
        return result.get("messages", [])

    messages = await _sessions.with_session(do)
    return {"text": str(messages)}


PLUGIN = Plugin(
    name="sms",
    tools=[
        PluginTool(
            "sms_login",
            "One-time login with the user's SquirrelWisdom email/password. Verifies the credentials and saves "
            "them locally (shared with Notes/Caroline's own login). NEVER needed if this machine is already "
            "logged in -- try sms_get_account_status first.",
            {"email": str, "password": str}, sms_login,
        ),
        PluginTool(
            "sms_set_account",
            "Registers YOUR OWN SMTP2GO account (API key, optionally a dedicated sending number) for "
            "sms_send/sms_view_received to use. The key is verified before being saved.",
            {"smtp2go_api_key": str, "smtp2go_sender": str | None}, sms_set_account,
        ),
        PluginTool(
            "sms_get_account_status",
            "Reports whether you have a registered SMTP2GO account and its sender number, if set. Never "
            "reveals the API key itself.",
            {}, sms_get_account_status,
        ),
        PluginTool(
            "sms_remove_account",
            "Deletes your registered SMTP2GO account. sms_send/sms_view_received will fail until you register "
            "a new one.",
            {}, sms_remove_account,
        ),
        PluginTool(
            "sms_send",
            "Sends an SMS to one or more numbers (E.164 format) via YOUR OWN registered SMTP2GO account. Costs "
            "real money on your own SMTP2GO account.",
            {"destination": str | list, "content": str, "sender": str | None}, sms_send,
        ),
        PluginTool(
            "sms_view_received",
            "Lists replies received on YOUR OWN registered SMTP2GO account since start_date (defaults to the "
            "last 7 days). This is polling, not a live inbox.",
            {"start_date": str | None, "end_date": str | None}, sms_view_received,
        ),
    ],
)
