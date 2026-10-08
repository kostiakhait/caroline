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
    get_live_client,
    login_finish,
    login_start,
)
from app.workspace_dir import WORKSPACE_DIR
from app.plugins.loader import Plugin, PluginTool


async def telegram_login_start(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    phone = str(args["phone"]).strip()
    try:
        message = await login_start(WORKSPACE_DIR, phone)
    except TelegramLoginError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": message}


async def telegram_login_finish(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    code = str(args["code"]).strip()
    password = args.get("password")
    try:
        message = await login_finish(WORKSPACE_DIR, code, str(password) if password else None)
    except TelegramLoginError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": message}


async def telegram_list_chats(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    client = get_live_client()
    if client is None:
        return {"text": "Telegram isn't connected yet -- use telegram_login_start/telegram_login_finish first.", "is_error": True}
    limit = int(args.get("limit") or 50)
    dialogs = await client.get_dialogs(limit=limit)
    rows = [
        {"id": d.id, "name": d.name, "isGroup": d.is_group, "isChannel": d.is_channel, "unread": d.unread_count}
        for d in dialogs
    ]
    return {"text": str(rows)}


async def telegram_send_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    client = get_live_client()
    if client is None:
        return {"text": "Telegram isn't connected yet -- use telegram_login_start/telegram_login_finish first.", "is_error": True}
    try:
        chat_id = int(args["chat"])
    except (TypeError, ValueError):
        return {"text": "chat must be the numeric chat id from telegram_list_chats or a proactive notification.", "is_error": True}
    text = str(args["text"])
    await client.send_message(chat_id, text)
    return {"text": f"Sent to {chat_id}."}


async def telegram_search_messages(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    client = get_live_client()
    if client is None:
        return {"text": "Telegram isn't connected yet -- use telegram_login_start/telegram_login_finish first.", "is_error": True}
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
