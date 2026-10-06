"""slack -- LLM-facing tools over app/slack_channel.py's Slack connection.
First of the planned messenger integrations (docs/MESSENGER_INTEGRATIONS_PLAN.md,
2026-10-06). Read and send from turn one, not read-only -- per explicit
instruction.

Acts AS the user (user token, xoxp-), not as a separate bot identity --
sees the user's own DMs and every channel they're a member of, same as
opening Slack themselves. See slack_channel.py's own doc comment for
why Socket Mode needs a SEPARATE app-level token purely for the
connection, distinct from this one.
"""

from __future__ import annotations

from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from app.slack_channel import SlackLoginError, get_user_token, run_oauth_login
from app.workspace_dir import WORKSPACE_DIR
from app.plugins.loader import Plugin, PluginTool


def _client() -> AsyncWebClient | None:
    token = get_user_token(WORKSPACE_DIR)
    return AsyncWebClient(token=token) if token else None


async def slack_login(_args: dict[str, Any], report_progress: Any) -> dict[str, Any]:
    def _progress(message: str) -> None:
        if report_progress is not None:
            report_progress(message)

    try:
        result = await run_oauth_login(WORKSPACE_DIR, _progress)
    except SlackLoginError as exc:
        return {"text": str(exc), "is_error": True}
    team = result.get("team") or "your Slack workspace"
    return {"text": f"Signed in to Slack ({team}). Try slack_list_channels to confirm."}


async def slack_list_channels(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    client = _client()
    if client is None:
        return {"text": "Slack isn't linked yet -- use slack_login first.", "is_error": True}
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


async def slack_send_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    client = _client()
    if client is None:
        return {"text": "Slack isn't linked yet -- use slack_login first.", "is_error": True}
    channel = str(args["channel"]).strip()
    text = str(args["text"])
    try:
        await client.chat_postMessage(channel=channel, text=text)
    except SlackApiError as exc:
        return {"text": f"Slack API error: {exc.response.get('error', str(exc))}", "is_error": True}
    return {"text": f"Sent to {channel}."}


async def slack_search_messages(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    client = _client()
    if client is None:
        return {"text": "Slack isn't linked yet -- use slack_login first.", "is_error": True}
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
        "Acts as the user's own Slack account (their real DMs and every channel they belong to), not a separate "
        "bot. `channel` everywhere here is the raw Slack conversation ID (e.g. \"C0123ABCD\" for a channel, "
        "\"D0123ABCD\" for a DM) -- always use the EXACT id from slack_list_channels or from a proactive "
        "new-message notification, never a guessed #name or @user, same reasoning as every other messenger/"
        "channel integration in this codebase: a bare name is ambiguous and a wrong guess sends to the wrong "
        "place silently. slack_login opens the user's own browser to Slack's real login/consent screen (a "
        "normal OAuth flow) -- call it only when the user has actually asked to connect Slack, never on your "
        "own initiative, and tell them to watch for the browser window that opens."
    )


PLUGIN = Plugin(
    name="slack",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "slack_login",
            "Connects Caroline to the user's Slack account: opens their own browser to Slack's real login/"
            "consent screen (normal OAuth, no tokens to copy/paste). Waits for them to approve, then saves "
            "the result encrypted. Call this only when the user has explicitly asked to connect Slack.",
            {}, slack_login,
        ),
        PluginTool(
            "slack_list_channels",
            "Lists every channel, private channel, group DM and 1:1 DM the user's Slack account belongs to, "
            "each with its raw conversation id -- the id to use for slack_send_message/slack_search_messages.",
            {}, slack_list_channels,
        ),
        PluginTool(
            "slack_send_message",
            "Sends a message to a Slack channel or DM, as the user. `channel` must be the exact conversation "
            "id (see this plugin's own usage_instructions).",
            {"channel": str, "text": str}, slack_send_message,
        ),
        PluginTool(
            "slack_search_messages",
            "Searches Slack messages matching `query`. Without `channel`, searches across the whole "
            "workspace (Slack's own search.messages). With `channel` (a conversation id), searches that "
            "conversation's own recent history instead. `limit` caps how many results come back (default 20).",
            {"query": str, "channel": str | None, "limit": int | None}, slack_search_messages,
        ),
    ],
)
