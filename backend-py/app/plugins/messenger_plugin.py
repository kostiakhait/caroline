"""messenger -- LLM-facing tools over app/messenger_channel.py's Facebook
Messenger connections. Fifth and last of the planned messenger integrations
(docs/MESSENGER_INTEGRATIONS_PLAN.md, 2026-10-06).

TWO accounts, same split as every other messenger plugin here: "self"
(default) is Caroline's own Messenger account -- she may read and reply
there as herself, no extra permission needed; "owner" is the human owner's
own personal Messenger account -- read freely, but only send AS the owner
when they have explicitly asked for that specific message. Acts via an
unofficial protocol library (fca-unofficial, Node sidecar) either way --
there is no official API for a personal Messenger inbox, only for business
Pages. Login is browser-based (changed 2026-10-19: 2FA is universal enough
now that a direct email/password login isn't realistic) -- messenger_login
just opens Caroline's own embedded browser window and the user logs in
there BY HAND, same as any ordinary browser; see messenger_channel.py's own
doc comment for the full flow (cookie-jar polling, appState export). No
password or 2FA code ever passes through this plugin at all.

Groups, attachments (2026-10-19), matching the WhatsApp/Telegram plugins'
surface where Messenger's own protocol actually has an equivalent --
messenger_delete_group approximates it (see its own description) rather
than claiming something Messenger can't actually do, same as
whatsapp_delete_group.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.messenger_channel import (
    ACCOUNTS,
    DEFAULT_ACCOUNT,
    MessengerError,
    browser_label,
    create_group,
    delete_group,
    download_attachment,
    is_valid_account,
    leave_group,
    list_chats,
    list_chats_page,
    login_start,
    read_thread_history,
    send_file,
    send_message,
    set_block,
    update_group_participants,
)
from app.workspace_dir import WORKSPACE_DIR
from app.context_cap import cap_count
from app.memory_topics import get_current_topic
from app.plugins.loader import Plugin, PluginTool
from app.plugins.messenger_search import find_by_name
from app.session_context import get_tab_id
from app.window_registry import register_window


def _account_or_error(args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    account = str(args.get("account") or DEFAULT_ACCOUNT).strip()
    if not is_valid_account(account):
        return account, {"text": f'"account" must be one of {", ".join(ACCOUNTS)}, not {account!r}.', "is_error": True}
    return account, None


async def messenger_login(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    try:
        message = await login_start(account)
    except MessengerError as exc:
        return {"text": str(exc), "is_error": True}
    tab_id = get_tab_id()
    register_window(
        f"browser:{browser_label(account)}", kind="browser", label=browser_label(account),
        purpose=f"Facebook login for the Messenger \"{account}\" account", tab_id=tab_id,
        topic_id=get_current_topic(WORKSPACE_DIR, tab_id),
    )
    return {"text": message}


def _format_chat_row(chat: dict[str, Any]) -> dict[str, Any]:
    """Converts the sidecar's raw epoch-ms "lastActivity" into an ISO-8601
    string -- per explicit instruction, the user wants each chat's last-
    activity date/time visible, not a bare millisecond number."""
    row = dict(chat)
    ts = row.pop("lastActivity", None)
    row["lastActivity"] = (
        datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(ts, (int, float)) and ts > 0 else None
    )
    return row


async def messenger_list_chats(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    """Per explicit instruction (2026-10-20): returns ONE page at a time --
    fast, instead of the earlier exhaustive fetch that took ~40s for a real
    account and made the model give up waiting mid-call. Pass the previous
    call's own "nextCursor" back as `cursor` to load the next page ("load
    the first page" = call with no cursor; "load the next page" = call
    again with the cursor just returned); `nextCursor: null` means there is
    nothing more. Chats come newest-activity-first. A short-TTL per-account
    cache (see list_chats_page's own docstring) makes repeated calls fast
    without silently going stale forever."""
    account, error = _account_or_error(args)
    if error:
        return error
    cursor = args.get("cursor")
    if cursor is not None and not isinstance(cursor, int):
        return {"text": '"cursor" must be the integer nextCursor a previous messenger_list_chats call returned, or omitted for the first page.', "is_error": True}
    try:
        page = await list_chats_page(account, cursor)
    except MessengerError as exc:
        return {"text": str(exc), "is_error": True}
    chats = [_format_chat_row(c) for c in page["chats"]]
    chats, _cut = cap_count(chats)
    return {"text": str({"chats": chats, "nextCursor": page["nextCursor"], "fromCache": page["fromCache"]})}


async def messenger_find_chat(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    query = str(args["query"]).strip()
    if not query:
        return {"text": 'messenger_find_chat needs "query".', "is_error": True}
    try:
        chats = await list_chats(account)
    except MessengerError as exc:
        return {"text": str(exc), "is_error": True}
    matches = await find_by_name(query, chats)
    matches, _cut = cap_count(matches)
    return {"text": str(matches)}


async def messenger_read_history(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    thread_id = str(args.get("threadId") or "").strip()
    if not thread_id:
        return {"text": 'messenger_read_history needs "threadId".', "is_error": True}
    amount = args.get("amount")
    if not isinstance(amount, int) or amount <= 0:
        return {"text": 'messenger_read_history needs "amount" (a positive integer) -- how many of the most recent messages to load.', "is_error": True}
    try:
        messages = await read_thread_history(account, thread_id, amount)
    except MessengerError as exc:
        return {"text": str(exc), "is_error": True}
    messages, _cut = cap_count(messages)
    return {"text": str(messages)}


async def messenger_send_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    to = str(args.get("to") or "").strip()
    text = str(args.get("text") or "")
    if not to or not text:
        return {"text": 'messenger_send_message needs "to" and "text".', "is_error": True}
    try:
        await send_message(account, to, text)
    except MessengerError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Sent to {to}."}


async def messenger_block_user(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    user_id = str(args.get("userId") or "").strip()
    if not user_id:
        return {"text": 'messenger_block_user needs "userId".', "is_error": True}
    try:
        await set_block(account, user_id, True)
    except MessengerError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Blocked {user_id}."}


async def messenger_unblock_user(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    user_id = str(args.get("userId") or "").strip()
    if not user_id:
        return {"text": 'messenger_unblock_user needs "userId".', "is_error": True}
    try:
        await set_block(account, user_id, False)
    except MessengerError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Unblocked {user_id}."}


async def messenger_download_attachment(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    ref = str(args.get("ref") or "").strip()
    if not ref:
        return {"text": 'messenger_download_attachment needs "ref" from a proactive Messenger notification.', "is_error": True}
    save_path = str(args.get("savePath") or "").strip() or None
    try:
        path = await download_attachment(WORKSPACE_DIR, account, ref, save_path)
    except MessengerError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Saved to {path}."}


async def messenger_send_file(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    to = str(args.get("to") or "").strip()
    path = str(args.get("path") or "").strip()
    if not to or not path:
        return {"text": 'messenger_send_file needs "to" and "path".', "is_error": True}
    try:
        await send_file(account, to, path, args.get("caption"))
    except MessengerError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Sent {path} to {to}."}


async def messenger_create_group(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    subject = str(args.get("subject") or "").strip()
    participants = [str(p) for p in (args.get("participants") or [])]
    if not subject:
        return {"text": 'messenger_create_group needs "subject".', "is_error": True}
    if not participants:
        return {"text": 'messenger_create_group needs at least one "participants" user id.', "is_error": True}
    try:
        thread_id = await create_group(account, subject, participants)
    except MessengerError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f'Created group "{subject}" (threadID {thread_id}).'}


async def _group_participants_tool(args: dict[str, Any], action: str, verb: str) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    thread_id = str(args.get("threadId") or "").strip()
    participants = [str(p) for p in (args.get("participants") or [])]
    if not thread_id or not participants:
        return {"text": 'needs "threadId" and at least one "participants" entry.', "is_error": True}
    try:
        await update_group_participants(account, thread_id, participants, action)
    except MessengerError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"{verb} {len(participants)} participant(s) in {thread_id}."}


async def messenger_add_members(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return await _group_participants_tool(args, "add", "Added")


async def messenger_remove_members(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return await _group_participants_tool(args, "remove", "Removed")


async def messenger_promote_members(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return await _group_participants_tool(args, "promote", "Promoted")


async def messenger_demote_members(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return await _group_participants_tool(args, "demote", "Demoted")


async def messenger_leave_group(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    thread_id = str(args.get("threadId") or "").strip()
    if not thread_id:
        return {"text": 'messenger_leave_group needs "threadId".', "is_error": True}
    try:
        await leave_group(account, thread_id)
    except MessengerError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Left {thread_id}."}


async def messenger_delete_group(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    account, error = _account_or_error(args)
    if error:
        return error
    thread_id = str(args.get("threadId") or "").strip()
    if not thread_id:
        return {"text": 'messenger_delete_group needs "threadId".', "is_error": True}
    try:
        await delete_group(account, thread_id)
    except MessengerError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"{thread_id} emptied and left -- Messenger has no true group delete, so every other participant this account could remove was removed first, then this account left too."}


def _usage_instructions() -> str:
    return (
        "Two accounts, same split as the other messenger plugins: `account=\"self\"` (default) is Caroline's own "
        "Messenger account -- read/reply there freely, no extra permission needed. `account=\"owner\"` is the "
        "human owner's own personal Messenger (their real chats) -- read freely, but only send AS the owner "
        "when they have explicitly asked you to send that specific message; an unofficial protocol library "
        "(fca-unofficial) either way, since there is no official API for a personal Messenger inbox, only for "
        "business Pages. messenger_login(account=...) is one-time setup per account -- it opens Caroline's own "
        "embedded browser window on facebook.com and the USER logs in there by hand (password, 2FA code, "
        "checkpoint -- whatever Facebook asks for, exactly like any ordinary browser login); no password or code "
        "ever passes through you at all. Call it only when the user has explicitly asked to connect that "
        "account, never on your own initiative -- after that, just tell them the window is open and to log in "
        "there; you don't need to watch or poll for them, the connection completes on its own once they do. "
        "messenger_list_chats(cursor=...) returns ONE page of the real thread list at a time, newest-activity-"
        "first -- omit cursor for the first page, pass back the previous call's own nextCursor for the next one; "
        "nextCursor: null means there is nothing more. Only page further if you actually need to (still haven't "
        "found what you're after) -- don't walk the whole list reflexively. messenger_find_chat(query) finds a "
        "chat by name/description, judged by meaning, searching the FULL thread list across as many pages as it "
        "takes -- use it instead of messenger_list_chats when looking for one specific chat rather than browsing. "
        "Both are backed by a short-lived per-account cache, so repeated calls are fast. Neither ever shows what "
        "was actually SAID in a thread (only name/id/last-activity) -- call messenger_read_history(threadId, "
        "amount) for that.\n"
        "`to`/`userId`/`threadId`/`participants` everywhere are Messenger's own numeric ids (from "
        "messenger_list_chats, a proactive notification, or a group's own threadID) -- never a guessed name.\n"
        "Attachments: a proactive notification for an incoming photo/video/file says so (\"media\": ...) and "
        "gives a \"ref\"; messenger_read_history gives the same kind of ref for a past message's own attachment. "
        "Either way, download it with messenger_download_attachment(ref) before using it (omit savePath for a "
        "default location) -- that ref only works for a short while, same limitation as whatsapp_download_"
        "attachment's own ref. messenger_send_file(to, path, caption) sends a local file.\n"
        "Groups: messenger_create_group(subject, participants) needs at least one other member. "
        "messenger_add_members/messenger_remove_members/messenger_promote_members/messenger_demote_members all "
        "take (threadId, participants). messenger_delete_group is an approximation, not a true delete -- see its "
        "own description; messenger_leave_group just leaves."
    )


PLUGIN = Plugin(
    name="messenger",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "messenger_login",
            "Connects Caroline to a Facebook Messenger account: opens Caroline's own embedded browser window on "
            "facebook.com for the user to log in BY HAND (password, 2FA, checkpoint -- whatever Facebook asks, "
            "same as any ordinary browser). `account`: \"self\" to connect Caroline's own Messenger account, "
            "\"owner\" to connect the human owner's personal account -- always confirm which one before calling "
            "this. Call only when the user has explicitly asked to connect that account. Returns immediately -- "
            "the connection itself completes automatically once the user finishes logging in, no further tool "
            "call needed.",
            {"account": str | None}, messenger_login,
        ),
        PluginTool(
            "messenger_list_chats",
            "Lists one PAGE of an account's real Messenger thread list, newest-activity-first, each row with its "
            "id (threadID), name, isGroup, and lastActivity (ISO 8601 date/time of that chat's last activity). "
            "Omit `cursor` to load the first (newest) page; to load the next page, pass back the exact "
            "\"nextCursor\" value the previous call returned -- `nextCursor: null` means there are no more pages. "
            "Keep calling with the returned cursor only as long as you actually need more (e.g. still looking for "
            "something) -- there is no need to page through everything if what you need is already on an earlier "
            "page. Recently-fetched pages are served from a short-lived cache, so repeat calls are fast. Shows the "
            "thread list only (name, id, last activity), never message content -- use messenger_read_history for "
            "that. `account`: \"self\" (default) or \"owner\".",
            {"cursor": int | None, "account": str | None}, messenger_list_chats,
        ),
        PluginTool(
            "messenger_find_chat",
            "Finds the chat(s) matching a name/description, by MEANING (transliteration, diminutives, typos, word "
            "order) -- never exact text. Use this instead of messenger_list_chats whenever you're looking for one "
            "specific person/group rather than browsing. Returns the same row shape as messenger_list_chats, only "
            "the ones that actually match -- possibly empty if genuinely nothing fits. `account`: \"self\" "
            "(default) or \"owner\".",
            {"query": str, "account": str | None}, messenger_find_chat,
        ),
        PluginTool(
            "messenger_read_history",
            "Reads the most recent messages in one Messenger thread -- the only way to see a thread's actual "
            "content; messenger_list_chats/messenger_find_chat only ever show the thread list itself (name, id), "
            "never what was said in it. `threadId` is the thread's id. `amount` is how many of the most recent "
            "messages to load -- always give an explicit number here, there is no built-in default; ask the user "
            "if you're not sure how much history they want. Returns each message's sender, text, timestamp, and "
            "an attachment ref (if any) usable with messenger_download_attachment. `account`: \"self\" (default) "
            "or \"owner\".",
            {"threadId": str, "amount": int, "account": str | None}, messenger_read_history,
        ),
        PluginTool(
            "messenger_send_message",
            "Sends a Messenger message. `to` is the exact threadID (see this plugin's own usage_instructions). "
            "`account`: \"self\" (default) or \"owner\" -- only send as \"owner\" when the user has explicitly "
            "asked for this specific message.",
            {"to": str, "text": str, "account": str | None}, messenger_send_message,
        ),
        PluginTool(
            "messenger_block_user",
            "Blocks someone on Messenger. `userId` is their Messenger user id. `account`: \"self\" (default) or "
            "\"owner\" -- blocking on the owner's own account is a real decision about who can reach them; only "
            "do it when they have asked for it.",
            {"userId": str, "account": str | None}, messenger_block_user,
        ),
        PluginTool(
            "messenger_unblock_user",
            "Reverses messenger_block_user. `userId` is their Messenger user id. `account`: \"self\" (default) or "
            "\"owner\".",
            {"userId": str, "account": str | None}, messenger_unblock_user,
        ),
        PluginTool(
            "messenger_download_attachment",
            "Downloads a Messenger message's attached media to a local file. `ref` comes from a proactive "
            "Messenger notification (\"media\": ...) -- only usable for a while after the message arrives. "
            "`savePath` is optional -- omit it to save under a default Caroline folder and get the resulting "
            "path back. `account`: \"self\" (default) or \"owner\".",
            {"ref": str, "savePath": str | None, "account": str | None}, messenger_download_attachment,
        ),
        PluginTool(
            "messenger_send_file",
            "Sends a local file as a Messenger attachment. `to` is a threadID. Optional `caption`. `account`: "
            "\"self\" (default) or \"owner\" -- same owner-impersonation rule as messenger_send_message.",
            {"to": str, "path": str, "caption": str | None, "account": str | None}, messenger_send_file,
        ),
        PluginTool(
            "messenger_create_group",
            "Creates a Messenger group chat. `participants` needs at least one other user id -- a group cannot be "
            "created with just yourself. `account`: \"self\" (default) or \"owner\".",
            {"subject": str, "participants": list, "account": str | None}, messenger_create_group,
        ),
        PluginTool(
            "messenger_add_members",
            "Adds members to a Messenger group. `threadId` is the group's id; `participants` is a list of user "
            "ids. `account`: \"self\" (default) or \"owner\".",
            {"threadId": str, "participants": list, "account": str | None}, messenger_add_members,
        ),
        PluginTool(
            "messenger_remove_members",
            "Removes members from a Messenger group. `threadId` is the group's id; `participants` is a list of "
            "user ids. `account`: \"self\" (default) or \"owner\".",
            {"threadId": str, "participants": list, "account": str | None}, messenger_remove_members,
        ),
        PluginTool(
            "messenger_promote_members",
            "Promotes members to admin in a Messenger group. `threadId` is the group's id; `participants` is a "
            "list of user ids. `account`: \"self\" (default) or \"owner\".",
            {"threadId": str, "participants": list, "account": str | None}, messenger_promote_members,
        ),
        PluginTool(
            "messenger_demote_members",
            "Demotes admins back to regular members in a Messenger group. `threadId` is the group's id; "
            "`participants` is a list of user ids. `account`: \"self\" (default) or \"owner\".",
            {"threadId": str, "participants": list, "account": str | None}, messenger_demote_members,
        ),
        PluginTool(
            "messenger_leave_group",
            "Leaves a Messenger group (does not delete it). `threadId` is the group's id. `account`: \"self\" "
            "(default) or \"owner\".",
            {"threadId": str, "account": str | None}, messenger_leave_group,
        ),
        PluginTool(
            "messenger_delete_group",
            "Approximates deleting a Messenger group: removes every other participant this account can, then "
            "leaves. Messenger has no true group-delete call, so this is the closest available -- the group ends "
            "up empty and inaccessible, including to this account. `threadId` is the group's id. `account`: "
            "\"self\" (default) or \"owner\".",
            {"threadId": str, "account": str | None}, messenger_delete_group,
        ),
    ],
)
