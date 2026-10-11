"""signal -- LLM-facing tools over app/signal_channel.py's Signal
connection. Fifth and last of the planned messenger integrations
(docs/MESSENGER_INTEGRATIONS_PLAN.md, 2026-10-06).

Acts AS the user's own Signal account (linked device via signal-cli) --
Signal has no bot/app API at all, this is the standard way any
third-party integration talks to it.

Attachments, contacts, groups (2026-10-11), matching the Telegram/
WhatsApp plugins' surface where Signal's own protocol actually has an
equivalent -- it doesn't always: Signal has no "add contact" call
distinct from messaging someone (any phone number can be messaged
directly, no prior contact needed, same as WhatsApp), so
signal_update_contact (set a local name) is the closest analog, not a
separate add/delete. Signal groups are also fully decentralized -- there
is no call that deletes a group "for everyone", not even for its creator
(every member keeps an independent copy) -- signal_delete_group
approximates one the same way whatsapp_delete_group does, and says so.
"""

from __future__ import annotations

from typing import Any

from app.signal_channel import (
    SignalError,
    create_group,
    delete_group,
    download_attachment,
    is_connected,
    list_contacts,
    list_groups,
    quit_group,
    send_file,
    send_message,
    set_block,
    start_login,
    update_contact_name,
    update_group_members,
)
from app.workspace_dir import WORKSPACE_DIR
from app.plugins.loader import Plugin, PluginTool


async def signal_login(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    try:
        message = await start_login(WORKSPACE_DIR)
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": message}


async def signal_list_contacts(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    try:
        contacts = await list_contacts()
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": str(contacts)}


async def signal_send_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    to = str(args["to"]).strip()
    text = str(args["text"])
    try:
        await send_message(to, text)
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Sent to {to}."}


async def signal_send_file(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    to = str(args.get("to") or "").strip()
    path = str(args.get("path") or "").strip()
    if not to or not path:
        return {"text": 'signal_send_file needs "to" and "path".', "is_error": True}
    try:
        await send_file(to, path, args.get("caption"))
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Sent {path} to {to}."}


async def signal_download_attachment(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    ref = str(args.get("ref") or "").strip()
    if not ref:
        return {"text": 'signal_download_attachment needs "ref" from a proactive Signal notification.', "is_error": True}
    save_path = str(args.get("savePath") or "").strip() or None
    try:
        path = await download_attachment(ref, save_path)
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Saved to {path}."}


async def signal_update_contact(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    recipient = str(args.get("recipient") or "").strip()
    if not recipient:
        return {"text": 'signal_update_contact needs "recipient".', "is_error": True}
    try:
        await update_contact_name(recipient, args.get("name"), args.get("givenName"), args.get("familyName"))
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Updated contact {recipient}."}


async def signal_block_user(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    target = str(args.get("target") or "").strip()
    if not target:
        return {"text": 'signal_block_user needs "target".', "is_error": True}
    try:
        await set_block(target, True)
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Blocked {target}."}


async def signal_unblock_user(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    target = str(args.get("target") or "").strip()
    if not target:
        return {"text": 'signal_unblock_user needs "target".', "is_error": True}
    try:
        await set_block(target, False)
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Unblocked {target}."}


async def signal_list_groups(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    try:
        groups = await list_groups()
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": str(groups)}


async def signal_create_group(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    name = str(args.get("name") or "").strip()
    members = [str(m) for m in (args.get("members") or [])]
    if not name:
        return {"text": 'signal_create_group needs "name".', "is_error": True}
    if not members:
        return {"text": 'signal_create_group needs at least one "members" entry.', "is_error": True}
    try:
        group_id = await create_group(name, members)
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f'Created group "{name}" (id {group_id}).'}


async def _group_members_tool(args: dict[str, Any], field: str, verb: str) -> dict[str, Any]:
    group_id = str(args.get("groupId") or "").strip()
    members = [str(m) for m in (args.get("members") or [])]
    if not group_id or not members:
        return {"text": 'needs "groupId" and at least one "members" entry.', "is_error": True}
    try:
        await update_group_members(group_id, **{field: members})
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"{verb} {len(members)} member(s) in {group_id}."}


async def signal_add_group_members(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return await _group_members_tool(args, "add", "Added")


async def signal_remove_group_members(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return await _group_members_tool(args, "remove", "Removed")


async def signal_promote_group_admins(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return await _group_members_tool(args, "promote", "Promoted")


async def signal_demote_group_admins(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return await _group_members_tool(args, "demote", "Demoted")


async def signal_leave_group(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    group_id = str(args.get("groupId") or "").strip()
    if not group_id:
        return {"text": 'signal_leave_group needs "groupId".', "is_error": True}
    try:
        await quit_group(group_id, delete=bool(args.get("delete")), new_admins=[str(a) for a in (args.get("newAdmins") or [])] or None)
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Left {group_id}."}


async def signal_delete_group(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    group_id = str(args.get("groupId") or "").strip()
    if not group_id:
        return {"text": 'signal_delete_group needs "groupId".', "is_error": True}
    try:
        await delete_group(group_id)
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"{group_id} emptied and left -- Signal has no true group delete (every member keeps an independent copy), so every other member this account could remove was removed first, then this account quit too."}


def _usage_instructions() -> str:
    return (
        "Acts as the user's own Signal account (linked as a secondary device via signal-cli, the standard "
        "tool for this -- Signal has no bot/app API at all). `to`/`target`/`recipient` accept either the "
        "exact phone number (E.164, e.g. +15551234567) or a group id -- distinguished automatically (a group "
        "id never starts with \"+\"). signal_login is one-time setup (a QR code image opens automatically to "
        "scan in the Signal app's Linked Devices screen) -- call it only when the user has explicitly asked "
        "to connect Signal, never on your own initiative. There's no model-facing message search here -- "
        "only contacts, groups, and sending.\n"
        "Like WhatsApp, Signal needs no prior contact to message a number cold -- any phone number can be "
        "messaged directly. signal_update_contact just sets a local display name (the closest Signal "
        "equivalent to telegram_add_contact); signal_block_user/signal_unblock_user work on either a "
        "recipient or a group.\n"
        "Attachments: a proactive notification for an incoming photo/video/document/voice message says so "
        "(\"Attached: ...\") and gives a \"ref\" -- download it with signal_download_attachment(ref) before "
        "using it (omit savePath for a default location). signal_send_file(to, path, caption) sends a local "
        "file.\n"
        "Groups: signal_create_group(name, members) needs at least one other member. "
        "signal_add_group_members/signal_remove_group_members/signal_promote_group_admins/"
        "signal_demote_group_admins all take (groupId, members). signal_delete_group is an approximation, "
        "not a true delete -- Signal has none at all, see its own description; signal_leave_group just "
        "leaves (optionally passing newAdmins if this account is the only admin)."
    )


PLUGIN = Plugin(
    name="signal",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "signal_login",
            "Connects Caroline to the user's Signal account as a linked device: opens a QR code image to "
            "scan in the Signal app (Settings > Linked Devices > Link New Device). Call only when the user "
            "has explicitly asked to connect Signal.",
            {}, signal_login,
        ),
        PluginTool(
            "signal_list_contacts",
            "Lists the user's Signal contacts known to signal-cli.",
            {}, signal_list_contacts,
        ),
        PluginTool(
            "signal_send_message",
            "Sends a Signal message, as the user. `to` is a phone number (E.164) or a group id -- no "
            "add-contact step needed first.",
            {"to": str, "text": str}, signal_send_message,
        ),
        PluginTool(
            "signal_send_file",
            "Sends a local file as a Signal attachment. `to` is a phone number or group id. Optional "
            "`caption` (sent as the message text alongside the attachment).",
            {"to": str, "path": str, "caption": str | None}, signal_send_file,
        ),
        PluginTool(
            "signal_download_attachment",
            "Downloads a Signal message's attached media to a local file. `ref` comes from a proactive "
            "Signal notification (\"Attached: ...\"). `savePath` is optional -- omit it to save under a "
            "default Caroline folder and get the resulting path back.",
            {"ref": str, "savePath": str | None}, signal_download_attachment,
        ),
        PluginTool(
            "signal_update_contact",
            "Sets a local display name for a Signal contact (creating a local contact entry if one didn't "
            "exist). `recipient` is their phone number. At least one of `name`, `givenName`, `familyName`.",
            {"recipient": str, "name": str | None, "givenName": str | None, "familyName": str | None}, signal_update_contact,
        ),
        PluginTool(
            "signal_block_user",
            "Blocks a Signal contact or group. `target` is a phone number or group id.",
            {"target": str}, signal_block_user,
        ),
        PluginTool(
            "signal_unblock_user",
            "Reverses signal_block_user. `target` is a phone number or group id.",
            {"target": str}, signal_unblock_user,
        ),
        PluginTool(
            "signal_list_groups",
            "Lists the user's Signal groups with their ids and members.",
            {}, signal_list_groups,
        ),
        PluginTool(
            "signal_create_group",
            "Creates a Signal group. `members` needs at least one other phone number -- a group cannot be "
            "created with just yourself.",
            {"name": str, "members": list}, signal_create_group,
        ),
        PluginTool(
            "signal_add_group_members",
            "Adds members to a Signal group. `groupId` is the group's id; `members` is a list of phone "
            "numbers.",
            {"groupId": str, "members": list}, signal_add_group_members,
        ),
        PluginTool(
            "signal_remove_group_members",
            "Removes members from a Signal group. `groupId` is the group's id; `members` is a list of phone "
            "numbers.",
            {"groupId": str, "members": list}, signal_remove_group_members,
        ),
        PluginTool(
            "signal_promote_group_admins",
            "Promotes members to admin in a Signal group. `groupId` is the group's id; `members` is a list "
            "of phone numbers.",
            {"groupId": str, "members": list}, signal_promote_group_admins,
        ),
        PluginTool(
            "signal_demote_group_admins",
            "Demotes admins back to regular members in a Signal group. `groupId` is the group's id; "
            "`members` is a list of phone numbers.",
            {"groupId": str, "members": list}, signal_demote_group_admins,
        ),
        PluginTool(
            "signal_leave_group",
            "Leaves a Signal group (does not delete it). `groupId` is the group's id. Optional `delete` also "
            "drops this account's local copy of the group. Optional `newAdmins` (phone numbers) is required "
            "if this account is currently the group's only admin.",
            {"groupId": str, "delete": bool | None, "newAdmins": list | None}, signal_leave_group,
        ),
        PluginTool(
            "signal_delete_group",
            "Approximates deleting a Signal group: removes every other member this account can, then leaves "
            "for good. Signal has no true group-delete call at all (every member keeps an independent copy, "
            "even the creator can't force a delete elsewhere) -- this is the closest available. `groupId` is "
            "the group's id.",
            {"groupId": str}, signal_delete_group,
        ),
    ],
)
