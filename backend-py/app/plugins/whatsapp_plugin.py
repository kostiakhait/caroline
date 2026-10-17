"""whatsapp -- LLM-facing tools over app/whatsapp_channel.py's WhatsApp
connections. Fourth of the planned messenger integrations
(docs/MESSENGER_INTEGRATIONS_PLAN.md, 2026-10-06).

TWO accounts, same split as Telegram's (2026-10-11): "self" (default) is
Caroline's own WhatsApp account -- she may read and reply there as
herself, no extra permission needed; "owner" is the human owner's own
personal WhatsApp account -- read freely, but only send AS the owner when
they have explicitly asked for that specific message. Acts via an
unofficial protocol library (Baileys, Node sidecar) either way -- there is
no official API for a personal account. See whatsapp_channel.py's own doc
comment for the real, stated risk (WhatsApp could restrict/ban an account
at their discretion) and for why "self" needs an actual second phone
number to link, same prerequisite class as Telegram's own app registration.

Contacts, attachments, groups (2026-10-11), matching the Telegram plugin's
surface where WhatsApp's own protocol actually has an equivalent --  it
doesn't always: WhatsApp has no server-side contact list to add/delete
(contacts live in the phone, not on WhatsApp's servers), so there's no
whatsapp_add_contact; whatsapp_check_number/whatsapp_block_user/
whatsapp_unblock_user are the real analogs instead. WhatsApp also has no
true group deletion -- whatsapp_delete_group approximates it (see its own
description) rather than claiming something WhatsApp can't actually do.
"""

from __future__ import annotations

from typing import Any

from app.whatsapp_channel import (
    ACCOUNTS,
    DEFAULT_ACCOUNT,
    WhatsAppError,
    check_number,
    create_group,
    delete_group,
    download_media,
    is_valid_account,
    leave_group,
    list_chats,
    send_file,
    send_message,
    set_block,
    start_login,
    update_group_participants,
)
from app.workspace_dir import WORKSPACE_DIR
from app.context_cap import cap_count
from app.plugins.loader import Plugin, PluginTool
from app.plugins.messenger_search import find_by_name


def _to_jid(raw: str) -> str:
    """Unlike Telegram, WhatsApp needs no prior contact/dialog to message a
    number cold -- but Baileys still needs the full jid, not a bare phone
    number, to send to. A plain number (no "@") is turned into the
    standard individual jid form; anything already containing "@" (an
    individual jid, or a group's "...@g.us") is passed through unchanged."""
    raw = raw.strip()
    if "@" in raw:
        return raw
    digits = "".join(c for c in raw if c.isdigit())
    return f"{digits}@s.whatsapp.net"


def _account_or_error(args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    account = str(args.get("account") or DEFAULT_ACCOUNT).strip()
    if not is_valid_account(account):
        return account, {"text": f'"account" must be one of {", ".join(ACCOUNTS)}, not {account!r}.', "is_error": True}
    return account, None


async def whatsapp_login(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    try:
        message = await start_login(WORKSPACE_DIR, account)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": message}


async def whatsapp_list_chats(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    try:
        chats = await list_chats(account)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    chats, _cut = cap_count(chats)
    return {"text": str(chats)}


async def whatsapp_find_chat(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    query = str(args["query"]).strip()
    if not query:
        return {"text": 'whatsapp_find_chat needs "query".', "is_error": True}
    try:
        chats = await list_chats(account)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    matches = await find_by_name(query, chats)
    matches, _cut = cap_count(matches)
    return {"text": str(matches)}


async def whatsapp_send_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    to = _to_jid(str(args["to"]))
    text = str(args["text"])
    try:
        await send_message(account, to, text)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Sent to {to}."}


async def whatsapp_check_number(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    phone = "".join(c for c in str(args.get("phone") or "") if c.isdigit())
    if not phone:
        return {"text": 'whatsapp_check_number needs "phone".', "is_error": True}
    try:
        exists, jid = await check_number(account, phone)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    if not exists:
        return {"text": f"{phone} is not on WhatsApp (or has hidden itself from this check)."}
    return {"text": f"{phone} is on WhatsApp, jid {jid}."}


async def whatsapp_block_user(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    raw = str(args.get("jid") or "").strip()
    if not raw:
        return {"text": 'whatsapp_block_user needs "jid".', "is_error": True}
    jid = _to_jid(raw)
    try:
        await set_block(account, jid, True)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Blocked {jid}."}


async def whatsapp_unblock_user(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    raw = str(args.get("jid") or "").strip()
    if not raw:
        return {"text": 'whatsapp_unblock_user needs "jid".', "is_error": True}
    jid = _to_jid(raw)
    try:
        await set_block(account, jid, False)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Unblocked {jid}."}


async def whatsapp_download_attachment(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    ref = str(args.get("ref") or "").strip()
    if not ref:
        return {"text": 'whatsapp_download_attachment needs "ref" from a proactive WhatsApp notification.', "is_error": True}
    save_path = str(args.get("savePath") or "").strip() or None
    try:
        path = await download_media(account, ref, save_path)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Saved to {path}."}


async def whatsapp_send_file(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    raw = str(args.get("to") or "").strip()
    path = str(args.get("path") or "").strip()
    if not raw or not path:
        return {"text": 'whatsapp_send_file needs "to" and "path".', "is_error": True}
    to = _to_jid(raw)
    try:
        await send_file(account, to, path, args.get("caption"))
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Sent {path} to {to}."}


async def whatsapp_create_group(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    subject = str(args.get("subject") or "").strip()
    participants = [_to_jid(str(p)) for p in (args.get("participants") or [])]
    if not subject:
        return {"text": 'whatsapp_create_group needs "subject".', "is_error": True}
    if not participants:
        return {"text": 'whatsapp_create_group needs at least one "participants" jid or phone number.', "is_error": True}
    try:
        jid = await create_group(account, subject, participants)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f'Created group "{subject}" (jid {jid}).'}


async def _group_participants_tool(args: dict[str, Any], action: str, verb: str) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    jid = str(args.get("jid") or "").strip()
    participants = [_to_jid(str(p)) for p in (args.get("participants") or [])]
    if not jid or not participants:
        return {"text": 'needs "jid" and at least one "participants" entry.', "is_error": True}
    try:
        await update_group_participants(account, jid, participants, action)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"{verb} {len(participants)} participant(s) in {jid}."}


async def whatsapp_add_members(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return await _group_participants_tool(args, "add", "Added")


async def whatsapp_remove_members(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return await _group_participants_tool(args, "remove", "Removed")


async def whatsapp_promote_members(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return await _group_participants_tool(args, "promote", "Promoted")


async def whatsapp_demote_members(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return await _group_participants_tool(args, "demote", "Demoted")


async def whatsapp_leave_group(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    jid = str(args.get("jid") or "").strip()
    if not jid:
        return {"text": 'whatsapp_leave_group needs "jid".', "is_error": True}
    try:
        await leave_group(account, jid)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Left {jid}."}


async def whatsapp_delete_group(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    jid = str(args.get("jid") or "").strip()
    if not jid:
        return {"text": 'whatsapp_delete_group needs "jid".', "is_error": True}
    try:
        await delete_group(account, jid)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"{jid} emptied and left -- WhatsApp has no true group delete, so every other participant this account could remove was removed first, then this account left too."}


def _usage_instructions() -> str:
    return (
        "Two accounts, same split as Telegram's: `account=\"self\"` (default) is Caroline's own WhatsApp "
        "account -- read/reply there freely, no extra permission needed. `account=\"owner\"` is the human "
        "owner's own personal WhatsApp (their real chats) -- read freely, but only send AS the owner when "
        "they have explicitly asked you to send that specific message; an unofficial protocol library "
        "(Baileys) either way, since there is no official API for a personal WhatsApp account -- WhatsApp "
        "could in principle restrict or ban the account for it at their own discretion. `to`/`jid`/"
        "`participants` everywhere accept either the exact WhatsApp jid (from whatsapp_list_chats, a "
        "proactive notification, or a group's own jid) or a plain phone number -- converted to a jid "
        "automatically, never a guessed name. whatsapp_login(account=...) is one-time setup per account (a QR "
        "code image opens automatically to scan) -- call it only when the user has explicitly asked to "
        "connect that account, and make sure they understand the risk stated above before doing so, never on "
        "your own initiative. whatsapp_find_chat(query) finds a chat by name/description, judged by meaning -- "
        "use it instead of whatsapp_list_chats when looking for one specific chat; both only ever see chats "
        "already seen since that account's connection came up (Baileys keeps no history by default).\n"
        "Unlike Telegram, WhatsApp needs no prior contact/dialog to message a number cold -- any registered "
        "number can be messaged directly, no add-contact step. whatsapp_check_number is purely an optional "
        "preflight (is this number even on WhatsApp); whatsapp_block_user/whatsapp_unblock_user are the real "
        "contact-management analogs here, since WhatsApp has no server-side contact list to add/delete.\n"
        "Attachments: a proactive notification for an incoming photo/video/document/voice message says so "
        "(\"media\": ...) and gives a \"ref\" -- download it with whatsapp_download_attachment(ref) before "
        "using it (omit savePath for a default location); unlike Telegram, that ref only works for a short "
        "while after the message arrives (WhatsApp gives no way to re-fetch an old message later). "
        "whatsapp_send_file(to, path, caption) sends a local file.\n"
        "Groups: whatsapp_create_group(subject, participants) needs at least one other member. "
        "whatsapp_add_members/whatsapp_remove_members/whatsapp_promote_members/whatsapp_demote_members all "
        "take (jid, participants). whatsapp_delete_group is an approximation, not a true delete -- see its own "
        "description; whatsapp_leave_group just leaves."
    )


PLUGIN = Plugin(
    name="whatsapp",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "whatsapp_login",
            "Connects Caroline to a WhatsApp account: starts the connection and opens a QR code image to scan "
            "with WhatsApp on the phone for that account (Linked Devices > Link a Device). `account`: \"self\" "
            "to connect Caroline's own WhatsApp account, \"owner\" to connect the human owner's personal "
            "account -- always confirm which one before calling this. Call only after making sure they "
            "understand this uses an unofficial library.",
            {"account": str | None}, whatsapp_login,
        ),
        PluginTool(
            "whatsapp_list_chats",
            "Lists an account's WhatsApp chats seen since the connection came up, each with its jid -- the id "
            "to use for whatsapp_send_message. `account`: \"self\" (default) or \"owner\".",
            {"account": str | None}, whatsapp_list_chats,
        ),
        PluginTool(
            "whatsapp_find_chat",
            "Finds the chat(s) matching a name/description, by MEANING (transliteration, diminutives, typos, "
            "word order) -- never exact text. Use this instead of whatsapp_list_chats whenever you're looking "
            "for one specific person/group rather than browsing. Returns the same row shape as "
            "whatsapp_list_chats, only the ones that actually match -- possibly empty if genuinely nothing "
            "fits. `account`: \"self\" (default) or \"owner\".",
            {"query": str, "account": str | None}, whatsapp_find_chat,
        ),
        PluginTool(
            "whatsapp_send_message",
            "Sends a WhatsApp message. `to` is the exact jid or a phone number (see this plugin's own "
            "usage_instructions) -- no add-contact step needed first, unlike Telegram. `account`: \"self\" "
            "(default) or \"owner\" -- only send as \"owner\" when the user has explicitly asked for this "
            "specific message.",
            {"to": str, "text": str, "account": str | None}, whatsapp_send_message,
        ),
        PluginTool(
            "whatsapp_check_number",
            "Checks whether a phone number is registered on WhatsApp at all. Purely optional -- "
            "whatsapp_send_message works on a number directly without this. `account`: \"self\" (default) or "
            "\"owner\".",
            {"phone": str, "account": str | None}, whatsapp_check_number,
        ),
        PluginTool(
            "whatsapp_block_user",
            "Blocks someone on WhatsApp. `jid` is their exact jid or phone number. `account`: \"self\" "
            "(default) or \"owner\" -- blocking on the owner's own account is a real decision about who can "
            "reach them; only do it when they have asked for it.",
            {"jid": str, "account": str | None}, whatsapp_block_user,
        ),
        PluginTool(
            "whatsapp_unblock_user",
            "Reverses whatsapp_block_user. `jid` is their exact jid or phone number. `account`: \"self\" "
            "(default) or \"owner\".",
            {"jid": str, "account": str | None}, whatsapp_unblock_user,
        ),
        PluginTool(
            "whatsapp_download_attachment",
            "Downloads a WhatsApp message's attached media (photo/video/document/voice/...) to a local file. "
            "`ref` comes from a proactive WhatsApp notification (\"media\": ...) -- only usable for a short "
            "while after the message arrives, unlike Telegram's messageId. `savePath` is optional -- omit it "
            "to save under a default Caroline folder and get the resulting path back. `account`: \"self\" "
            "(default) or \"owner\".",
            {"ref": str, "savePath": str | None, "account": str | None}, whatsapp_download_attachment,
        ),
        PluginTool(
            "whatsapp_send_file",
            "Sends a local file as a WhatsApp attachment (photo/video/document/voice -- guessed from the "
            "file's own extension). `to` is a jid or phone number. Optional `caption`. `account`: \"self\" "
            "(default) or \"owner\" -- same owner-impersonation rule as whatsapp_send_message.",
            {"to": str, "path": str, "caption": str | None, "account": str | None}, whatsapp_send_file,
        ),
        PluginTool(
            "whatsapp_create_group",
            "Creates a WhatsApp group. `participants` needs at least one other jid/phone number -- a group "
            "cannot be created with just yourself. `account`: \"self\" (default) or \"owner\".",
            {"subject": str, "participants": list, "account": str | None}, whatsapp_create_group,
        ),
        PluginTool(
            "whatsapp_add_members",
            "Adds members to a WhatsApp group. `jid` is the group's jid; `participants` is a list of "
            "jids/phone numbers. `account`: \"self\" (default) or \"owner\".",
            {"jid": str, "participants": list, "account": str | None}, whatsapp_add_members,
        ),
        PluginTool(
            "whatsapp_remove_members",
            "Removes members from a WhatsApp group. `jid` is the group's jid; `participants` is a list of "
            "jids/phone numbers. `account`: \"self\" (default) or \"owner\".",
            {"jid": str, "participants": list, "account": str | None}, whatsapp_remove_members,
        ),
        PluginTool(
            "whatsapp_promote_members",
            "Promotes members to admin in a WhatsApp group. `jid` is the group's jid; `participants` is a "
            "list of jids/phone numbers. `account`: \"self\" (default) or \"owner\".",
            {"jid": str, "participants": list, "account": str | None}, whatsapp_promote_members,
        ),
        PluginTool(
            "whatsapp_demote_members",
            "Demotes admins back to regular members in a WhatsApp group. `jid` is the group's jid; "
            "`participants` is a list of jids/phone numbers. `account`: \"self\" (default) or \"owner\".",
            {"jid": str, "participants": list, "account": str | None}, whatsapp_demote_members,
        ),
        PluginTool(
            "whatsapp_leave_group",
            "Leaves a WhatsApp group (does not delete it). `jid` is the group's jid. `account`: \"self\" "
            "(default) or \"owner\".",
            {"jid": str, "account": str | None}, whatsapp_leave_group,
        ),
        PluginTool(
            "whatsapp_delete_group",
            "Approximates deleting a WhatsApp group: removes every other participant this account can, then "
            "leaves. WhatsApp has no true group-delete call, so this is the closest available -- the group "
            "ends up empty and inaccessible, including to this account. `jid` is the group's jid. `account`: "
            "\"self\" (default) or \"owner\".",
            {"jid": str, "account": str | None}, whatsapp_delete_group,
        ),
    ],
)
