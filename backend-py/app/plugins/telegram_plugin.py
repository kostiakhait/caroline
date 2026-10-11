"""telegram -- LLM-facing tools over app/telegram_channel.py's Telegram
connections. Second of the planned messenger integrations
(docs/MESSENGER_INTEGRATIONS_PLAN.md, 2026-10-06).

TWO separate Telegram accounts (decision 2026-10-11), selected by `account`
on every tool: "self" (default) is Caroline's OWN Telegram account -- her
own DMs and groups, hers to read and answer freely, same as any other tool
she uses on her own initiative. "owner" is the human owner's personal
Telegram account, linked so Caroline can read it and keep them posted, but
she may send AS the owner only when they have explicitly asked her to, for
that specific message -- see telegram_send_message's own description. This
is a permission rule, not a technical one (nothing here can check "did the
owner really ask for this" -- the model has to actually follow it, the same
shape as policies.py's no_unauthorized_secret_changes_instruction).

Login is conversational (telegram_login_start -> telegram_login_finish),
since Telegram's own login is phone number -> a code sent through Telegram
itself -> an optional 2FA password -- there's no browser OAuth screen the
way Slack has. Login always names which account it is for.
"""

from __future__ import annotations

from typing import Any

from app.telegram_channel import (
    ACCOUNTS,
    DEFAULT_ACCOUNT,
    TelegramLoginError,
    app_configured,
    get_live_client,
    is_valid_account,
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


def _not_connected(account: str) -> str:
    return f'The "{account}" Telegram account isn\'t connected yet -- use telegram_login_start/telegram_login_finish with account="{account}" first.'


def _account_or_error(args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    account = str(args.get("account") or DEFAULT_ACCOUNT).strip()
    if not is_valid_account(account):
        return account, {"text": f'"account" must be one of {", ".join(ACCOUNTS)}, not {account!r}.', "is_error": True}
    return account, None


def _not_ready(account: str) -> dict[str, Any] | None:
    if not app_configured():
        return {"text": NOT_AVAILABLE, "is_error": True}
    if get_live_client(account) is None:
        return {"text": _not_connected(account), "is_error": True}
    return None


async def telegram_login_start(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    if not app_configured():
        return {"text": NOT_AVAILABLE, "is_error": True}
    account, error = _account_or_error(args)
    if error:
        return error
    phone = str(args["phone"]).strip()
    try:
        message = await login_start(WORKSPACE_DIR, phone, account)
    except TelegramLoginError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": message}


async def telegram_login_finish(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    if not app_configured():
        return {"text": NOT_AVAILABLE, "is_error": True}
    account, error = _account_or_error(args)
    if error:
        return error
    code = str(args["code"]).strip()
    password = args.get("password")
    try:
        message = await login_finish(WORKSPACE_DIR, code, account, str(password) if password else None)
    except TelegramLoginError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": message}


async def telegram_list_chats(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    client = get_live_client(account)
    limit = int(args.get("limit") or 50)
    dialogs = await client.get_dialogs(limit=limit)
    rows = [
        {"id": d.id, "name": d.name, "isGroup": d.is_group, "isChannel": d.is_channel, "unread": d.unread_count}
        for d in dialogs
    ]
    return {"text": str(rows)}


async def telegram_send_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    client = get_live_client(account)
    try:
        chat_id = int(args["chat"])
    except (TypeError, ValueError):
        return {"text": "chat must be the numeric chat id from telegram_list_chats or a proactive notification.", "is_error": True}
    text = str(args["text"])
    await client.send_message(chat_id, text)
    return {"text": f'Sent to {chat_id} (account="{account}").'}


async def telegram_search_messages(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    client = get_live_client(account)
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
        "TWO separate Telegram accounts, chosen with \"account\" on every tool: \"self\" (the default -- your "
        "OWN Telegram account, your own DMs and groups, yours to read and answer freely on your own initiative, "
        "same as any other tool) and \"owner\" (the human owner's personal Telegram account -- you may read it "
        "and tell them what's in it, but see telegram_send_message's own description before ever sending "
        "anything there). Login is conversational and always names the account: "
        "telegram_login_start(phone, account) sends a code via Telegram itself, then "
        "telegram_login_finish(code, account) completes it (or asks for a 2FA password if the account has one). "
        "`chat` everywhere is the numeric chat id from telegram_list_chats or a proactive new-message "
        "notification, never a guessed name. telegram_search_messages is per-chat only -- Telethon has no "
        "single-call search across every chat the way Slack's search.messages does."
    )


PLUGIN = Plugin(
    name="telegram",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "telegram_login_start",
            "Starts connecting a Telegram account: sends a login code to the given phone number via Telegram. "
            "`account`: \"self\" to connect Caroline's own Telegram account, \"owner\" to connect the human "
            "owner's personal account -- always confirm which one before calling this. Follow with "
            "telegram_login_finish (same account) once they have the code.",
            {"phone": str, "account": str}, telegram_login_start,
        ),
        PluginTool(
            "telegram_login_finish",
            "Completes Telegram login with the code from telegram_login_start, for the SAME `account`. If the "
            "account has two-step verification, call again with `password` once asked for it.",
            {"code": str, "account": str, "password": str | None}, telegram_login_finish,
        ),
        PluginTool(
            "telegram_list_chats",
            "Lists a Telegram account's chats (DMs, groups, channels) with their numeric ids -- the id to use "
            "for telegram_send_message/telegram_search_messages. `account`: \"self\" (default, your own "
            "account) or \"owner\" (the owner's own account).",
            {"limit": int | None, "account": str | None}, telegram_list_chats,
        ),
        PluginTool(
            "telegram_send_message",
            "Sends a Telegram message. `account` (default \"self\") picks which account it is sent FROM: "
            "\"self\" sends as you, your own account, your own call to make like any other tool. \"owner\" "
            "sends AS THE OWNER, from their own personal account -- use this ONLY when the owner has explicitly "
            "asked you, for this specific message, to send it from their account; never on your own initiative, "
            "never because a past approval or a similar earlier message makes it seem fine. If in doubt, ask "
            "them first rather than send as them. `chat` must be the exact numeric chat id.",
            {"chat": str, "text": str, "account": str | None}, telegram_send_message,
        ),
        PluginTool(
            "telegram_search_messages",
            "Searches one Telegram chat's own messages for `query`. `chat` is required (the numeric chat id) "
            "-- see this plugin's own usage_instructions for why there's no cross-chat search. `account`: "
            "\"self\" (default) or \"owner\".",
            {"chat": str, "query": str, "limit": int | None, "account": str | None}, telegram_search_messages,
        ),
    ],
)
