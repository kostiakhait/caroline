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
from pathlib import Path
from typing import Any, Callable

from telethon import TelegramClient, events, utils
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import CreateChannelRequest, DeleteChannelRequest
from telethon.tl.functions.contacts import BlockRequest, DeleteContactsRequest, ImportContactsRequest, UnblockRequest
from telethon.tl.functions.messages import CreateChatRequest, DeleteChatRequest
from telethon.tl.types import Channel, Chat, InputPhoneContact

from app.logging_setup import log_event
from app.secret_store import decrypt_text_from_file, encrypt_text_to_file, messenger_secrets_dir
from app.task_supervisor import supervise
from app.workspace_dir import WORKSPACE_DIR

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


# Groups and channels (2026-10-11). Telegram has two different underlying
# entity types here -- a "basic group" (small, created with CreateChatRequest,
# deleted with DeleteChatRequest) and a "channel", which covers both
# broadcast channels and supergroups (created with CreateChannelRequest,
# megagroup=True for a supergroup; deleted with DeleteChannelRequest).
# create_group always makes a basic group (CreateChatRequest requires at
# least one other member to begin with, unlike a channel); create_channel
# always makes the channel-family entity, broadcast or supergroup. The
# high-level client.edit_admin/kick_participant/delete_dialog methods paper
# over that split for admin/kick/leave, so only delete_chat needs to look
# at which type it got (get_entity) to pick the matching delete call.
async def create_group(client: TelegramClient, title: str, user_ids: list[int]) -> int:
    users = [await client.get_input_entity(uid) for uid in user_ids]
    result = await client(CreateChatRequest(users=users, title=title))
    return result.chats[0].id


async def create_channel(client: TelegramClient, title: str, about: str = "", megagroup: bool = False) -> int:
    result = await client(CreateChannelRequest(title=title, about=about, megagroup=megagroup))
    return result.chats[0].id


async def set_admin(client: TelegramClient, chat_id: int, user_id: int, is_admin: bool, title: str | None = None) -> None:
    entity = await client.get_input_entity(chat_id)
    user = await client.get_input_entity(user_id)
    await client.edit_admin(entity, user, is_admin=is_admin, title=title)


async def remove_member(client: TelegramClient, chat_id: int, user_id: int) -> None:
    """Kicking yourself ('me') leaves the chat instead -- same as Telethon's
    own kick_participant; leave_chat below is the direct way to do that."""
    entity = await client.get_input_entity(chat_id)
    user = await client.get_input_entity(user_id)
    await client.kick_participant(entity, user)


async def delete_chat(client: TelegramClient, chat_id: int) -> None:
    """Attempts to delete the group/channel outright -- Telegram only
    honors this for its creator (or an admin with the right permission on
    a channel); otherwise it may just remove this account instead, same as
    leave_chat. There is no separate "delete" concept for a private chat."""
    entity = await client.get_entity(chat_id)
    if isinstance(entity, Channel):
        await client(DeleteChannelRequest(channel=entity))
    elif isinstance(entity, Chat):
        await client(DeleteChatRequest(chat_id=entity.id))
    else:
        raise TelegramLoginError(f"Chat {chat_id} is neither a group nor a channel -- nothing to delete.")


async def leave_chat(client: TelegramClient, chat_id: int) -> None:
    await client.delete_dialog(chat_id)


# Attachments (2026-10-11, after the deliberate choice to notify on every
# incoming media message, not just captioned ones -- see _describe_media/
# _on_new below). Telethon talks MTProto directly, so both directions move
# a local file path straight through -- no base64/HTTP hop the way email's
# and notes' attachment tools need, matching this codebase's general
# "local path in, local path out" convention for file tools.
TELEGRAM_DOWNLOADS_DIR = Path(WORKSPACE_DIR) / "telegram_downloads"


async def download_attachment(client: TelegramClient, chat_id: int, message_id: int, save_path: str | None = None) -> str:
    """Downloads a message's attached media to a local file via Telethon's
    own client.download_media. When save_path is omitted, picks a default
    name under TELEGRAM_DOWNLOADS_DIR and returns the resulting path --
    unlike a file the model is already holding (where it picks the
    destination itself), it usually has no good default for a Telegram
    attachment it has not seen yet."""
    message = await client.get_messages(chat_id, ids=message_id)
    if message is None or not message.media:
        raise TelegramLoginError(f"Message {message_id} in chat {chat_id} has no downloadable attachment.")
    if save_path:
        target: str | Path = save_path
    else:
        name = (message.file.name if message.file else None) or f"{message_id}{(message.file.ext if message.file else '') or ''}"
        TELEGRAM_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        target = TELEGRAM_DOWNLOADS_DIR / f"{chat_id}_{message_id}_{name}"
    result = await client.download_media(message, file=str(target))
    if not result:
        raise TelegramLoginError(f"Telegram returned no file for message {message_id} in chat {chat_id}.")
    return result


async def send_file(client: TelegramClient, chat_id: int, path: str, caption: str | None = None) -> None:
    if not Path(path).is_file():
        raise TelegramLoginError(f"No such local file: {path}")
    await client.send_file(chat_id, path, caption=caption)


def entity_fields(entity: Any) -> dict[str, Any]:
    """What Telegram itself says about a user, group or channel, from the
    entity alone (no extra request): ids, names, @username(s), phone, flags.
    Shared by the incoming-message notice and every tool that names people
    (2026-10-23: Caroline could not get a phone number, a user id or a
    username out of a Telegram conversation -- none of the tools returned
    them). A phone is only there when Telegram shows it to this account: the
    person is in its contacts or shares the number with everyone. Fields
    Telegram left empty are left out."""
    if entity is None:
        return {}
    fields: dict[str, Any] = {"id": utils.get_peer_id(entity)}
    usernames = [u.username for u in (getattr(entity, "usernames", None) or []) if getattr(u, "active", True)]
    username = getattr(entity, "username", None) or (usernames[0] if usernames else None)
    if isinstance(entity, (Chat, Channel)):
        fields.update({
            "kind": "channel" if getattr(entity, "broadcast", False) else "group",
            "title": getattr(entity, "title", None),
            "username": f"@{username}" if username else None,
            "membersCount": getattr(entity, "participants_count", None),
        })
    else:
        status = getattr(entity, "status", None)
        was_online = getattr(status, "was_online", None)
        fields.update({
            "kind": "bot" if getattr(entity, "bot", False) else "user",
            "firstName": getattr(entity, "first_name", None),
            "lastName": getattr(entity, "last_name", None),
            "username": f"@{username}" if username else None,
            "otherUsernames": [f"@{u}" for u in usernames if u != username] or None,
            "phone": f"+{entity.phone}" if getattr(entity, "phone", None) else None,
            "isContact": getattr(entity, "contact", None),
            "isMutualContact": getattr(entity, "mutual_contact", None),
            "verified": getattr(entity, "verified", None) or None,
            "deleted": getattr(entity, "deleted", None) or None,
            "lastSeen": was_online.isoformat() if was_online else (type(status).__name__.removeprefix("UserStatus") if status else None),
        })
    return {key: value for key, value in fields.items() if value not in (None, "", [])}


def _describe_media(event: events.NewMessage.Event) -> str | None:
    """A short human label for an incoming message's attachment, if any --
    checked specific-to-general since Telethon's event.document also
    matches video/audio/gif/voice/sticker (they're all Telegram documents
    under the hood; the narrower properties tell those apart)."""
    if event.photo:
        return "photo"
    if event.voice:
        return "voice message"
    if event.video_note:
        return "video note"
    if event.video:
        return "video"
    if event.audio:
        return "audio"
    if event.gif:
        return "GIF"
    if event.sticker:
        return "sticker"
    if event.document:
        name = getattr(event.file, "name", None) if event.file else None
        return f'file "{name}"' if name else "file"
    return None


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


async def _start_background(
    workspace_dir: str, account: str, inject_proactive: Callable[[str], None] | None, touch: Callable[[], None] = lambda: None,
) -> None:
    """Runs forever once a session exists for this account. Telethon
    reconnects internally; this only returns on an unrecoverable failure
    (supervise() restarts it).

    `touch()` is supervise()'s heartbeat callback (2026-10-11, after a real
    incident: client.connect() hung forever here once, silently, with no
    exception and no return -- the outer try/except in start_telegram_
    channel's own loop never got a chance to fire, so nothing ever noticed
    or recovered for 55+ minutes). Called once right after a successful
    connect+auth, then pumped periodically for as long as this account
    stays connected -- see the loop started just before
    run_until_disconnected() below."""
    session = decrypt_text_from_file(_session_path(workspace_dir, account))
    if not session:
        log_event("plugin:telegram", "no_session_on_disk", account=account)
        return
    _require_app_credentials()
    client = _new_client(session)
    log_event("plugin:telegram", "connect_attempt_started", account=account)
    # Belt-and-suspenders alongside supervise()'s own heartbeat watchdog
    # (which catches a hang here too, eventually) -- this makes a stuck
    # connect() raise directly, so it reaches start_telegram_channel's own
    # except Exception/connection_failed log line immediately instead of
    # waiting out a full heartbeat cycle.
    await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT_S)
    log_event("plugin:telegram", "connect_succeeded", account=account)
    status = _status[account]
    if not await asyncio.wait_for(client.is_user_authorized(), timeout=CONNECT_TIMEOUT_S):
        status.linked = False
        log_event("plugin:telegram", "session_not_authorized", account=account)
        return
    me = await asyncio.wait_for(client.get_me(), timeout=CONNECT_TIMEOUT_S)
    status.linked = True
    status.connected = True
    status.own_user_id = me.id
    _live_clients[account] = client
    log_event("plugin:telegram", "connected", account=account, user_id=me.id)
    touch()

    @client.on(events.NewMessage(incoming=True))
    async def _on_new(event: events.NewMessage.Event) -> None:
        # Private chats always; groups only when the user is actually mentioned
        # or replied to -- otherwise every group chatter would interrupt the turn.
        if not (event.is_private or event.mentioned or event.is_reply):
            return
        text = (event.raw_text or "").strip()
        media = _describe_media(event)
        # A media message with no caption used to be dropped here entirely
        # (empty raw_text) -- decision 2026-10-11: notify on it too, same as
        # a captioned one, so a bare photo/file is never silently invisible.
        if not text and not media:
            return
        sender = await event.get_sender()
        name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)])) \
            or getattr(sender, "title", None) or str(event.sender_id)
        kind = "DM" if event.is_private else "group mention"
        status.last_event_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        log_event("plugin:telegram", "message_received", account=account, kind=kind, chat_id=event.chat_id, has_media=bool(media))
        if inject_proactive is None:
            return
        # Who wrote it, as Telegram knows them -- in a group chat_id is the
        # group's, so without this the author's own id was never visible.
        who = ", ".join(f"{key}={value}" for key, value in entity_fields(sender).items() if key not in ("firstName", "lastName"))
        note = (
            f'[New Telegram {kind} in {_ACCOUNT_LABEL[account]} Telegram account, from "{name}" '
            f'(sender: {who or f"id={event.sender_id}"}; chat_id={event.chat_id}, message_id={event.id}, '
            f'account="{account}"):\n{text or "(no caption)"}'
        )
        if media:
            note += (
                f'\nAttached: {media}. Use telegram_download_attachment(chat="{event.chat_id}", '
                f'messageId={event.id}, account="{account}") to save it locally first.'
            )
        if account == "self":
            note += (
                "\nThis is Caroline's own separate Telegram account, not the user's -- replying here is "
                "Caroline's own correspondence, not something done on the user's behalf, and needs no extra "
                f'permission. Reply with telegram_send_message(chat="{event.chat_id}", text=..., account="self") '
                "if it needs a response.]"
            )
        else:
            note += (
                "\nThis is the OWNER'S OWN correspondence, not yours -- read it and tell the owner about it if it "
                "matters, but do not reply as them unless they have explicitly asked you to send this specific "
                f'reply (telegram_send_message(chat="{event.chat_id}", text=..., account="owner")).]'
            )
        inject_proactive(note)

    async def _pump_heartbeat() -> None:
        # Nothing here can directly observe Telethon's own internal health
        # once connected -- this pump just proves the surrounding coroutine
        # is still scheduled and running at all, which is what the stuck-
        # forever incident actually needed (the hang was in connect(),
        # never reaching this point in the first place).
        while True:
            touch()
            await asyncio.sleep(HEARTBEAT_PUMP_INTERVAL_S)

    heartbeat_pump = asyncio.ensure_future(_pump_heartbeat())
    try:
        await client.run_until_disconnected()
    finally:
        heartbeat_pump.cancel()
        status.connected = False
        _live_clients.pop(account, None)


# Opt-in heartbeat watchdog (task_supervisor.py's supervise(), 2026-10-11):
# generous enough that a slow-but-healthy connect (mobile network, a cold
# Telegram DC) never false-positives, short enough that a genuinely stuck
# connect() -- the real incident this fixes -- is caught and retried well
# within a human noticing by accident.
CHANNEL_HEARTBEAT_TIMEOUT_S = 60.0
HEARTBEAT_PUMP_INTERVAL_S = 10.0
# Per-step timeout for connect/is_user_authorized/get_me -- well under
# CHANNEL_HEARTBEAT_TIMEOUT_S, so a stuck step is caught and logged
# immediately by _loop's own except Exception rather than only being
# caught later by the heartbeat watchdog's coarser, silent cancellation.
CONNECT_TIMEOUT_S = 30.0


def start_telegram_channel(workspace_dir: str, inject_proactive: Callable[[str], None]) -> list[asyncio.Task[None]]:
    """Startup loop, one per account: if a session already exists, connect;
    otherwise re-check every 30s (cheap file read) until login_finish has
    stored one. The two accounts are fully independent -- one can be linked
    and running while the other has never logged in."""
    def _make_loop(account: str) -> Callable[[Callable[[], None]], Any]:
        async def _loop(touch: Callable[[], None]) -> None:
            while True:
                if not has_telegram_session(workspace_dir, account):
                    # Idly waiting for this account to ever be linked is
                    # normal, indefinite, healthy -- not a stall.
                    touch()
                    await asyncio.sleep(30)
                    continue
                _status[account].linked = True
                try:
                    await _start_background(workspace_dir, account, inject_proactive, touch)
                except Exception as exc:
                    _status[account].connected = False
                    _status[account].last_error = str(exc)
                    log_event("plugin:telegram", "connection_failed", account=account, error=str(exc))
                touch()
                await asyncio.sleep(5.0)
        return _loop

    return [
        supervise(f"telegram_channel_{account}", _make_loop(account), heartbeat_timeout_s=CHANNEL_HEARTBEAT_TIMEOUT_S)
        for account in ACCOUNTS
    ]
