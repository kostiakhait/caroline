"""telegram -- LLM-facing tools over app/telegram_channel.py's Telegram
connection. Second of the planned messenger integrations
(docs/MESSENGER_INTEGRATIONS_PLAN.md, 2026-10-06).

Acts AS the user (Telethon's MTProto Client API, not a bot) -- sees their
own DMs and every group they're in, same as opening Telegram themselves.

Login is conversational (telegram_login_start -> telegram_login_finish),
since Telegram's own login is phone number -> a code sent through Telegram
itself -> an optional 2FA password -- there's no browser OAuth screen the
way Slack has.
"""

from __future__ import annotations

from typing import Any

from app.telegram_channel import (
    TelegramLoginError,
    app_configured,
    get_live_client,
    login_finish,
    login_start,
)
from app.workspace_dir import WORKSPACE_DIR
from app.plugins.loader import Plugin, PluginTool


# Bug fix (2026-10-11), seen live: with the application not registered,
# telegram_list_chats said only "isn't connected yet -- log in first", so
# Caroline asked the owner for a phone number and a code that could never
# work. Every tool now says first that Telegram is not available in this build.
NOT_AVAILABLE = (
    "Telegram can't be used in this build of Caroline: the Caroline Telegram application isn't registered yet "
    "(it needs an api_id/api_hash from my.telegram.org, built into Caroline). Logging in cannot work until a "
    "build with it ships -- do NOT ask the user for a phone number or a login code. Tell them Telegram isn't "
    "available yet."
)
NOT_CONNECTED = "Telegram isn't connected yet -- use telegram_login_start/telegram_login_finish first."


def _not_ready() -> dict[str, Any] | None:
    if not app_configured():
        return {"text": NOT_AVAILABLE, "is_error": True}
    if get_live_client() is None:
        return {"text": NOT_CONNECTED, "is_error": True}
    return None


async def telegram_login_start(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    if not app_configured():
        return {"text": NOT_AVAILABLE, "is_error": True}
    phone = str(args["phone"]).strip()
    try:
        message = await login_start(WORKSPACE_DIR, phone)
    except TelegramLoginError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": message}


async def telegram_login_finish(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    if not app_configured():
        return {"text": NOT_AVAILABLE, "is_error": True}
    code = str(args["code"]).strip()
    password = args.get("password")
    try:
        message = await login_finish(WORKSPACE_DIR, code, str(password) if password else None)
    except TelegramLoginError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": message}


async def telegram_list_chats(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    not_ready = _not_ready()
    if not_ready:
        return not_ready
    client = get_live_client()
    limit = int(args.get("limit") or 50)
    dialogs = await client.get_dialogs(limit=limit)
    rows = [
        {"id": d.id, "name": d.name, "isGroup": d.is_group, "isChannel": d.is_channel, "unread": d.unread_count}
        for d in dialogs
    ]
    return {"text": str(rows)}


async def telegram_send_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    not_ready = _not_ready()
    if not_ready:
        return not_ready
    client = get_live_client()
    try:
        chat_id = int(args["chat"])
    except (TypeError, ValueError):
        return {"text": "chat must be the numeric chat id from telegram_list_chats or a proactive notification.", "is_error": True}
    text = str(args["text"])
    await client.send_message(chat_id, text)
    return {"text": f"Sent to {chat_id}."}


async def telegram_search_messages(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    not_ready = _not_ready()
    if not_ready:
        return not_ready
    client = get_live_client()
    try:
        chat_id = int(args["chat"])
    except (TypeError, ValueError):
        return {"text": "chat must be the numeric chat id from telegram_list_chats -- Telethon has no single-call global search across every chat (unlike Slack), only per-chat.", "is_error": True}
    query = str(args["query"]).strip()
    limit = int(args.get("limit") or 20)
    rows = []
    async for m in client.iter_messages(chat_id, search=query, limit=limit):
        rows.append({"id": m.id, "date": m.date.isoformat() if m.date else None, "text": m.raw_text})
    return {"text": str(rows)}


def _usage_instructions() -> str:
    return (
        "Acts as the user's own Telegram account (their real DMs and groups), not a bot. Login is "
        "conversational: telegram_login_start(phone) sends a code via Telegram itself, then "
        "telegram_login_finish(code) completes it (or asks for a 2FA password if the account has one) -- only "
        "call these when the user has explicitly asked to connect Telegram. `chat` everywhere is the numeric "
        "chat id from telegram_list_chats or a proactive new-message notification, never a guessed name. "
        "telegram_search_messages is per-chat only -- Telethon has no single-call search across every chat the "
        "way Slack's search.messages does."
    )


PLUGIN = Plugin(
    name="telegram",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "telegram_login_start",
            "Starts connecting Caroline to the user's Telegram account: sends a login code to the given phone "
            "number via Telegram. Follow with telegram_login_finish once the user has the code.",
            {"phone": str}, telegram_login_start,
        ),
        PluginTool(
            "telegram_login_finish",
            "Completes Telegram login with the code from telegram_login_start. If the account has two-step "
            "verification, call again with `password` once asked for it.",
            {"code": str, "password": str | None}, telegram_login_finish,
        ),
        PluginTool(
            "telegram_list_chats",
            "Lists the user's Telegram chats (DMs, groups, channels) with their numeric ids -- the id to use "
            "for telegram_send_message/telegram_search_messages.",
            {"limit": int | None}, telegram_list_chats,
        ),
        PluginTool(
            "telegram_send_message",
            "Sends a message to a Telegram chat, as the user. `chat` must be the exact numeric chat id.",
            {"chat": str, "text": str}, telegram_send_message,
        ),
        PluginTool(
            "telegram_search_messages",
            "Searches one Telegram chat's own messages for `query`. `chat` is required (the numeric chat id) "
            "-- see this plugin's own usage_instructions for why there's no cross-chat search.",
            {"chat": str, "query": str, "limit": int | None}, telegram_search_messages,
        ),
    ],
)
