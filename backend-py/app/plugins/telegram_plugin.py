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

Contact management (2026-10-11, after a real cold-outreach failure: sending
to a bare phone number the account had never talked to before was refused
by Telegram -- the same rule every Telegram app enforces, that an account
may only message someone already "known" to it). telegram_add_contact adds
someone by phone number first (what the ordinary app does automatically
when you add a number before messaging it); telegram_send_message now also
resolves a phone-number-looking `chat` the same way on its own, so the
common case is one call, not two. telegram_delete_contact,
telegram_block_user and telegram_unblock_user round out ordinary contact
management.

Usernames (2026-10-16, after a real incident: told to message six people by
username, Caroline reported no way to do that directly and asked for phone
numbers to add them as contacts first -- a real gap in the code, not an
actual Telegram restriction). `chat`/`user` accepts a Telegram @username
directly, resolved with no contact-adding step at all: unlike a phone
number, username lookup is NOT subject to the "must already be known"
rule -- the same way opening someone's profile by @handle needs no prior
contact in any Telegram client.
"""

from __future__ import annotations

import re
from typing import Any

from app.telegram_channel import (
    ACCOUNTS,
    DEFAULT_ACCOUNT,
    TelegramLoginError,
    app_configured,
    block_user,
    create_channel,
    create_group,
    delete_chat,
    delete_contact,
    download_attachment,
    get_live_client,
    is_valid_account,
    leave_chat,
    login_finish,
    login_start,
    remove_member,
    resolve_or_add_contact,
    send_file,
    set_admin,
    unblock_user,
)
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, Chat

from app.logging_setup import log_event
from app.telegram_channel import entity_fields
from app.workspace_dir import WORKSPACE_DIR
from app.context_cap import cap_batch, cap_count, skipped_note, truncate
from app.plugins.loader import Plugin, PluginTool
from app.plugins.messenger_search import find_by_name


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

_PHONE = re.compile(r"^\+?[\d][\d \-()]{5,}$")
# Telegram's own username rules: 5-32 chars, starts with a letter, letters/
# digits/underscores after that. The leading "@" people naturally type is
# optional here and stripped before the actual resolve call.
_USERNAME = re.compile(r"^@?[a-zA-Z][a-zA-Z0-9_]{4,31}$")


def _looks_like_phone(raw: str) -> bool:
    # A bare chat id (from telegram_list_chats/telegram_add_contact) is a
    # plain base-10 integer, never with a "+" or separators -- int("+123")
    # parses fine in Python, so a leading "+" or any phone-style separator
    # is what actually distinguishes "+420 792 429649" from a real chat id.
    return raw.startswith("+") or any(c in raw for c in " -()")


def _looks_like_username(raw: str) -> bool:
    return bool(_USERNAME.match(raw))


def _not_connected(account: str) -> str:
    return f'The "{account}" Telegram account isn\'t connected yet -- use telegram_login_start/telegram_login_finish with account="{account}" first.'


def _account_or_error(args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    account = str(args.get("account") or DEFAULT_ACCOUNT).strip()
    if not is_valid_account(account):
        return account, {"text": f'"account" must be one of {", ".join(ACCOUNTS)}, not {account!r}.', "is_error": True}
    return account, None


def _display_id(resolved: Any) -> Any:
    """_resolve_chat may now return a full Telethon entity (the username
    path, see its own docstring) instead of a bare int -- this is what
    every f-string below shows the model, so a resolved entity's own
    .id prints the same plain number a numeric chat id always did,
    instead of the entity object's verbose repr."""
    return getattr(resolved, "id", resolved)


def _not_ready(account: str) -> dict[str, Any] | None:
    if not app_configured():
        return {"text": NOT_AVAILABLE, "is_error": True}
    if get_live_client(account) is None:
        return {"text": _not_connected(account), "is_error": True}
    return None


async def _resolve_chat(client: Any, raw: str) -> tuple[int | Any | None, dict[str, Any] | None]:
    """A numeric chat id as given; a Telegram @username resolved directly
    (no contact-adding needed -- unlike a phone number, Telegram's username
    lookup (contacts.ResolveUsername) has no "must already know this person"
    restriction, the same way opening someone's profile by @handle in the
    app itself needs no prior contact); or -- when `raw` looks like a phone
    number instead -- resolved by adding the contact first, the same way
    telegram_add_contact does (phone-number lookup IS subject to Telegram's
    "may only message someone already known" rule and to the owner's privacy
    settings). (chat_id, None) on success, or (None, error envelope).

    Per explicit instruction (2026-10-16), after a real incident: told to
    message six people by username, Caroline reported she had "no tool to
    write directly by username" and needed phone numbers to add them as
    contacts first -- true of the code as it stood (only numeric id and
    phone number were ever resolved here), but not a real Telegram
    limitation; this adds the missing, always-available username path."""
    raw = raw.strip()
    if not _looks_like_phone(raw):
        try:
            return int(raw), None
        except ValueError:
            pass
    if _looks_like_username(raw):
        try:
            entity = await client.get_entity(raw.lstrip("@"))
        except Exception as exc:
            return None, {
                "text": f'Telegram found no account at username "{raw}" (or it could not be resolved right now): {exc}',
                "is_error": True,
            }
        # Per explicit instruction (2026-10-16), a real incident just after
        # the fix above shipped: returning only entity.id (a bare int) and
        # letting callers pass that back into send_message/send_file/etc.
        # made the FIRST send to a just-resolved username fail with a raw
        # OSError (Errno 22, "Invalid argument") -- a bare id forces
        # Telethon to re-resolve it from its own entity cache a second
        # time, a round trip the id-only numeric-chat-id path never needed
        # (that id always came from an existing dialog, already fully
        # cached with its access hash). Returning the entity object
        # get_entity already fully resolved removes that whole second
        # lookup -- every Telethon call this feeds into (send_message,
        # send_file, block/unblock, ...) accepts a resolved entity directly,
        # same as it accepts a bare int.
        return entity, None
    if not _PHONE.match(raw):
        return None, {"text": "chat must be the numeric chat id from telegram_list_chats/telegram_add_contact or a proactive notification, a Telegram @username, or a phone number to message cold.", "is_error": True}
    user_id, result = await resolve_or_add_contact(client, raw)
    if user_id is None:
        return None, {"text": result, "is_error": True}
    return user_id, None


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
    limit = min(int(args.get("limit") or 50), 500)
    dialogs = await client.get_dialogs(limit=limit)
    return {"text": str([_dialog_row(d) for d in dialogs])}


def _dialog_row(dialog: Any) -> dict[str, Any]:
    """One chat of telegram_list_chats/telegram_find_chat: its id, name and
    kind, plus the @username and phone Telegram shows for it (see
    telegram_channel.entity_fields) -- 2026-10-23, the list used to carry
    neither."""
    row = {"id": dialog.id, "name": dialog.name, "isGroup": dialog.is_group, "isChannel": dialog.is_channel, "unread": dialog.unread_count}
    fields = entity_fields(dialog.entity)
    for key in ("username", "phone"):
        if fields.get(key):
            row[key] = fields[key]
    return row


async def telegram_find_chat(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    query = str(args["query"]).strip()
    if not query:
        return {"text": 'telegram_find_chat needs "query".', "is_error": True}
    client = get_live_client(account)
    # A much higher cap than telegram_list_chats' own default (50) -- this
    # tool exists specifically so a chat far down the dialog list is still
    # found, not just whatever happens to be most recently active.
    limit = min(int(args.get("limit") or 500), 2000)
    dialogs = await client.get_dialogs(limit=limit)
    rows = [_dialog_row(d) for d in dialogs]
    matches = await find_by_name(query, rows)
    matches, _cut = cap_count(matches)
    return {"text": str(matches)}


async def telegram_add_contact(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    phone = str(args.get("phone") or "").strip()
    if not phone:
        return {"text": 'telegram_add_contact needs "phone".', "is_error": True}
    client = get_live_client(account)
    user_id, result = await resolve_or_add_contact(client, phone, args.get("name"))
    if user_id is None:
        return {"text": result, "is_error": True}
    return {"text": f'Added "{result}" ({phone}) to the "{account}" account\'s Telegram contacts -- chat id {user_id}.'}


async def telegram_send_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    client = get_live_client(account)
    chat_id, error = await _resolve_chat(client, str(args["chat"]))
    if error:
        return error
    text = str(args["text"])
    await client.send_message(chat_id, text)
    return {"text": f'Sent to {_display_id(chat_id)} (account="{account}").'}


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
    # No query: the latest messages of the chat (Telegram's own search with
    # an empty string), so a conversation can be read without guessing a word in it.
    query = str(args.get("query") or "").strip()
    # Bug fix (2026-10-16), confirmed live (full-plugin context-overflow audit, same incident
    # as email_batch_plugin.py's own fix): `limit` had no hard ceiling, and every matched
    # message's FULL raw_text went back with no truncation and no total-char budget -- the
    # same "many full bodies, one call" shape as the email-batch incident.
    limit = min(int(args.get("limit") or 20), 200)
    rows = []
    async for m in client.iter_messages(chat_id, search=query or None, limit=limit):
        text, _note = truncate(m.raw_text or "")
        # Who wrote each message (2026-10-23): in a group the rows never said.
        sender = entity_fields(getattr(m, "sender", None)) or {"id": m.sender_id}
        rows.append({"id": m.id, "date": m.date.isoformat() if m.date else None, "from": sender, "text": text})
    included, skipped = cap_batch(rows, lambda r: r["text"], lambda r: str(r["id"]))
    lines = [str(included)]
    if skipped:
        lines.append(skipped_note(skipped, 200_000, "Narrow the query or search a smaller limit to see the rest in full."))
    return {"text": "\n\n".join(lines)}


async def _entity_for_lookup(client: Any, raw: str) -> tuple[Any, dict[str, Any] | None]:
    """The entity named by a numeric id, an @username or a phone number, for
    reading only: unlike _resolve_chat, a phone number is NOT added to the
    contacts -- looking someone up must change nothing. Telegram resolves a
    phone only for a contact of this account."""
    raw = raw.strip()
    target: Any = raw
    if not _looks_like_phone(raw):
        try:
            target = int(raw)
        except ValueError:
            target = raw.lstrip("@") if _looks_like_username(raw) else raw
    try:
        return await client.get_entity(target), None
    except Exception as exc:
        hint = (" A phone number is only found for someone in this account's contacts -- telegram_add_contact adds "
                "them, if that is what you want." if _PHONE.match(raw) else "")
        return None, {"text": f'Telegram could not find "{raw}": {exc}.{hint}', "is_error": True}


async def telegram_get_info(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    """Everything Telegram tells this account about one person, group or
    channel: entity_fields plus the profile's own "about" text and, for a
    person, the number of chats in common; for a group or channel, its
    description and member count."""
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    client = get_live_client(account)
    entity, error = await _entity_for_lookup(client, str(args.get("who") or ""))
    if error:
        return error
    info = entity_fields(entity)
    try:
        if isinstance(entity, Channel):
            full = (await client(GetFullChannelRequest(entity))).full_chat
            info.update({"about": full.about or None, "membersCount": full.participants_count})
        elif isinstance(entity, Chat):
            full = (await client(GetFullChatRequest(entity.id))).full_chat
            info.update({"about": full.about or None})
        else:
            full = (await client(GetFullUserRequest(entity))).full_user
            info.update({"about": full.about or None, "commonChatsCount": full.common_chats_count})
    except Exception as exc:  # the basic fields above are still a real answer
        info["fullProfileError"] = str(exc)
    log_event("plugin:telegram", "get_info", account=account, kind=info.get("kind"))
    return {"text": str({key: value for key, value in info.items() if value not in (None, "")})}


async def telegram_list_members(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    """The members of a group (or of a channel, when this account is allowed
    to see them), each with entity_fields: id, names, @username, phone."""
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    client = get_live_client(account)
    entity, error = await _entity_for_lookup(client, str(args.get("chat") or ""))
    if error:
        return error
    rows = []
    try:
        async for user in client.iter_participants(entity):
            rows.append(entity_fields(user))
    except Exception as exc:
        return {"text": f"Telegram did not give the member list of this chat: {exc}", "is_error": True}
    log_event("plugin:telegram", "list_members", account=account, count=len(rows))
    included, skipped = cap_batch(rows, lambda r: str(r), lambda r: str(r.get("id")))
    lines = [f"{len(rows)} member(s): {included}"]
    if skipped:
        lines.append(skipped_note(skipped, 200_000, "Look up the ones you need by name with telegram_get_info."))
    return {"text": "\n\n".join(lines)}


async def telegram_delete_contact(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
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
        return {"text": "chat must be the numeric chat id from telegram_list_chats or telegram_add_contact.", "is_error": True}
    await delete_contact(client, chat_id)
    return {"text": f'Removed {chat_id} from the "{account}" account\'s Telegram contacts. Chat history and block status are unchanged.'}


async def telegram_block_user(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
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
        return {"text": "chat must be the numeric chat id from telegram_list_chats or telegram_add_contact.", "is_error": True}
    await block_user(client, chat_id)
    return {"text": f'Blocked {chat_id} on the "{account}" account.'}


async def telegram_unblock_user(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
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
        return {"text": "chat must be the numeric chat id from telegram_list_chats or telegram_add_contact.", "is_error": True}
    await unblock_user(client, chat_id)
    return {"text": f'Unblocked {chat_id} on the "{account}" account.'}


async def telegram_download_attachment(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    client = get_live_client(account)
    try:
        chat_id = int(args["chat"])
        message_id = int(args["messageId"])
    except (TypeError, ValueError, KeyError):
        return {"text": "telegram_download_attachment needs the numeric chat and messageId from a proactive Telegram notification.", "is_error": True}
    save_path = str(args.get("savePath") or "").strip() or None
    try:
        path = await download_attachment(client, chat_id, message_id, save_path)
    except TelegramLoginError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Saved to {path}."}


async def telegram_send_file(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    client = get_live_client(account)
    chat_id, error = await _resolve_chat(client, str(args["chat"]))
    if error:
        return error
    path = str(args.get("path") or "").strip()
    if not path:
        return {"text": 'telegram_send_file needs "path".', "is_error": True}
    try:
        await send_file(client, chat_id, path, args.get("caption"))
    except TelegramLoginError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f'Sent {path} to {_display_id(chat_id)} (account="{account}").'}


async def telegram_create_group(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    title = str(args.get("title") or "").strip()
    if not title:
        return {"text": 'telegram_create_group needs "title".', "is_error": True}
    members = [str(m) for m in (args.get("members") or [])]
    if not members:
        return {"text": 'telegram_create_group needs at least one "members" entry (a chat id or phone number) -- a basic group cannot be created with just yourself in it.', "is_error": True}
    client = get_live_client(account)
    user_ids = []
    for raw in members:
        user_id, error = await _resolve_chat(client, raw)
        if error:
            return error
        user_ids.append(user_id)
    chat_id = await create_group(client, title, user_ids)
    return {"text": f'Created group "{title}" (chat id {chat_id}) on the "{account}" account.'}


async def telegram_create_channel(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    title = str(args.get("title") or "").strip()
    if not title:
        return {"text": 'telegram_create_channel needs "title".', "is_error": True}
    client = get_live_client(account)
    is_megagroup = bool(args.get("isMegagroup"))
    chat_id = await create_channel(client, title, str(args.get("about") or ""), is_megagroup)
    kind = "supergroup" if is_megagroup else "broadcast channel"
    return {"text": f'Created {kind} "{title}" (chat id {chat_id}) on the "{account}" account.'}


async def telegram_set_admin(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    client = get_live_client(account)
    try:
        chat_id = int(args["chat"])
    except (TypeError, ValueError, KeyError):
        return {"text": "chat must be the numeric chat id of the group/channel.", "is_error": True}
    user_id, error = await _resolve_chat(client, str(args.get("user") or ""))
    if error:
        return error
    is_admin = bool(args.get("isAdmin"))
    try:
        await set_admin(client, chat_id, user_id, is_admin, args.get("title"))
    except TelegramLoginError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f'{"Promoted" if is_admin else "Demoted"} {_display_id(user_id)} in {chat_id} (account="{account}").'}


async def telegram_remove_member(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    client = get_live_client(account)
    try:
        chat_id = int(args["chat"])
    except (TypeError, ValueError, KeyError):
        return {"text": "chat must be the numeric chat id of the group/channel.", "is_error": True}
    user_id, error = await _resolve_chat(client, str(args.get("user") or ""))
    if error:
        return error
    await remove_member(client, chat_id, user_id)
    return {"text": f'Removed {_display_id(user_id)} from {chat_id} (account="{account}").'}


async def telegram_delete_chat(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    client = get_live_client(account)
    try:
        chat_id = int(args["chat"])
    except (TypeError, ValueError, KeyError):
        return {"text": "chat must be the numeric chat id of the group/channel.", "is_error": True}
    try:
        await delete_chat(client, chat_id)
    except TelegramLoginError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f'Deletion of {chat_id} requested (account="{account}") -- Telegram only applies it fully if this account created the chat or has the right admin permission; otherwise this account was likely just removed from it instead.'}


async def telegram_leave_chat(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    not_ready = _not_ready(account)
    if not_ready:
        return not_ready
    client = get_live_client(account)
    try:
        chat_id = int(args["chat"])
    except (TypeError, ValueError, KeyError):
        return {"text": "chat must be the numeric chat id of the group/channel.", "is_error": True}
    await leave_chat(client, chat_id)
    return {"text": f'Left {chat_id} (account="{account}").'}


def _usage_instructions() -> str:
    return (
        "TWO separate Telegram accounts, chosen with \"account\" on every tool: \"self\" (the default -- your "
        "OWN Telegram account, your own DMs and groups, yours to read and answer freely on your own initiative, "
        "same as any other tool) and \"owner\" (the human owner's personal Telegram account -- you may read it "
        "and tell them what's in it, but see telegram_send_message's own description before ever sending "
        "anything there). Login is conversational and always names the account: "
        "telegram_login_start(phone, account) sends a code via Telegram itself, then "
        "telegram_login_finish(code, account) completes it (or asks for a 2FA password if the account has one). "
        "`chat` everywhere is the numeric chat id from telegram_list_chats/telegram_add_contact or a proactive "
        "new-message notification, never a guessed name. telegram_search_messages is per-chat only -- Telethon "
        "has no single-call search across every chat the way Slack's search.messages does.\n"
        "Messaging someone cold: Telegram only lets an account message someone already \"known\" to it (a "
        "contact, or an existing chat) -- a bare phone number it has never seen is refused. "
        "telegram_send_message resolves a phone number given as `chat` on its own (adding the contact if "
        "needed), so for a first message you can usually just pass the phone number directly; use "
        "telegram_add_contact first only when you want the contact added without sending anything yet, or to "
        "learn the chat id ahead of time. Either way, when Telegram has no account reachable at that number "
        "(not registered, or hidden by privacy settings -- these look the same from here), tell the user plainly "
        "rather than retrying.\n"
        "Attachments: a proactive notification for an incoming photo/video/document/voice message says so "
        "(\"Attached: ...\") and gives its messageId -- download it with telegram_download_attachment(chat, "
        "messageId, account) before using it; omit savePath to let it pick a default location and return the "
        "resulting path. telegram_send_file(chat, path, caption, account) sends a local file the same way "
        "telegram_send_message sends text, including the same phone-number auto-resolve and the same "
        "owner-account permission rule.\n"
        "Groups and channels: telegram_create_group(title, members, account) makes a small basic group -- "
        "`members` needs at least one other chat id/phone number, it cannot be created with just yourself in "
        "it. telegram_create_channel(title, about, isMegagroup, account) makes a broadcast channel "
        "(isMegagroup=false) or a supergroup (isMegagroup=true) instead -- no initial members needed. "
        "telegram_set_admin(chat, user, isAdmin, account) promotes/demotes a member (`user` may be a phone "
        "number too). telegram_remove_member(chat, user, account) kicks someone. telegram_delete_chat(chat, "
        "account) asks Telegram to delete the whole group/channel, but it only actually does that for the "
        "creator (or an admin with the right permission on a channel) -- otherwise this account is just "
        "removed from it, same as telegram_leave_chat(chat, account), which always just leaves."
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
            "telegram_find_chat",
            "Finds the chat(s) matching a name/description, by MEANING (transliteration, diminutives, typos, "
            "word order) -- never exact text. Use this instead of telegram_list_chats whenever you're looking "
            "for one specific person/group rather than browsing: a near-miss spelling, a nickname, or a chat "
            "further down the list than telegram_list_chats' own default would still be found. Returns the "
            "same row shape as telegram_list_chats, only the ones that actually match -- possibly empty if "
            "genuinely nothing fits. `account`: \"self\" (default) or \"owner\".",
            {"query": str, "limit": int | None, "account": str | None}, telegram_find_chat,
        ),
        PluginTool(
            "telegram_add_contact",
            "Adds someone to a Telegram account's contacts by phone number -- what's needed before messaging "
            "someone who has never talked to this account (see this plugin's own usage_instructions). Optional "
            "`name` labels the contact; Telegram's own match on the number is what matters. Returns the "
            "resulting chat id, or says plainly when Telegram has no account reachable at that number. "
            "`account`: \"self\" (default) or \"owner\". telegram_send_message does this on its own when given "
            "a phone number directly, so call this separately mainly when you want the contact added first "
            "without sending anything yet.",
            {"phone": str, "name": str | None, "account": str | None}, telegram_add_contact,
        ),
        PluginTool(
            "telegram_send_message",
            "Sends a Telegram message. `account` (default \"self\") picks which account it is sent FROM: "
            "\"self\" sends as you, your own account, your own call to make like any other tool. \"owner\" "
            "sends AS THE OWNER, from their own personal account -- use this ONLY when the owner has explicitly "
            "asked you, for this specific message, to send it from their account; never on your own initiative, "
            "never because a past approval or a similar earlier message makes it seem fine. If in doubt, ask "
            "them first rather than send as them. `chat` is the exact numeric chat id, a Telegram @username "
            "(resolved directly, no contact-adding needed), OR a phone number for a first message to someone "
            "this account has never talked to -- resolved (and added to contacts) automatically.",
            {"chat": str, "text": str, "account": str | None}, telegram_send_message,
        ),
        PluginTool(
            "telegram_search_messages",
            "Searches one Telegram chat's own messages for `query`; without `query`, returns the chat's latest "
            "messages. Every message comes with who wrote it (`from`: id, name, @username, phone when Telegram "
            "shows it). `chat` is required (the numeric chat id) -- see this plugin's own usage_instructions for "
            "why there's no cross-chat search. `account`: \"self\" (default) or \"owner\".",
            {"chat": str, "query": str | None, "limit": int | None, "account": str | None}, telegram_search_messages,
        ),
        PluginTool(
            "telegram_get_info",
            "Everything Telegram tells this account about one person, group or channel: numeric id, first and "
            "last name, @username(s), phone number, whether they are a contact, a bot, last seen, the profile's "
            "own \"about\" text, chats in common; for a group or channel its title, @username, description and "
            "member count. `who` is a numeric id (a chat id, or a sender id from a notification or a message's "
            "`from`), an @username, or a phone number of someone in this account's contacts. A phone number is "
            "only there when Telegram shows it to this account (a contact, or someone sharing it with everyone). "
            "Reads only, changes nothing. `account`: \"self\" (default) or \"owner\".",
            {"who": str, "account": str | None}, telegram_get_info,
        ),
        PluginTool(
            "telegram_list_members",
            "Lists the members of a group (or of a channel, when this account may see them), each with numeric "
            "id, names, @username and phone when Telegram shows it. `chat` is the group's numeric id or "
            "@username. `account`: \"self\" (default) or \"owner\".",
            {"chat": str, "account": str | None}, telegram_list_members,
        ),
        PluginTool(
            "telegram_delete_contact",
            "Removes someone from a Telegram account's contacts (their chat history and block status are "
            "unchanged). `chat` is their numeric chat id. `account`: \"self\" (default) or \"owner\".",
            {"chat": str, "account": str | None}, telegram_delete_contact,
        ),
        PluginTool(
            "telegram_block_user",
            "Blocks someone on a Telegram account -- they can no longer message or call this account. `chat` "
            "is their numeric chat id. `account`: \"self\" (default) or \"owner\" -- blocking on the owner's "
            "own account is a real decision about who can reach them; only do it when they have asked for it.",
            {"chat": str, "account": str | None}, telegram_block_user,
        ),
        PluginTool(
            "telegram_unblock_user",
            "Reverses telegram_block_user. `chat` is their numeric chat id. `account`: \"self\" (default) or "
            "\"owner\".",
            {"chat": str, "account": str | None}, telegram_unblock_user,
        ),
        PluginTool(
            "telegram_download_attachment",
            "Downloads a Telegram message's attached media (photo/video/document/voice/...) to a local file. "
            "`chat` and `messageId` come from a proactive Telegram notification (\"Attached: ...\"). `savePath` "
            "is optional -- omit it to save under a default Caroline folder and get the resulting path back; "
            "give it only when you want a specific destination. `account`: \"self\" (default) or \"owner\".",
            {"chat": str, "messageId": int, "savePath": str | None, "account": str | None}, telegram_download_attachment,
        ),
        PluginTool(
            "telegram_send_file",
            "Sends a local file as a Telegram attachment (photo/video/document/voice -- Telegram infers the "
            "right kind from the file itself). `chat` is the numeric chat id, or a phone number for a first "
            "message (resolved the same way telegram_send_message does). Optional `caption`. `account` "
            "(default \"self\") picks which account sends it -- same owner-impersonation rule as "
            "telegram_send_message: only send as \"owner\" when explicitly asked, for this specific message.",
            {"chat": str, "path": str, "caption": str | None, "account": str | None}, telegram_send_file,
        ),
        PluginTool(
            "telegram_create_group",
            "Creates a basic Telegram group chat. `members` is a list of at least one chat id or phone number "
            "to start it with -- a group cannot be created with just yourself. `account`: \"self\" (default) "
            "or \"owner\".",
            {"title": str, "members": list, "account": str | None}, telegram_create_group,
        ),
        PluginTool(
            "telegram_create_channel",
            "Creates a Telegram channel: a broadcast channel (isMegagroup=false, the default) or a supergroup "
            "(isMegagroup=true). Optional `about`. No initial members, unlike telegram_create_group. `account`: "
            "\"self\" (default) or \"owner\".",
            {"title": str, "about": str | None, "isMegagroup": bool | None, "account": str | None}, telegram_create_channel,
        ),
        PluginTool(
            "telegram_set_admin",
            "Promotes (isAdmin=true) or demotes (isAdmin=false) someone in a group/channel. `chat` is the "
            "group/channel's numeric id; `user` is their chat id or phone number. Optional `title` sets their "
            "custom admin rank label. Only works if this account itself has the right to grant admin there. "
            "`account`: \"self\" (default) or \"owner\".",
            {"chat": str, "user": str, "isAdmin": bool, "title": str | None, "account": str | None}, telegram_set_admin,
        ),
        PluginTool(
            "telegram_remove_member",
            "Removes (kicks) someone from a group/channel. `chat` is the group/channel's numeric id; `user` is "
            "their chat id or phone number. `account`: \"self\" (default) or \"owner\".",
            {"chat": str, "user": str, "account": str | None}, telegram_remove_member,
        ),
        PluginTool(
            "telegram_delete_chat",
            "Asks Telegram to delete a group/channel outright. `chat` is its numeric id. Telegram only honors "
            "this fully for the chat's creator (or an admin with the right permission on a channel) -- "
            "otherwise this account is simply removed from it instead, the same as telegram_leave_chat. "
            "`account`: \"self\" (default) or \"owner\".",
            {"chat": str, "account": str | None}, telegram_delete_chat,
        ),
        PluginTool(
            "telegram_leave_chat",
            "Leaves a group or channel (does not delete it). `chat` is its numeric id. `account`: \"self\" "
            "(default) or \"owner\".",
            {"chat": str, "account": str | None}, telegram_leave_chat,
        ),
    ],
)
