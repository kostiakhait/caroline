"""whatsapp -- LLM-facing tools over app/whatsapp_channel.py's WhatsApp
connection. Fourth of the planned messenger integrations
(docs/MESSENGER_INTEGRATIONS_PLAN.md, 2026-10-06).

Acts AS the user's personal WhatsApp account via an unofficial protocol
library (Baileys, Node sidecar) -- there is no official API for a
personal account. See whatsapp_channel.py's own doc comment for the real,
stated risk (WhatsApp could restrict/ban the account at their discretion).

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
    WhatsAppError,
    check_number,
    create_group,
    delete_group,
    download_media,
    is_connected,
    leave_group,
    list_chats,
    send_file,
    send_message,
    set_block,
    start_login,
    update_group_participants,
)
from app.workspace_dir import WORKSPACE_DIR
from app.plugins.loader import Plugin, PluginTool


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


async def whatsapp_login(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    try:
        message = await start_login(WORKSPACE_DIR)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": message}


async def whatsapp_list_chats(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    try:
        chats = await list_chats()
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": str(chats)}


async def whatsapp_send_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    to = _to_jid(str(args["to"]))
    text = str(args["text"])
    try:
        await send_message(to, text)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Sent to {to}."}


async def whatsapp_check_number(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    phone = "".join(c for c in str(args.get("phone") or "") if c.isdigit())
    if not phone:
        return {"text": 'whatsapp_check_number needs "phone".', "is_error": True}
    try:
        exists, jid = await check_number(phone)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    if not exists:
        return {"text": f"{phone} is not on WhatsApp (or has hidden itself from this check)."}
    return {"text": f"{phone} is on WhatsApp, jid {jid}."}


async def whatsapp_block_user(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    raw = str(args.get("jid") or "").strip()
    if not raw:
        return {"text": 'whatsapp_block_user needs "jid".', "is_error": True}
    jid = _to_jid(raw)
    try:
        await set_block(jid, True)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Blocked {jid}."}


async def whatsapp_unblock_user(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    raw = str(args.get("jid") or "").strip()
    if not raw:
        return {"text": 'whatsapp_unblock_user needs "jid".', "is_error": True}
    jid = _to_jid(raw)
    try:
        await set_block(jid, False)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Unblocked {jid}."}


async def whatsapp_download_attachment(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    ref = str(args.get("ref") or "").strip()
    if not ref:
        return {"text": 'whatsapp_download_attachment needs "ref" from a proactive WhatsApp notification.', "is_error": True}
    save_path = str(args.get("savePath") or "").strip() or None
    try:
        path = await download_media(ref, save_path)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Saved to {path}."}


async def whatsapp_send_file(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    raw = str(args.get("to") or "").strip()
    path = str(args.get("path") or "").strip()
    if not raw or not path:
        return {"text": 'whatsapp_send_file needs "to" and "path".', "is_error": True}
    to = _to_jid(raw)
    try:
        await send_file(to, path, args.get("caption"))
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Sent {path} to {to}."}


async def whatsapp_create_group(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    subject = str(args.get("subject") or "").strip()
    participants = [_to_jid(str(p)) for p in (args.get("participants") or [])]
    if not subject:
        return {"text": 'whatsapp_create_group needs "subject".', "is_error": True}
    if not participants:
        return {"text": 'whatsapp_create_group needs at least one "participants" jid or phone number.', "is_error": True}
    try:
        jid = await create_group(subject, participants)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f'Created group "{subject}" (jid {jid}).'}


async def _group_participants_tool(args: dict[str, Any], action: str, verb: str) -> dict[str, Any]:
    jid = str(args.get("jid") or "").strip()
    participants = [_to_jid(str(p)) for p in (args.get("participants") or [])]
    if not jid or not participants:
        return {"text": 'needs "jid" and at least one "participants" entry.', "is_error": True}
    try:
        await update_group_participants(jid, participants, action)
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
    jid = str(args.get("jid") or "").strip()
    if not jid:
        return {"text": 'whatsapp_leave_group needs "jid".', "is_error": True}
    try:
        await leave_group(jid)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Left {jid}."}


async def whatsapp_delete_group(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    jid = str(args.get("jid") or "").strip()
    if not jid:
        return {"text": 'whatsapp_delete_group needs "jid".', "is_error": True}
    try:
        await delete_group(jid)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"{jid} emptied and left -- WhatsApp has no true group delete, so every other participant this account could remove was removed first, then this account left too."}


def _usage_instructions() -> str:
    return (
        "Acts as the user's own personal WhatsApp account (their real chats), via an unofficial protocol "
        "library (Baileys) -- there is no official API for a personal account, so WhatsApp could in principle "
        "restrict or ban the account for this at their own discretion. `to`/`jid`/`participants` everywhere "
        "accept either the exact WhatsApp jid (from whatsapp_list_chats, a proactive notification, or a "
        "group's own jid) or a plain phone number -- converted to a jid automatically, never a guessed name. "
        "whatsapp_login is one-time setup (a QR code image opens automatically to scan) -- call it only when "
        "the user has explicitly asked to connect WhatsApp, and make sure they understand the risk stated "
        "above before doing so, never on your own initiative. WhatsApp has no model-facing search here -- "
        "whatsapp_list_chats only returns chats already seen since the connection came up (Baileys keeps no "
        "history by default).\n"
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
            "Connects Caroline to the user's personal WhatsApp account: starts the connection and opens a QR "
            "code image to scan with WhatsApp on their phone (Linked Devices > Link a Device). Call only when "
            "the user has explicitly asked, after making sure they understand this uses an unofficial library.",
            {}, whatsapp_login,
        ),
        PluginTool(
            "whatsapp_list_chats",
            "Lists WhatsApp chats seen since the connection came up, each with its jid -- the id to use for "
            "whatsapp_send_message.",
            {}, whatsapp_list_chats,
        ),
        PluginTool(
            "whatsapp_send_message",
            "Sends a WhatsApp message, as the user. `to` is the exact jid or a phone number (see this "
            "plugin's own usage_instructions) -- no add-contact step needed first, unlike Telegram.",
            {"to": str, "text": str}, whatsapp_send_message,
        ),
        PluginTool(
            "whatsapp_check_number",
            "Checks whether a phone number is registered on WhatsApp at all. Purely optional -- "
            "whatsapp_send_message works on a number directly without this.",
            {"phone": str}, whatsapp_check_number,
        ),
        PluginTool(
            "whatsapp_block_user",
            "Blocks someone on WhatsApp. `jid` is their exact jid or phone number.",
            {"jid": str}, whatsapp_block_user,
        ),
        PluginTool(
            "whatsapp_unblock_user",
            "Reverses whatsapp_block_user. `jid` is their exact jid or phone number.",
            {"jid": str}, whatsapp_unblock_user,
        ),
        PluginTool(
            "whatsapp_download_attachment",
            "Downloads a WhatsApp message's attached media (photo/video/document/voice/...) to a local file. "
            "`ref` comes from a proactive WhatsApp notification (\"media\": ...) -- only usable for a short "
            "while after the message arrives, unlike Telegram's messageId. `savePath` is optional -- omit it "
            "to save under a default Caroline folder and get the resulting path back.",
            {"ref": str, "savePath": str | None}, whatsapp_download_attachment,
        ),
        PluginTool(
            "whatsapp_send_file",
            "Sends a local file as a WhatsApp attachment (photo/video/document/voice -- guessed from the "
            "file's own extension). `to` is a jid or phone number. Optional `caption`.",
            {"to": str, "path": str, "caption": str | None}, whatsapp_send_file,
        ),
        PluginTool(
            "whatsapp_create_group",
            "Creates a WhatsApp group. `participants` needs at least one other jid/phone number -- a group "
            "cannot be created with just yourself.",
            {"subject": str, "participants": list}, whatsapp_create_group,
        ),
        PluginTool(
            "whatsapp_add_members",
            "Adds members to a WhatsApp group. `jid` is the group's jid; `participants` is a list of "
            "jids/phone numbers.",
            {"jid": str, "participants": list}, whatsapp_add_members,
        ),
        PluginTool(
            "whatsapp_remove_members",
            "Removes members from a WhatsApp group. `jid` is the group's jid; `participants` is a list of "
            "jids/phone numbers.",
            {"jid": str, "participants": list}, whatsapp_remove_members,
        ),
        PluginTool(
            "whatsapp_promote_members",
            "Promotes members to admin in a WhatsApp group. `jid` is the group's jid; `participants` is a "
            "list of jids/phone numbers.",
            {"jid": str, "participants": list}, whatsapp_promote_members,
        ),
        PluginTool(
            "whatsapp_demote_members",
            "Demotes admins back to regular members in a WhatsApp group. `jid` is the group's jid; "
            "`participants` is a list of jids/phone numbers.",
            {"jid": str, "participants": list}, whatsapp_demote_members,
        ),
        PluginTool(
            "whatsapp_leave_group",
            "Leaves a WhatsApp group (does not delete it). `jid` is the group's jid.",
            {"jid": str}, whatsapp_leave_group,
        ),
        PluginTool(
            "whatsapp_delete_group",
            "Approximates deleting a WhatsApp group: removes every other participant this account can, then "
            "leaves. WhatsApp has no true group-delete call, so this is the closest available -- the group "
            "ends up empty and inaccessible, including to this account. `jid` is the group's jid.",
            {"jid": str}, whatsapp_delete_group,
        ),
    ],
)
