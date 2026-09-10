"""email -- Caroline's own mailbox tools, backed entirely by Camerlengo's
email:* v2 API (added 2026-09-09 specifically for this -- see
d:/REPO/reforce/API/Api2EmailCommands.py) rather than any local IMAP/SMTP
client, per explicit instruction. Every account/message/folder operation
goes through the SAME v2 session/call_v2 machinery sms_plugin.py already
uses (sw_api.py) -- no local vault, no local IMAP library, no local
credential storage at all; Camerlengo owns mailbox registration
server-side (email_accounts table, one row per registered address, owned
by the caller's own SquirrelWisdom login, established lazily via the same
~/.mcp-notes/credentials.json sms/notes already use).

Every command below requires a session (Camerlengo's email:* commands are
all `auth: user_role`) -- there is no shared/system mailbox fallback, since
an IMAP mailbox is never a generic relay the way SMTP2GO's shared account
is for sms:send.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.plugins.sw_api import SessionManager, call_v2
from app.policies import read_content_not_headers_instruction

_sessions = SessionManager()

_ACCOUNT_PARAM_NOTE = (
    ' Which registered mailbox performs this operation, e.g. "khait@navlink.net" (see '
    "email_list_accounts). Required on every call -- there is no default/active account."
)


async def email_login(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> dict[str, Any]:
        extra: dict[str, Any] = {
            "session": session, "address": args["address"], "password": args["password"], "imapServer": args["imapServer"],
        }
        if args.get("imapPort") is not None:
            extra["imapPort"] = args["imapPort"]
        if args.get("smtpServer"):
            extra["smtpServer"] = args["smtpServer"]
        if args.get("smtpPort") is not None:
            extra["smtpPort"] = args["smtpPort"]
        return await call_v2("email:setAccount", **extra)

    await _sessions.with_session(do)
    return {"text": f"Saved account {args['address']} (imap: {args['imapServer']}:{args.get('imapPort') or 993}). Pass address: \"{args['address']}\" explicitly on every other call that should use it."}


async def email_list_accounts(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> Any:
        result = await call_v2("email:listAccounts", session=session)
        return result.get("accounts") or []

    accounts = await _sessions.with_session(do)
    return {"text": json.dumps(accounts, indent=2, ensure_ascii=False)}


async def email_remove_account(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda session: call_v2("email:removeAccount", session=session, address=args["address"]))
    return {"text": f'Removed account "{args["address"]}".'}


async def email_list_folders(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> Any:
        result = await call_v2("email:listFolders", session=session, address=args["address"])
        return {k: v for k, v in result.items() if k not in (".status", ".msgid")}

    data = await _sessions.with_session(do)
    return {"text": json.dumps(data, indent=2, ensure_ascii=False)}


async def email_list_messages(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> Any:
        extra: dict[str, Any] = {"session": session, "address": args["address"], "folder": args.get("folder") or "INBOX"}
        if args.get("limit") is not None:
            extra["limit"] = args["limit"]
        if args.get("unseenOnly") is not None:
            extra["unseenOnly"] = bool(args["unseenOnly"])
        if args.get("query"):
            extra["query"] = args["query"]
        result = await call_v2("email:listMessages", **extra)
        return result.get("messages") or []

    messages = await _sessions.with_session(do)
    return {"text": json.dumps(messages, indent=2, ensure_ascii=False)}


async def email_get_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> Any:
        result = await call_v2("email:getMessage", session=session, address=args["address"], folder=args["folder"], uid=args["uid"])
        return {k: v for k, v in result.items() if k not in (".status", ".msgid")}

    data = await _sessions.with_session(do)
    return {"text": json.dumps(data, indent=2, ensure_ascii=False)}


async def email_mark(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda session: call_v2(
        "email:markFlag", session=session, address=args["address"], folder=args["folder"],
        uid=args["uid"], flag=args["flag"], set=bool(args["set"]),
    ))
    return {"text": f'{"Added" if args["set"] else "Removed"} flag {args["flag"]} on uid {args["uid"]} in {args["folder"]}.'}


async def email_move(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda session: call_v2(
        "email:moveMessage", session=session, address=args["address"], folder=args["folder"],
        uid=args["uid"], destFolder=args["destFolder"],
    ))
    return {"text": f'Moved uid {args["uid"]} from {args["folder"]} to {args["destFolder"]}.'}


async def email_delete(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda session: call_v2(
        "email:deleteMessage", session=session, address=args["address"], folder=args["folder"], uid=args["uid"],
    ))
    return {"text": f'Deleted uid {args["uid"]} from {args["folder"]}.'}


async def email_create_folder(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda session: call_v2("email:createFolder", session=session, address=args["address"], path=args["path"]))
    return {"text": f'Created folder "{args["path"]}".'}


async def email_delete_folder(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda session: call_v2("email:deleteFolder", session=session, address=args["address"], path=args["path"]))
    return {"text": f'Deleted folder "{args["path"]}".'}


async def email_download_attachment(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> Any:
        return await call_v2(
            "email:downloadAttachment", session=session, address=args["address"], folder=args["folder"],
            uid=args["uid"], attachmentIndex=args["attachmentIndex"],
        )

    result = await _sessions.with_session(do)
    content_b64 = result.get("content")
    if not isinstance(content_b64, str):
        raise RuntimeError(f'Attachment {args["attachmentIndex"]} on uid {args["uid"]} had no content.')
    data = base64.b64decode(content_b64)
    Path(args["savePath"]).write_bytes(data)
    return {"text": f'Downloaded {len(data)} byte(s) ("{result.get("filename")}") to "{args["savePath"]}".'}


async def email_send(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> Any:
        extra: dict[str, Any] = {
            "session": session, "to": args["to"], "subject": args["subject"], "text": args.get("text") or "",
        }
        if args.get("address"):
            extra["address"] = args["address"]
        if args.get("from"):
            extra["from"] = args["from"]
        if args.get("html"):
            extra["html"] = True
            extra["text"] = args["html"]
            if args.get("text"):
                extra["plainText"] = args["text"]
        if args.get("cc"):
            extra["cc"] = args["cc"]
        if args.get("bcc"):
            extra["bcc"] = args["bcc"]
        if args.get("inReplyTo"):
            extra["inReplyTo"] = args["inReplyTo"]
        if args.get("references"):
            extra["references"] = args["references"]
        if args.get("attachments"):
            wire_attachments = []
            for a in args["attachments"]:
                p = Path(a["path"])
                wire_attachments.append({
                    "filename": a.get("filename") or p.name,
                    "content": base64.b64encode(p.read_bytes()).decode("ascii"),
                    "content_type": a.get("contentType") or "application/octet-stream",
                })
            extra["attachments"] = wire_attachments
        return await call_v2("email:send", **extra)

    result = await _sessions.with_session(do)
    sent_note = f'Saved a copy to Sent.' if result.get("savedToSentFolder") else "Not saved to Sent (no registered account used, or no Sent folder)."
    return {"text": f'Sent to {", ".join(args["to"])} (subject: "{args["subject"]}"). Message-Id: {result.get("messageId")}. {sent_note}'}


def _check_sent_mail_too_instruction() -> str:
    return (
        "When checking mail for anything that looks like it needs a reply or action, check the Sent folder too, "
        "not just the inbox -- an incoming message that looks unanswered may already have a reply sitting in "
        "Sent that just hasn't been seen/marked from the other end yet. Don't flag or nudge about something as "
        "needing attention, or re-raise it as unresolved, without first checking whether it was already answered. "
        'This applies per-thread/subject, not as a blanket "read all of Sent every time" -- check Sent for the '
        "specific thing you're about to flag before flagging it."
    )


def _mark_discussed_emails_read_instruction() -> str:
    return (
        "Once you've reported an email's content to the user or discussed it with them in the conversation, "
        "mark it as read (email_mark) if it was unread -- they've now seen it via you, so an unread badge on it "
        "is just noise. This applies to an email you summarized/read aloud in a briefing or digest, one you "
        "opened and described in answer to a question, and one whose content came up in back-and-forth "
        "discussion -- not to an email you merely listed by subject/sender without describing its content, and "
        "not to one the user hasn't actually seen discussed yet. When in doubt whether it's been meaningfully "
        "surfaced to the user, leave it unread rather than mark it."
    )


def _usage_instructions() -> str:
    return "\n\n".join((
        read_content_not_headers_instruction(),
        _check_sent_mail_too_instruction(),
        _mark_discussed_emails_read_instruction(),
    ))


PLUGIN = Plugin(
    name="email",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "email_login",
            "Registers one mailbox's IMAP/SMTP credentials with Camerlengo. Verifies the credentials by "
            "actually connecting, then saves them server-side alongside any other accounts already "
            "registered this way. This does NOT make the account a default for other calls -- every other "
            "tool below still requires the account (`address`) to be named explicitly each time. If "
            "smtpServer/smtpPort are omitted, they default to imapServer and 587.",
            {
                "address": str, "password": str, "imapServer": str,
                "imapPort": int | None, "smtpServer": str | None, "smtpPort": int | None,
            }, email_login,
        ),
        PluginTool(
            "email_list_accounts",
            "Lists every account registered via email_login (address + IMAP host). Never returns passwords. "
            "There is no \"active\" account -- use the returned addresses as the `address` argument on other calls.",
            {}, email_list_accounts,
        ),
        PluginTool(
            "email_remove_account",
            "Deletes a registered mailbox. Every other call naming this address will fail until it's "
            "registered again via email_login.",
            {"address": str}, email_remove_account,
        ),
        PluginTool(
            "email_list_folders",
            "Lists every IMAP folder/mailbox for the given account (INBOX, Sent, Trash, custom folders, "
            "etc.), plus the best-guess special folders (inbox/sent/trash/drafts/spam)." + _ACCOUNT_PARAM_NOTE,
            {"address": str}, email_list_folders,
        ),
        PluginTool(
            "email_list_messages",
            "Lists lightweight message metadata (uid, from, subject, date, flags) for a folder, newest "
            "first. Use `query` for a simple subject/from/body substring search, or `unseenOnly` to "
            "restrict to unread mail." + _ACCOUNT_PARAM_NOTE,
            {
                "address": str, "folder": str | None, "limit": int | None,
                "unseenOnly": bool | None, "query": str | None,
            }, email_list_messages,
        ),
        PluginTool(
            "email_get_message",
            "Fetches a single message's full content (subject, from, to, date, text/html body, attachment "
            "list) by folder + uid." + _ACCOUNT_PARAM_NOTE,
            {"address": str, "folder": str, "uid": int}, email_get_message,
        ),
        PluginTool(
            "email_mark",
            'Adds or removes an IMAP flag on a message, e.g. flag:"\\\\Seen" set:true to mark read, or '
            'flag:"\\\\Flagged" for starring.' + _ACCOUNT_PARAM_NOTE,
            {"address": str, "folder": str, "uid": int, "flag": str, "set": bool}, email_mark,
        ),
        PluginTool(
            "email_move",
            "Moves a message from one folder to another (e.g. archiving, filing into a project folder)." + _ACCOUNT_PARAM_NOTE,
            {"address": str, "folder": str, "uid": int, "destFolder": str}, email_move,
        ),
        PluginTool(
            "email_delete",
            "Deletes a message -- moves it to the account's Trash folder if one exists, otherwise flags "
            "\\Deleted and expunges it." + _ACCOUNT_PARAM_NOTE,
            {"address": str, "folder": str, "uid": int}, email_delete,
        ),
        PluginTool(
            "email_create_folder",
            'Creates an IMAP folder (mailbox). For a nested path (e.g. "Projects/Foo"), the parent folder '
            "usually needs to already exist." + _ACCOUNT_PARAM_NOTE,
            {"address": str, "path": str}, email_create_folder,
        ),
        PluginTool(
            "email_delete_folder",
            "Deletes an IMAP folder (mailbox) and everything in it -- irreversible, there is no trash for "
            "the folder itself (only for messages moved out of it beforehand)." + _ACCOUNT_PARAM_NOTE,
            {"address": str, "path": str}, email_delete_folder,
        ),
        PluginTool(
            "email_download_attachment",
            "Saves one attachment from a message to a local file path, by its index in email_get_message's "
            "attachment list." + _ACCOUNT_PARAM_NOTE,
            {"address": str, "folder": str, "uid": int, "attachmentIndex": int, "savePath": str}, email_download_attachment,
        ),
        PluginTool(
            "email_send",
            "Sends an email via Camerlengo. Pass `address` to authenticate as one of your OWN registered "
            "mailboxes (own SMTP creds, own From, a Sent-folder copy); omit it to relay through the shared "
            'no_reply@partners.solutions identity instead. Use `from` to set a different display From '
            "header while still authenticating as `address` -- double-check they match the identity you "
            "intend before sending.",
            {
                "address": str | None, "to": list, "subject": str, "from": str | None,
                "text": str | None, "html": str | None, "cc": list | None, "bcc": list | None,
                "attachments": list | None, "inReplyTo": str | None, "references": list | None,
            }, email_send,
        ),
    ],
)
