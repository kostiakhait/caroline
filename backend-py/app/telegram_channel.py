"""app/telegram_channel.py -- Telegram connections (Telethon, MTProto Client
API -- NOT a bot), per docs/MESSENGER_INTEGRATIONS_PLAN.md.

TWO separate accounts, not one (decision 2026-10-11, after the single-
account design above shipped but before any real login ever worked):
"self" is Caroline's OWN Telegram account (her own number, her own DMs and
groups -- she may read and reply there as herself, no extra permission
needed); "owner" is the human owner's personal Telegram account, linked so
Caroline can read it and keep them posted, but may send AS the owner only
when they have explicitly asked her to, for that specific message (see
telegram_send_message's own description -- this is a permission rule, not
a technical gate, same shape as policies.py's
no_unauthorized_secret_changes_instruction: nothing here can enforce it
mechanically, the model has to actually follow it). Both accounts use the
SAME Caroline application (api_id/api_hash identify the app, not the
account -- Telegram allows many accounts under one app) and otherwise work
identically: their own session file, their own live client, their own
login flow, their own incoming-message notifications.

Login is a conversational flow driven by the model, not a native form:
phone number -> the code Telegram sends -> the 2FA password if the account
has one. The resulting Telethon StringSession is encrypted at rest via
app/secret_store.py (DPAPI), never stored plaintext.

TELEGRAM_API_ID / TELEGRAM_API_HASH identify the Caroline APPLICATION
itself (not any one account). They come from one app registration at
my.telegram.org -- a one-time step done by a human with a Telegram account
and phone (Caroline cannot do this herself), same deferred step as the
Slack App ("Пока не регистрируем, реализуем"). Embedded directly in this
file once registered (decision 2026-10-11: same as how official Telegram
clients ship their own api_id/api_hash built in -- Telegram's own rule is
only that api_hash not be published source-available/to end users at
large, not that an app can't carry its own); until then they stay empty
and every login attempt fails with a clear, non-misleading message.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from telethon import TelegramClient, events, utils
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import BlockRequest, DeleteContactsRequest, ImportContactsRequest, UnblockRequest
from telethon.tl.types import InputPhoneContact

from app.logging_setup import log_event
from app.secret_store import decrypt_text_from_file, encrypt_text_to_file, messenger_secrets_dir
from app.task_supervisor import supervise

TELEGRAM_API_ID = 33974656
TELEGRAM_API_HASH = "4b0c8b70111e8830ca4be9a698870ad5"

ACCOUNTS = ("self", "owner")
DEFAULT_ACCOUNT = "self"


class TelegramLoginError(Exception):
    """Surfaced to the model/user as a plain is_error reply, never a crash."""


def is_valid_account(account: str) -> bool:
    return account in ACCOUNTS


def _check_account(account: str) -> str:
    if account not in ACCOUNTS:
        raise TelegramLoginError(f'"account" must be one of {", ".join(ACCOUNTS)}, not {account!r}.')
    return account


def _session_path(workspace_dir: str, account: str):
    return messenger_secrets_dir(workspace_dir, "telegram") / f"session_{account}.bin"


def has_telegram_session(workspace_dir: str, account: str) -> bool:
    return decrypt_text_from_file(_session_path(workspace_dir, account)) is not None


def app_configured() -> bool:
    """Whether this build carries the Caroline Telegram application's
    api_id/api_hash; without them no login can work at all, for either
    account."""
    return bool(TELEGRAM_API_ID and TELEGRAM_API_HASH)


def _require_app_credentials() -> None:
    if not app_configured():
        raise TelegramLoginError(
            "Telegram isn't set up in this build yet -- the Caroline Telegram application (api_id/api_hash from "
            "my.telegram.org) hasn't been registered/configured. Nothing to do here until that happens."
        )


def _new_client(session: str = "") -> TelegramClient:
    return TelegramClient(StringSession(session), TELEGRAM_API_ID, TELEGRAM_API_HASH)


# One in-flight login per account: phone_code_hash + the connected client,
# held between the "send me the code" and "here's the code" turns. A login
# of one account never disturbs an in-progress login of the other.
_pending: dict[str, dict[str, Any]] = {}


async def login_start(workspace_dir: str, phone: str, account: str) -> str:
    _check_account(account)
    _require_app_credentials()
    old = _pending.get(account)
    if old is not None:
        await old["client"].disconnect()
    client = _new_client()
    await client.connect()
    sent = await client.send_code_request(phone)
    _pending[account] = {"client": client, "phone": phone, "phone_code_hash": sent.phone_code_hash}
    log_event("plugin:telegram", "login_code_sent", account=account)
    return "Telegram sent a login code to that number (in the Telegram app or by SMS). Give it to me to finish."


async def login_finish(workspace_dir: str, code: str, account: str, password: str | None = None) -> str:
    _check_account(account)
    pending = _pending.get(account)
    if pending is None:
        raise TelegramLoginError(f'No Telegram login in progress for the "{account}" account -- start with the phone number first.')
    client = pending["client"]
    try:
        await client.sign_in(phone=pending["phone"], code=code.strip(), phone_code_hash=pending["phone_code_hash"])
    except SessionPasswordNeededError:
        if not password:
            return "This account has two-step verification. Tell me its password to finish."
        await client.sign_in(password=password)
    except Exception as exc:
        raise TelegramLoginError(f"Telegram rejected the code: {exc}") from exc
    encrypt_text_to_file(_session_path(workspace_dir, account), client.session.save(), f"Telegram session ({account}, Telethon StringSession)")
    await client.disconnect()
    del _pending[account]
    log_event("plugin:telegram", "login_succeeded", account=account)
    # The startup loop (start_telegram_channel) notices the new session within
    # its 30s recheck and starts the live client itself -- starting one here
    # too would run two clients on the same account.
    return "Telegram is connected -- it'll start receiving messages within about 30 seconds."


async def resolve_or_add_contact(client: TelegramClient, phone: str, name: str | None = None) -> tuple[int | None, str | None]:
    """Resolves a bare phone number to a Telegram user id, adding them to
    this account's Telegram contacts if needed (telethon.contacts.
    ImportContactsRequest) -- Telegram's own rule for starting a chat cold:
    an account may only message someone who is already "known" to it (a
    contact, or an existing dialog); a bare phone number alone is refused.
    This is exactly what the ordinary Telegram app does the moment you add
    a number to your contacts. Returns (user_id, display_name) on success,
    or (None, reason) when Telegram has no match for that number -- either
    it is not registered on Telegram, or its owner's privacy settings hide
    them from being found this way (both look identical from here; there
    is no way to tell them apart)."""
    phone = phone.strip()
    first_name = (name or "").strip() or "Contact"
    parts = first_name.split(None, 1)
    first_name, last_name = parts[0], (parts[1] if len(parts) > 1 else "")
    result = await client(ImportContactsRequest(contacts=[InputPhoneContact(client_id=0, phone=phone, first_name=first_name, last_name=last_name)]))
    if not result.users:
        return None, (
            f"Telegram has no account reachable at {phone} -- either that number isn't registered on Telegram, "
            "or its owner's privacy settings hide them from being found by phone number (these look identical "
            "from here). Ask the owner to confirm the number, or have that person message first instead."
        )
    user = result.users[0]
    display = " ".join(filter(None, [getattr(user, "first_name", None), getattr(user, "last_name", None)])) or phone
    return user.id, display


async def delete_contact(client: TelegramClient, chat_id: int) -> None:
    """Removes someone from this account's Telegram contacts (does not
    block them, does not delete the chat history)."""
    entity = await client.get_input_entity(chat_id)
    await client(DeleteContactsRequest(id=[utils.get_input_user(entity)]))


async def block_user(client: TelegramClient, chat_id: int) -> None:
    entity = await client.get_input_entity(chat_id)
    await client(BlockRequest(id=entity))


async def unblock_user(client: TelegramClient, chat_id: int) -> None:
    entity = await client.get_input_entity(chat_id)
    await client(UnblockRequest(id=entity))


@dataclass
class TelegramChannelStatus:
    account: str = ""
    linked: bool = False
    connected: bool = False
    own_user_id: int | None = None
    last_event_at_iso: str | None = None
    last_error: str | None = None


_status: dict[str, TelegramChannelStatus] = {account: TelegramChannelStatus(account=account) for account in ACCOUNTS}
_live_clients: dict[str, TelegramClient] = {}


def get_telegram_channel_status() -> dict[str, Any]:
    return {account: asdict(status) for account, status in _status.items()}


def get_live_client(account: str) -> TelegramClient | None:
    return _live_clients.get(account)


_ACCOUNT_LABEL = {"self": "your own", "owner": "the owner's"}


async def _start_background(workspace_dir: str, account: str, inject_proactive: Callable[[str], None] | None) -> None:
    """Runs forever once a session exists for this account. Telethon
    reconnects internally; this only returns on an unrecoverable failure
    (supervise() restarts it)."""
    session = decrypt_text_from_file(_session_path(workspace_dir, account))
    if not session:
        return
    _require_app_credentials()
    client = _new_client(session)
    await client.connect()
    status = _status[account]
    if not await client.is_user_authorized():
        status.linked = False
        log_event("plugin:telegram", "session_not_authorized", account=account)
        return
    me = await client.get_me()
    status.linked = True
    status.connected = True
    status.own_user_id = me.id
    _live_clients[account] = client

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
        status.last_event_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        log_event("plugin:telegram", "message_received", account=account, kind=kind, chat_id=event.chat_id)
        if inject_proactive is None:
            return
        note = (
            f'[New Telegram {kind} in {_ACCOUNT_LABEL[account]} Telegram account, from "{name}" '
            f'(chat_id={event.chat_id}, account="{account}"):\n{text}'
        )
        if account == "self":
            note += f'\nReply with telegram_send_message(chat="{event.chat_id}", text=..., account="self") if it needs a response.]'
        else:
            note += (
                "\nThis is the OWNER'S OWN correspondence, not yours -- read it and tell the owner about it if it "
                "matters, but do not reply as them unless they have explicitly asked you to send this specific "
                f'reply (telegram_send_message(chat="{event.chat_id}", text=..., account="owner")).]'
            )
        inject_proactive(note)

    try:
        await client.run_until_disconnected()
    finally:
        status.connected = False
        _live_clients.pop(account, None)


def start_telegram_channel(workspace_dir: str, inject_proactive: Callable[[str], None]) -> list[asyncio.Task[None]]:
    """Startup loop, one per account: if a session already exists, connect;
    otherwise re-check every 30s (cheap file read) until login_finish has
    stored one. The two accounts are fully independent -- one can be linked
    and running while the other has never logged in."""
    def _make_loop(account: str) -> Callable[[], Any]:
        async def _loop() -> None:
            while True:
                if not has_telegram_session(workspace_dir, account):
                    await asyncio.sleep(30)
                    continue
                _status[account].linked = True
                try:
                    await _start_background(workspace_dir, account, inject_proactive)
                except Exception as exc:
                    _status[account].connected = False
                    _status[account].last_error = str(exc)
                    log_event("plugin:telegram", "connection_failed", account=account, error=str(exc))
                await asyncio.sleep(5.0)
        return _loop

    return [supervise(f"telegram_channel_{account}", _make_loop(account)) for account in ACCOUNTS]
