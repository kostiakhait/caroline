"""app/discord_channel.py -- Discord bot connection (discord.py gateway),
per docs/MESSENGER_INTEGRATIONS_PLAN.md. Third messenger, and the one
BOT-only exception in the plan: a personal Discord account acting as a
"self-bot" violates Discord's own ToS and risks a real account ban, so
unlike Slack/Telegram this never acts as the user -- only as a separate
bot identity, seeing only servers/DMs it was explicitly invited to.

Setup is a plain bot token (created once at discord.com/developers,
Bot tab) -- there's no better alternative the way Slack had OAuth: a
Discord BOT has no "log in as yourself" concept at all, the token IS its
only credential, same as every other Discord bot framework. Stored
DPAPI-encrypted via app/secret_store.py regardless.

MESSAGE CONTENT is a privileged gateway intent, off by default -- the bot
can't read message text at all until it's enabled both in code (below)
AND in the Developer Portal (Bot tab, "Message Content Intent" toggle).
discord_set_token's own result text reminds the user of this.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import discord

from app.logging_setup import log_event
from app.secret_store import decrypt_text_from_file, encrypt_text_to_file, messenger_secrets_dir
from app.task_supervisor import supervise


def _token_path(workspace_dir: str):
    return messenger_secrets_dir(workspace_dir, "discord") / "token.bin"


def get_token(workspace_dir: str) -> str | None:
    return decrypt_text_from_file(_token_path(workspace_dir))


def has_discord_token(workspace_dir: str) -> bool:
    return get_token(workspace_dir) is not None


def set_discord_token(workspace_dir: str, token: str) -> None:
    encrypt_text_to_file(_token_path(workspace_dir), token.strip(), "Discord bot token")
    log_event("plugin:discord", "token_set")


@dataclass
class DiscordChannelStatus:
    linked: bool = False
    connected: bool = False
    own_user_id: int | None = None
    last_event_at_iso: str | None = None
    last_error: str | None = None


_status = DiscordChannelStatus()
_live_client: discord.Client | None = None


def get_discord_channel_status() -> dict[str, Any]:
    return asdict(_status)


def get_live_client() -> discord.Client | None:
    return _live_client


async def _run_client(workspace_dir: str, inject_proactive: Callable[[str], None]) -> None:
    global _live_client
    token = get_token(workspace_dir)
    assert token  # caller already checked has_discord_token

    intents = discord.Intents.default()
    intents.message_content = True  # privileged -- also needs the Developer Portal toggle, see this module's own doc comment
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        _status.connected = True
        _status.own_user_id = client.user.id if client.user else None
        log_event("plugin:discord", "connected", user_id=_status.own_user_id)

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author == client.user:
            return
        is_dm = isinstance(message.channel, discord.DMChannel)
        mentioned = client.user is not None and client.user in message.mentions
        if not (is_dm or mentioned):
            return
        text = (message.content or "").strip()
        if not text:
            return
        kind = "DM" if is_dm else "mention"
        _status.last_event_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        log_event("plugin:discord", "message_received", kind=kind, channel_id=message.channel.id)
        inject_proactive(
            f'[New Discord {kind} from "{message.author}" (channel={message.channel.id}) -- reply with '
            f'discord_send_message(channel="{message.channel.id}", text=...) if it needs a response:\n{text}]'
        )

    _live_client = client
    try:
        await client.start(token)
    finally:
        _status.connected = False
        _live_client = None


def start_discord_channel(workspace_dir: str, inject_proactive: Callable[[str], None]) -> asyncio.Task[None]:
    async def _loop() -> None:
        while True:
            linked = has_discord_token(workspace_dir)
            _status.linked = linked
            if not linked:
                await asyncio.sleep(30)
                continue
            try:
                await _run_client(workspace_dir, inject_proactive)
            except Exception as exc:
                _status.connected = False
                _status.last_error = str(exc)
                log_event("plugin:discord", "connection_failed", error=str(exc))
                await asyncio.sleep(5.0)

    return supervise("discord_channel", _loop)
