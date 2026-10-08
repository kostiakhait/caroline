"""discord -- LLM-facing tools over app/discord_channel.py's Discord
connection. Third of the planned messenger integrations
(docs/MESSENGER_INTEGRATIONS_PLAN.md, 2026-10-06).

Bot-only, unlike Slack/Telegram -- see discord_channel.py's own doc
comment for why a personal-account self-bot isn't an option here. Sees
only servers/DMs the bot was explicitly invited to, not the user's own
full account.
"""

from __future__ import annotations

from typing import Any

import discord

from app.discord_channel import get_live_client, has_discord_token, set_discord_token
from app.workspace_dir import WORKSPACE_DIR
from app.plugins.loader import Plugin, PluginTool


async def discord_set_token(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    token = str(args["token"]).strip()
    if not token:
        return {"text": "token is required.", "is_error": True}
    set_discord_token(WORKSPACE_DIR, token)
    return {
        "text": "Discord bot token saved (encrypted). Two things to check if it doesn't come up within a few "
        "seconds: the bot must be invited to at least one server (OAuth2 > URL Generator, \"bot\" scope), and "
        "\"Message Content Intent\" must be enabled for it (Developer Portal > Bot tab) -- without that toggle "
        "the bot connects but can't read any message text at all."
    }


async def discord_list_channels(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    client = get_live_client()
    if client is None:
        return {"text": "Discord isn't connected yet -- use discord_set_token first.", "is_error": True}
    rows = []
    for guild in client.guilds:
        for channel in guild.text_channels:
            rows.append({"id": channel.id, "name": f"{guild.name}#{channel.name}", "kind": "channel"})
    for dm in client.private_channels:
        if isinstance(dm, discord.DMChannel) and dm.recipient is not None:
            rows.append({"id": dm.id, "name": f"DM: {dm.recipient}", "kind": "dm"})
    return {"text": str(rows)}


async def discord_send_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    client = get_live_client()
    if client is None:
        return {"text": "Discord isn't connected yet -- use discord_set_token first.", "is_error": True}
    try:
        channel_id = int(args["channel"])
    except (TypeError, ValueError):
        return {"text": "channel must be the numeric channel/DM id from discord_list_channels or a proactive notification.", "is_error": True}
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.DiscordException as exc:
            return {"text": f"Couldn't find channel {channel_id}: {exc}", "is_error": True}
    await channel.send(str(args["text"]))
    return {"text": f"Sent to {channel_id}."}


async def discord_search_messages(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    client = get_live_client()
    if client is None:
        return {"text": "Discord isn't connected yet -- use discord_set_token first.", "is_error": True}
    try:
        channel_id = int(args["channel"])
    except (TypeError, ValueError):
        return {"text": "channel must be the numeric channel/DM id -- Discord has no bot-accessible cross-channel search.", "is_error": True}
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    query = str(args["query"]).strip().lower()
    limit = int(args.get("limit") or 20)
    rows = []
    async for m in channel.history(limit=200):
        if query in (m.content or "").lower():
            rows.append({"id": m.id, "author": str(m.author), "createdAt": m.created_at.isoformat(), "text": m.content})
            if len(rows) >= limit:
                break
    return {"text": str(rows)}


def _usage_instructions() -> str:
    return (
        "A Discord BOT, not the user's own account (self-bots violate Discord's ToS -- see this plugin's own "
        "module doc comment) -- sees only servers it was invited to and DMs sent directly to it, never the "
        "user's full personal account. `channel` everywhere is the numeric channel/DM id from "
        "discord_list_channels or a proactive new-message notification. discord_search_messages is per-channel "
        "only (Discord's bot API has no cross-channel search). discord_set_token is one-time setup -- call it "
        "only when the user has explicitly asked to connect a Discord bot, never on your own initiative."
    )


PLUGIN = Plugin(
    name="discord",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "discord_set_token",
            "One-time setup: saves a Discord bot token (encrypted), created by the user at "
            "discord.com/developers (New Application > Bot tab > Reset Token).",
            {"token": str}, discord_set_token,
        ),
        PluginTool(
            "discord_list_channels",
            "Lists every text channel in a server the bot has joined, plus any open DMs, each with its "
            "numeric id -- the id to use for discord_send_message/discord_search_messages.",
            {}, discord_list_channels,
        ),
        PluginTool(
            "discord_send_message",
            "Sends a message to a Discord channel or DM, as the bot. `channel` must be the exact numeric id.",
            {"channel": str, "text": str}, discord_send_message,
        ),
        PluginTool(
            "discord_search_messages",
            "Searches one Discord channel's recent messages (up to the last 200) for `query`. `channel` is "
            "required -- see this plugin's own usage_instructions for why there's no cross-channel search.",
            {"channel": str, "query": str, "limit": int | None}, discord_search_messages,
        ),
    ],
)
