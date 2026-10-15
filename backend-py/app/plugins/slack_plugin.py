"""slack -- LLM-facing tools over app/slack_channel.py's Slack connection.
First of the planned messenger integrations (docs/MESSENGER_INTEGRATIONS_PLAN.md,
2026-10-06). Read and send from turn one, not read-only -- per explicit
instruction.

TWO accounts, same split as Telegram's (2026-10-11): `account="self"`
(default) is Caroline's own separate Slack bot identity -- read/reply
there freely, no extra permission needed, but it only sees channels/DMs
the bot itself was invited to. `account="owner"` acts AS the human owner
(their own DMs and every channel they're a member of, same as opening
Slack themselves) -- read freely, but only send as them when they've
explicitly asked for that specific message. See slack_channel.py's own
doc comment for why both share one Socket Mode connection and why the
bot-token half isn't confirmed working against a live install yet.
"""

from __future__ import annotations

from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from app.slack_channel import (
    ACCOUNTS,
    DEFAULT_ACCOUNT,
    SlackLoginError,
    get_token_for_account,
    run_oauth_login,
)
from app.workspace_dir import WORKSPACE_DIR
from app.plugins.loader import Plugin, PluginTool
from app.plugins.messenger_search import find_by_name


def _account_or_error(args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    account = str(args.get("account") or DEFAULT_ACCOUNT).strip()
    if account not in ACCOUNTS:
        return account, {"text": f'"account" must be one of {", ".join(ACCOUNTS)}, not {account!r}.', "is_error": True}
    return account, None


def _client_or_error(account: str) -> tuple[AsyncWebClient | None, dict[str, Any] | None]:
    token = get_token_for_account(WORKSPACE_DIR, account)
    if not token:
        return None, {"text": f'Slack ("{account}") isn\'t linked yet -- use slack_login(account="{account}") first.', "is_error": True}
    return AsyncWebClient(token=token), None


async def slack_login(_args: dict[str, Any], report_progress: Any) -> dict[str, Any]:
    def _progress(message: str) -> None:
        if report_progress is not None:
            report_progress(message)

    try:
        result = await run_oauth_login(WORKSPACE_DIR, _progress)
    except SlackLoginError as exc:
        return {"text": str(exc), "is_error": True}
    team = result.get("team") or "your Slack workspace"
    bot_note = " Caroline's own bot identity was also linked." if result.get("hasBotToken") else " (No separate bot identity came back -- only the owner's own account is usable for now.)"
    return {"text": f"Signed in to Slack ({team}). Try slack_list_channels to confirm.{bot_note}"}


async def slack_list_channels(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    client, error = _client_or_error(account)
    if error:
        return error
    try:
        result = await client.conversations_list(
            types="public_channel,private_channel,mpim,im", exclude_archived=True, limit=200,
        )
    except SlackApiError as exc:
        return {"text": f"Slack API error: {exc.response.get('error', str(exc))}", "is_error": True}
    rows = []
    for ch in result.get("channels", []):
        kind = "dm" if ch.get("is_im") else "group_dm" if ch.get("is_mpim") else "private_channel" if ch.get("is_private") else "channel"
        rows.append({"id": ch["id"], "name": ch.get("name") or ch.get("user") or ch["id"], "kind": kind})
    return {"text": str(rows)}


async def slack_find_channel(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    client, error = _client_or_error(account)
    if error:
        return error
    query = str(args["query"]).strip()
    if not query:
        return {"text": 'slack_find_channel needs "query".', "is_error": True}
    try:
        result = await client.conversations_list(
            types="public_channel,private_channel,mpim,im", exclude_archived=True, limit=200,
        )
    except SlackApiError as exc:
        return {"text": f"Slack API error: {exc.response.get('error', str(exc))}", "is_error": True}
    rows = []
    for ch in result.get("channels", []):
        kind = "dm" if ch.get("is_im") else "group_dm" if ch.get("is_mpim") else "private_channel" if ch.get("is_private") else "channel"
        rows.append({"id": ch["id"], "name": ch.get("name") or ch.get("user") or ch["id"], "kind": kind})
    matches = await find_by_name(query, rows)
    return {"text": str(matches)}


async def slack_send_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    client, error = _client_or_error(account)
    if error:
        return error
    channel = str(args["channel"]).strip()
    text = str(args["text"])
    try:
        await client.chat_postMessage(channel=channel, text=text)
    except SlackApiError as exc:
        return {"text": f"Slack API error: {exc.response.get('error', str(exc))}", "is_error": True}
    return {"text": f"Sent to {channel}."}


async def slack_search_messages(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    client, error = _client_or_error(account)
    if error:
        return error
    query = str(args["query"]).strip()
    channel = args.get("channel")
    count = int(args.get("limit") or 20)
    try:
        if channel:
            # A specific channel's own recent history, filtered client-side --
            # search.messages' own "in:" operator needs a channel NAME, not
            # the ID this plugin otherwise deals in exclusively (see
            # slack_send_message's own doc comment on why IDs, not names).
            result = await client.conversations_history(channel=str(channel).strip(), limit=200)
            msgs = [m for m in result.get("messages", []) if query.lower() in (m.get("text") or "").lower()][:count]
            rows = [{"ts": m.get("ts"), "user": m.get("user"), "text": m.get("text")} for m in msgs]
        else:
            # search.messages is a user-token-only Slack API -- a bot
            # token (account="self") has no equivalent at all.
            if account == "self":
                return {"text": 'Workspace-wide search needs account="owner" -- Slack has no bot-token search API.', "is_error": True}
            result = await client.search_messages(query=query, count=count)
            matches = (result.get("messages") or {}).get("matches", [])
            rows = [
                {"channel": m.get("channel", {}).get("id"), "channelName": m.get("channel", {}).get("name"), "ts": m.get("ts"), "user": m.get("user"), "text": m.get("text")}
                for m in matches
            ]
    except SlackApiError as exc:
        return {"text": f"Slack API error: {exc.response.get('error', str(exc))}", "is_error": True}
    return {"text": str(rows)}


def _usage_instructions() -> str:
    return (
        "Two accounts, same split as Telegram's: `account=\"self\"` (default) is Caroline's own separate "
        "Slack bot identity -- read/reply there freely, no extra permission needed, but it only sees "
        "channels/DMs the bot itself was invited to. `account=\"owner\"` acts as the user's own Slack account "
        "(their real DMs and every channel they belong to) -- read freely, but only send as them when they've "
        "explicitly asked for that specific message. `channel` everywhere here is the raw Slack conversation "
        "ID (e.g. \"C0123ABCD\" for a channel, \"D0123ABCD\" for a DM) -- always use the EXACT id from "
        "slack_list_channels or from a proactive new-message notification, never a guessed #name or @user, "
        "same reasoning as every other messenger/channel integration in this codebase: a bare name is "
        "ambiguous and a wrong guess sends to the wrong place silently. slack_login opens the user's own "
        "browser to Slack's real login/consent screen (a normal OAuth flow, requesting both identities at "
        "once) -- call it only when the user has actually asked to connect Slack, never on your own "
        "initiative, and tell them to watch for the browser window that opens. Workspace-wide search "
        "(slack_search_messages without `channel`) only works with account=\"owner\" -- Slack has no "
        "bot-token search API at all."
    )


PLUGIN = Plugin(
    name="slack",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "slack_login",
            "Connects Caroline to Slack: opens the user's own browser to Slack's real login/consent screen "
            "(normal OAuth, no tokens to copy/paste) -- requests both Caroline's own bot identity and the "
            "owner's own account in one authorization. Waits for them to approve, then saves whichever "
            "token(s) come back, encrypted. Call this only when the user has explicitly asked to connect Slack.",
            {}, slack_login,
        ),
        PluginTool(
            "slack_list_channels",
            "Lists every channel, private channel, group DM and 1:1 DM an account's Slack identity belongs "
            "to (or, for \"self\", was invited to), each with its raw conversation id -- the id to use for "
            "slack_send_message/slack_search_messages. `account`: \"self\" (default) or \"owner\".",
            {"account": str | None}, slack_list_channels,
        ),
        PluginTool(
            "slack_find_channel",
            "Finds the channel/DM(s) matching a name/description, by MEANING (transliteration, diminutives, "
            "typos, word order) -- never exact text. Use this instead of slack_list_channels whenever you're "
            "looking for one specific channel/person rather than browsing. Returns the same row shape as "
            "slack_list_channels, only the ones that actually match -- possibly empty if genuinely nothing "
            "fits. `account`: \"self\" (default) or \"owner\".",
            {"query": str, "account": str | None}, slack_find_channel,
        ),
        PluginTool(
            "slack_send_message",
            "Sends a message to a Slack channel or DM. `channel` must be the exact conversation id (see this "
            "plugin's own usage_instructions). `account`: \"self\" (default) or \"owner\" -- only send as "
            "\"owner\" when the user has explicitly asked for this specific message.",
            {"channel": str, "text": str, "account": str | None}, slack_send_message,
        ),
        PluginTool(
            "slack_search_messages",
            "Searches Slack messages matching `query`. Without `channel`, searches across the whole "
            "workspace (Slack's own search.messages, account=\"owner\" only -- no bot-token equivalent "
            "exists). With `channel` (a conversation id), searches that conversation's own recent history "
            "instead (works for either account). `limit` caps how many results come back (default 20). "
            "`account`: \"self\" (default) or \"owner\".",
            {"query": str, "channel": str | None, "limit": int | None, "account": str | None}, slack_search_messages,
        ),
    ],
)
