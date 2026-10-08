"""app/telegram_channel.py -- Telegram personal-account connection (Telethon,
MTProto Client API -- NOT a bot), per docs/MESSENGER_INTEGRATIONS_PLAN.md.
Acts as the user, sees their own DMs and groups.

Login is a conversational flow driven by the model, not a native form:
phone number -> the code Telegram sends -> the 2FA password if the account
has one. The resulting Telethon StringSession is encrypted at rest via
app/secret_store.py (DPAPI), never stored plaintext.

TELEGRAM_API_ID / TELEGRAM_API_HASH identify the Caroline APPLICATION (not
the user). They come from one app registration at my.telegram.org -- same
deferred one-time step as the Slack App ("Пока не регистрируем, реализуем"),
so they stay empty until then, and login fails clearly until they're set.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from app.logging_setup import log_event
from app.secret_store import decrypt_text_from_file, encrypt_text_to_file, messenger_secrets_dir
from app.task_supervisor import supervise

TELEGRAM_API_ID = 0
TELEGRAM_API_HASH = ""


class TelegramLoginError(Exception):
    """Surfaced to the model/user as a plain is_error reply, never a crash."""


def _session_path(workspace_dir: str):
    return messenger_secrets_dir(workspace_dir, "telegram") / "session.bin"


def has_telegram_session(workspace_dir: str) -> bool:
    return decrypt_text_from_file(_session_path(workspace_dir)) is not None


def _require_app_credentials() -> None:
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise TelegramLoginError(
            "Telegram isn't set up in this build yet -- the Caroline Telegram application (api_id/api_hash from "
            "my.telegram.org) hasn't been registered/configured. Nothing to do here until that happens."
        )


def _new_client(session: str = "") -> TelegramClient:
    return TelegramClient(StringSession(session), TELEGRAM_API_ID, TELEGRAM_API_HASH)


# One in-flight login at a time: phone_code_hash + the connected client, held
# between the "send me the code" and "here's the code" turns.
_pending: dict[str, Any] | None = None


async def login_start(workspace_dir: str, phone: str) -> str:
    global _pending
    _require_app_credentials()
    if _pending is not None:
        await _pending["client"].disconnect()
    client = _new_client()
    await client.connect()
    sent = await client.send_code_request(phone)
    _pending = {"client": client, "phone": phone, "phone_code_hash": sent.phone_code_hash}
    log_event("plugin:telegram", "login_code_sent")
    return "Telegram sent a login code to that number (in the Telegram app or by SMS). Give it to me to finish."


async def login_finish(workspace_dir: str, code: str, password: str | None = None) -> str:
    global _pending
    if _pending is None:
        raise TelegramLoginError("No Telegram login in progress -- start with the phone number first.")
    client = _pending["client"]
    try:
        await client.sign_in(phone=_pending["phone"], code=code.strip(), phone_code_hash=_pending["phone_code_hash"])
    except SessionPasswordNeededError:
        if not password:
            return "This account has two-step verification. Tell me its password to finish."
        await client.sign_in(password=password)
    except Exception as exc:
        raise TelegramLoginError(f"Telegram rejected the code: {exc}") from exc
    encrypt_text_to_file(_session_path(workspace_dir), client.session.save(), "Telegram session (Telethon StringSession)")
    await client.disconnect()
    _pending = None
    log_event("plugin:telegram", "login_succeeded")
    # The startup loop (start_telegram_channel) notices the new session within
    # its 30s recheck and starts the live client itself -- starting one here
    # too would run two clients on the same account.
    return "Telegram is connected -- it'll start receiving messages within about 30 seconds."


@dataclass
class TelegramChannelStatus:
    linked: bool = False
    connected: bool = False
    own_user_id: int | None = None
    last_event_at_iso: str | None = None
    last_error: str | None = None


_status = TelegramChannelStatus()
_live_client: TelegramClient | None = None


def get_telegram_channel_status() -> dict[str, Any]:
    return asdict(_status)


def get_live_client() -> TelegramClient | None:
    return _live_client


async def _start_background(workspace_dir: str, inject_proactive: Callable[[str], None] | None) -> None:
    """Runs forever once a session exists. Telethon reconnects internally;
    this only returns on an unrecoverable failure (supervise() restarts it)."""
    global _live_client
    session = decrypt_text_from_file(_session_path(workspace_dir))
    if not session:
        return
    _require_app_credentials()
    client = _new_client(session)
    await client.connect()
    if not await client.is_user_authorized():
        _status.linked = False
        log_event("plugin:telegram", "session_not_authorized")
        return
    me = await client.get_me()
    _status.linked = True
    _status.connected = True
    _status.own_user_id = me.id
    _live_client = client

    @client.on(events.NewMessage(incoming=True))
    async def _on_new(event: events.NewMessage.Event) -> None:
        # Private chats always; groups only when the user is actually mentioned
        # or replied to -- otherwise every group chatter would interrupt the turn.
        if not (event.is_private or event.mentioned or event.is_reply):
            return
        text = (event.raw_text or "").strip()
        if not text:
            return
        sender = await event.get_sender()
        name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or str(event.sender_id)
        kind = "DM" if event.is_private else "group mention"
        _status.last_event_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        log_event("plugin:telegram", "message_received", kind=kind, chat_id=event.chat_id)
        if inject_proactive is not None:
            inject_proactive(
                f'[New Telegram {kind} from "{name}" (chat_id={event.chat_id}) -- reply with '
                f"telegram_send_message(chat=\"{event.chat_id}\", text=...) if it needs a response:\n{text}]"
            )

    try:
        await client.run_until_disconnected()
    finally:
        _status.connected = False
        _live_client = None


def start_telegram_channel(workspace_dir: str, inject_proactive: Callable[[str], None]) -> asyncio.Task[None]:
    """Startup loop: if a session already exists, connect; otherwise re-check
    every 30s (cheap file read) until login_finish has stored one."""
    async def _loop() -> None:
        while True:
            if not has_telegram_session(workspace_dir):
                await asyncio.sleep(30)
                continue
            _status.linked = True
            try:
                await _start_background(workspace_dir, inject_proactive)
            except Exception as exc:
                _status.connected = False
                _status.last_error = str(exc)
                log_event("plugin:telegram", "connection_failed", error=str(exc))
            await asyncio.sleep(5.0)

    return supervise("telegram_channel", _loop)
