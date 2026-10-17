"""email -- Caroline's own mailbox tools, backed by Camerlengo's email:* v2
API (see d:/REPO/reforce/API/Api2EmailCommands.py) rather than any local
IMAP/SMTP client. Camerlengo does the actual IMAP/SMTP work but stores
NOTHING itself (2026-09-10, per explicit instruction: it's a stateless
RESTful API, no server-side account table, ever) -- every operation below
takes the full mailbox credentials (password/imapHost/...) as ordinary
required parameters, passed straight through to Camerlengo on every call.

Per explicit instruction (2026-09-10): there is deliberately no separate
credential-storage adapter/tool here (no "email_login", no vault module).
Credentials live as ordinary Notes, found and saved through the SAME
general-purpose notes_* tools used for everything else -- see
_usage_instructions below for the exact convention. This plugin never
reads or writes them itself.

Every command still requires a Camerlengo session (`auth: user_role`) --
that identifies the CALLER for abuse-prevention/logging, unrelated to
which mailbox credentials are being operated on. One exception, confirmed
live 2026-09-16: email_send's own email:send command predates the rest of
this file (the newer multi-account IMAP surface added 2026-09-09) and is
still on the OLDER `auth: scope` model, grouped with sms:send -- it needs
EMAIL_SERVICE_KEY passed alongside session, not session alone. See
EMAIL_SERVICE_KEY's own comment for the incident this fixes.

Bug fix (2026-09-10): a real send failed 3/3 tries with a confusing SMTP/TLS
error. Root cause: Camerlengo silently reused the IMAP host as the SMTP host
whenever smtpServer was omitted (fixed server-side too, in
Api2EmailCommands.py -- it now rejects a missing smtpServer explicitly
instead of guessing wrong). On this side: the tool's own params were named
imapServer/smtpServer while the actual credential notes already in use
store imapHost/smtpHost -- not a naming confusion the model needs help
with (it maps synonyms fine), but renamed to match anyway, on the theory
that removing an unnecessary translation step removes one more place a
field can silently get dropped. The instructions below now say explicitly:
fill in EVERY field this tool asks for, matching by MEANING against
whatever a credential source actually calls it (imapHost/imap_host/"IMAP
server"/etc. are all the same field) -- never skip one just because a
source's exact wording doesn't match this tool's own parameter name.
"""

from __future__ import annotations

import asyncio
import base64
from email.utils import getaddresses
from pathlib import Path
import json
from typing import Any

from app.plugins.contacts_plugin import upsert_contacts_from_emails
from app.plugins.loader import Plugin, PluginTool
from app.plugins.sw_api import SessionManager, call_v2
from app.policies import follow_explicit_parameters_instruction, notes_folder_fallback_instruction, read_content_not_headers_instruction

_sessions = SessionManager()

# Bug fix (2026-09-16), confirmed live ("Что за ошибка revoked key?"):
# email:send (unlike the rest of this file's email:* commands) is one of
# the ORIGINAL, pre-existing Camerlengo email commands (grouped with
# sms:send under `auth: scope` in reforce's own docs/api/commands.md, NOT
# the newer `auth: user_role` multi-account IMAP surface added 2026-09-09
# that every other function below actually uses) -- it needs a scoped
# service key IN ADDITION TO session, the same shape sms_plugin.py's own
# SMS_SERVICE_KEY already provides for sms:send. This file never sent one
# at all (confirmed via git history: email:send was written fresh
# directly in this Python port, never carried over from anywhere that
# already had a key) -- every single send was failing unconditionally,
# for every mailbox, with Camerlengo's own generic "Missing, unknown,
# expired or revoked key" error (matches "missing" here, not an actual
# revocation). Granted a real email:send scope to the existing
# "caroline-email-provisioning" key (reforce's key_scopes table, id 20 --
# previously scoped only to email:create) rather than mint a whole new
# credential for the same feature area.
from app.sw_service_keys_local import EMAIL_SERVICE_KEY  # noqa: E402

_ACCOUNT_PARAM_NOTE = (
    " Look up this mailbox's saved credentials first (see this plugin's own usage instructions) -- "
    "every call needs them explicitly, there is no default/active account or server-side registration."
)


def _creds_kwargs(args: dict[str, Any]) -> dict[str, Any]:
    """Tool-facing param names are imapHost/smtpHost (renamed 2026-09-10,
    see this plugin's own module docstring) -- translated to Camerlengo's
    existing wire field names (imapServer/smtpServer) here, at the one
    boundary that needs to know both, so Camerlengo's own API contract
    doesn't have to change."""
    kwargs: dict[str, Any] = {"address": args["address"], "password": args["password"], "imapServer": args["imapHost"]}
    if args.get("imapPort") is not None:
        kwargs["imapPort"] = args["imapPort"]
    if args.get("smtpHost"):
        kwargs["smtpServer"] = args["smtpHost"]
    if args.get("smtpPort") is not None:
        kwargs["smtpPort"] = args["smtpPort"]
    return kwargs


async def email_list_folders(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> Any:
        result = await call_v2("email:listFolders", session=session, **_creds_kwargs(args))
        return {k: v for k, v in result.items() if k not in (".status", ".msgid")}

    data = await _sessions.with_session(do)
    return {"text": json.dumps(data, indent=2, ensure_ascii=False)}


async def email_list_messages(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> Any:
        extra: dict[str, Any] = {"session": session, "folder": args.get("folder") or "INBOX", **_creds_kwargs(args)}
        if args.get("limit") is not None:
            extra["limit"] = args["limit"]
        if args.get("unseenOnly") is not None:
            extra["unseenOnly"] = bool(args["unseenOnly"])
        if args.get("query"):
            extra["query"] = args["query"]
        if args.get("sinceIso"):
            extra["sinceDate"] = args["sinceIso"]
        if args.get("untilIso"):
            extra["untilDate"] = args["untilIso"]
        result = await call_v2("email:listMessages", **extra)
        return result.get("messages") or []

    messages = await _sessions.with_session(do)
    return {"text": json.dumps(messages, indent=2, ensure_ascii=False)}


async def email_latest_uid(address: str, password: str, imap_host: str, folder: str = "INBOX") -> int | None:
    """Per explicit instruction (2026-10-12): the reply-expected auto-
    checker's own email probe (chat_session.py's _reply_watch_tick) needs
    to know whether NEW mail has landed in a mailbox since a message was
    sent, on a 5/15-minute cadence -- too fine-grained for IMAP's own
    SINCE search (day granularity only, see Api2EmailCommands.py's
    before_date/since_date). UIDs are monotonically increasing per folder
    (RFC 3501), so "highest UID now > the UID captured right after
    sending" is a simple, exact "something new arrived" check with no
    date parsing at all. Internal helper, not a model-facing tool -- same
    credentials shape as every other function here, called directly with
    plain arguments rather than through the args-dict tool contract."""
    async def do(session: str) -> Any:
        result = await call_v2(
            "email:listMessages", session=session, folder=folder, limit=1,
            address=address, password=password, imapServer=imap_host,
        )
        messages = result.get("messages") or []
        return int(messages[0]["uid"]) if messages else None

    return await _sessions.with_session(do)


# Bug fix (2026-10-16), confirmed live: email_read_range's own batch fetch (which calls this
# function as its worker, see email_batch_plugin.py) returned a handful of messages' FULL,
# untruncated bodies in one tool response -- one long thread was enough to overflow the
# conversation's context even after auto-compaction, archiving the session as unrecoverable.
# Same failure shape as the 41-image/~48.5MB incident files_plugin.py/pdf_pages.py already
# document and fixed for images/PDFs -- this plugin never got that same treatment. Fixed the
# same way: truncate with a continuation offset instead of returning the whole body.
# EMAIL_BODY_MAX_CHARS reuses the number already established for the same "a long text is cut
# with a note" pattern elsewhere in this codebase (apps/microagents/settings.py's
# MEMORY_NOTES_MAX_TEXT_CHARS), not a new, separately-chosen constant.
EMAIL_BODY_MAX_CHARS = 20_000


def _truncate_body(data: dict[str, Any], body_offset: int) -> None:
    """Mutates data in place: each of "text"/"html", if present and longer than
    EMAIL_BODY_MAX_CHARS from body_offset, is cut to that window and a
    continuation note is appended to "text" (the one field the model actually
    reads as prose) -- same shape as files_plugin.py's read_document_pages
    "N more pages beyond this range -- call again with page_start=N"."""
    for field in ("text", "html"):
        value = data.get(field)
        if not isinstance(value, str):
            continue
        total = len(value)
        window = value[body_offset:body_offset + EMAIL_BODY_MAX_CHARS]
        if body_offset + len(window) < total:
            data[field] = window
            if field == "text":
                shown_end = body_offset + len(window)
                data["text"] += (
                    f"\n\n[... body truncated: showing characters {body_offset}-{shown_end} of {total} total -- "
                    f"call email_get_message again with bodyOffset={shown_end} to continue reading ...]"
                )
        elif body_offset:
            data[field] = window


async def email_get_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> Any:
        result = await call_v2("email:getMessage", session=session, folder=args["folder"], uid=args["uid"], **_creds_kwargs(args))
        return {k: v for k, v in result.items() if k not in (".status", ".msgid")}

    data = await _sessions.with_session(do)
    # Per explicit instruction (2026-10-13): save every sender/addressee on a message Caroline
    # actually reads -- see upsert_contacts_from_emails' own docstring. "from"/"to" here are
    # already flattened "Name <addr>, addr2" display strings (Api2EmailCommands.py's own
    # _address_text), the exact shape email.utils.getaddresses is for. Excludes the mailbox
    # being read -- it's the owner's own account, not a new contact. Fire-and-forget: never
    # worth delaying the read over this.
    pairs = getaddresses([data.get("from") or "", data.get("to") or ""])
    asyncio.ensure_future(upsert_contacts_from_emails(pairs, exclude={args.get("address") or ""}))
    _truncate_body(data, int(args.get("bodyOffset") or 0))
    return {"text": json.dumps(data, indent=2, ensure_ascii=False)}


async def email_mark(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda session: call_v2(
        "email:markFlag", session=session, folder=args["folder"], uid=args["uid"],
        flag=args["flag"], set=bool(args["set"]), **_creds_kwargs(args),
    ))
    return {"text": f'{"Added" if args["set"] else "Removed"} flag {args["flag"]} on uid {args["uid"]} in {args["folder"]}.'}


async def email_move(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda session: call_v2(
        "email:moveMessage", session=session, folder=args["folder"], uid=args["uid"],
        destFolder=args["destFolder"], **_creds_kwargs(args),
    ))
    return {"text": f'Moved uid {args["uid"]} from {args["folder"]} to {args["destFolder"]}.'}


async def email_delete(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda session: call_v2(
        "email:deleteMessage", session=session, folder=args["folder"], uid=args["uid"], **_creds_kwargs(args),
    ))
    return {"text": f'Deleted uid {args["uid"]} from {args["folder"]}.'}


async def email_create_folder(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda session: call_v2("email:createFolder", session=session, path=args["path"], **_creds_kwargs(args)))
    return {"text": f'Created folder "{args["path"]}".'}


async def email_delete_folder(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda session: call_v2("email:deleteFolder", session=session, path=args["path"], **_creds_kwargs(args)))
    return {"text": f'Deleted folder "{args["path"]}".'}


async def email_download_attachment(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    async def do(session: str) -> Any:
        return await call_v2(
            "email:downloadAttachment", session=session, folder=args["folder"], uid=args["uid"],
            attachmentIndex=args["attachmentIndex"], **_creds_kwargs(args),
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
            "key": EMAIL_SERVICE_KEY, "session": session, "to": args["to"], "subject": args["subject"],
            "text": args.get("text") or "",
        }
        if args.get("address"):
            extra.update(_creds_kwargs(args))
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
    # Per explicit instruction (2026-10-13): save every actual recipient of a message Caroline
    # sends -- see upsert_contacts_from_emails' own docstring. to/cc/bcc are model-supplied
    # strings, either bare addresses or "Name <addr>" -- getaddresses handles both. Excludes the
    # mailbox sent FROM (the owner's/Caroline's own account, never a new contact) and the no_reply
    # relay identity used when no mailbox credentials were given.
    pairs = getaddresses([*args["to"], *(args.get("cc") or []), *(args.get("bcc") or [])])
    own_addresses = {args.get("address") or "", args.get("from") or "", "no_reply@partners.solutions"}
    asyncio.ensure_future(upsert_contacts_from_emails(pairs, exclude=own_addresses))
    sent_note = f'Saved a copy to Sent.' if result.get("savedToSentFolder") else "Not saved to Sent (no address/credentials given, or no Sent folder)."
    return {"text": f'Sent to {", ".join(args["to"])} (subject: "{args["subject"]}"). Message-Id: {result.get("messageId")}. {sent_note}'}


def _credentials_convention_instruction() -> str:
    return (
        "This plugin has no credential storage or \"login\" tool of its own, deliberately -- every mailbox's "
        "IMAP/SMTP credentials MUST be found and saved through the SAME general-purpose notes_* tools you "
        'already use for everything else, never through an email-specific adapter. The convention: one note '
        'per mailbox, in folder "Caroline:Vault" (see vault_security_instruction -- this used to say "Claude '
        'Credentials", a stale, pre-2026-09-14 folder name that directly contradicted the general vault '
        "convention; confirmed live, 2026-09-22, that this exact contradiction caused a real mailbox's "
        'credentials to go unfound), titled exactly "vault:email:<address>" (e.g. '
        '"vault:email:kostia.khait@gmail.com"), with the rest of the note\'s text being a single-line JSON '
        'object: {"password": "...", "imapHost": "...", "imapPort": 993, "smtpHost": "...", "smtpPort": 587}. '
        "Before calling ANY other tool in this plugin for a given address, you MUST look up that note first "
        "(notes_search or notes_list on that folder). If it's genuinely not there, see the note below on "
        "searching every folder before concluding no credentials exist -- an OLDER note for the same mailbox "
        "can still be sitting in a since-renamed folder.\n\n"
        "Fill in EVERY field this tool asks for -- match a saved note's (or the user's own) fields to this "
        "tool's parameters BY MEANING, not by requiring an identical spelling: \"imapHost\"/\"imap_host\"/\"IMAP "
        "server\" are all the same thing as this tool's imapHost parameter, and likewise for smtpHost. Never "
        "skip a field just because a source you're reading happened to phrase it differently, and never treat "
        "smtpHost as skippable or assume it equals imapHost -- real providers almost always use two DIFFERENT "
        "hosts for IMAP vs. SMTP (e.g. Gmail: imap.gmail.com vs. smtp.gmail.com); omitting or guessing it wrong "
        "breaks sending with a confusing low-level error instead of a clear one. If the user gives you a new "
        "mailbox's credentials to use, save them yourself via notes_create in exactly this format before using "
        "them, translating whatever they call each field into this convention's own names."
    )


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
        _credentials_convention_instruction(),
        notes_folder_fallback_instruction(),
        read_content_not_headers_instruction(),
        _check_sent_mail_too_instruction(),
        _mark_discussed_emails_read_instruction(),
        follow_explicit_parameters_instruction(),
    ))


_CREDENTIAL_PARAMS = {
    "address": str, "password": str, "imapHost": str,
    "imapPort": int | None, "smtpHost": str | None, "smtpPort": int | None,
}


PLUGIN = Plugin(
    name="email",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "email_list_folders",
            "Lists every IMAP folder/mailbox for the given account (INBOX, Sent, Trash, custom folders, "
            "etc.), plus the best-guess special folders (inbox/sent/trash/drafts/spam)." + _ACCOUNT_PARAM_NOTE,
            {**_CREDENTIAL_PARAMS}, email_list_folders,
        ),
        PluginTool(
            "email_list_messages",
            "Lists lightweight message metadata (uid, from, subject, date, flags) for a folder, newest "
            "first. Use `query` for a simple subject/from/body substring search, `unseenOnly` to "
            "restrict to unread mail, or `sinceIso`/`untilIso` (ISO dates, e.g. \"2026-10-01\") for a "
            "[since, until) date interval (day granularity only -- IMAP has no finer filter). Need the "
            "actual content (to/from/cc/body/attachments), not just this metadata, for every message in "
            "a folder/date range? Use email_read_range instead of listing then fetching each uid one by "
            "one. Checking more than one mailbox or folder at once? Use email_list_messages_batch." + _ACCOUNT_PARAM_NOTE,
            {**_CREDENTIAL_PARAMS, "folder": str | None, "limit": int | None, "unseenOnly": bool | None, "query": str | None, "sinceIso": str | None, "untilIso": str | None},
            email_list_messages,
        ),
        PluginTool(
            "email_get_message",
            "Fetches a single message's content (subject, from, to, cc, date, text/html body, "
            f"attachment list) by folder + uid. A body longer than {EMAIL_BODY_MAX_CHARS} characters "
            "comes back truncated with a note telling you how long it really is and what bodyOffset to "
            "call again with to keep reading -- never assume a truncated body IS the whole message." + _ACCOUNT_PARAM_NOTE,
            {**_CREDENTIAL_PARAMS, "folder": str, "uid": int, "bodyOffset": int | None}, email_get_message,
        ),
        PluginTool(
            "email_mark",
            'Adds or removes an IMAP flag on a message, e.g. flag:"\\\\Seen" set:true to mark read, or '
            'flag:"\\\\Flagged" for starring. Marking several messages at once (possibly across different '
            "mailboxes)? Use email_mark_batch instead." + _ACCOUNT_PARAM_NOTE,
            {**_CREDENTIAL_PARAMS, "folder": str, "uid": int, "flag": str, "set": bool}, email_mark,
        ),
        PluginTool(
            "email_move",
            "Moves a message from one folder to another (e.g. archiving, filing into a project folder). "
            "Moving several messages at once? Use email_move_batch instead." + _ACCOUNT_PARAM_NOTE,
            {**_CREDENTIAL_PARAMS, "folder": str, "uid": int, "destFolder": str}, email_move,
        ),
        PluginTool(
            "email_delete",
            "Deletes a message -- moves it to the account's Trash folder if one exists, otherwise flags "
            "\\Deleted and expunges it. Deleting several messages at once (a cleanup list, possibly across "
            "different mailboxes)? Use email_delete_batch instead." + _ACCOUNT_PARAM_NOTE,
            {**_CREDENTIAL_PARAMS, "folder": str, "uid": int}, email_delete,
        ),
        PluginTool(
            "email_create_folder",
            'Creates an IMAP folder (mailbox). For a nested path (e.g. "Projects/Foo"), the parent folder '
            "usually needs to already exist." + _ACCOUNT_PARAM_NOTE,
            {**_CREDENTIAL_PARAMS, "path": str}, email_create_folder,
        ),
        PluginTool(
            "email_delete_folder",
            "Deletes an IMAP folder (mailbox) and everything in it -- irreversible, there is no trash for "
            "the folder itself (only for messages moved out of it beforehand)." + _ACCOUNT_PARAM_NOTE,
            {**_CREDENTIAL_PARAMS, "path": str}, email_delete_folder,
        ),
        PluginTool(
            "email_download_attachment",
            "Saves one attachment from a message to a local file path, by its index in email_get_message's "
            "attachment list." + _ACCOUNT_PARAM_NOTE,
            {**_CREDENTIAL_PARAMS, "folder": str, "uid": int, "attachmentIndex": int, "savePath": str}, email_download_attachment,
        ),
        PluginTool(
            "email_send",
            "Sends an email via Camerlengo. To authenticate as one of YOUR OWN mailboxes (own SMTP creds, own "
            "From, a Sent-folder copy), pass ALL of address/password/imapHost/smtpHost together -- these are "
            "not independently optional, and smtpHost is NOT the same host as imapHost for real providers "
            "(e.g. Gmail: imap.gmail.com vs. smtp.gmail.com) -- a saved credential note has every one of these "
            "fields, look it up and pass all four rather than sending with some of them missing. Omit ALL of "
            "them together to relay through the shared no_reply@partners.solutions identity instead (no "
            "mailbox authentication, no Sent-folder copy). Use `from` to set a different display From header "
            "while still authenticating as `address` -- double-check they match the identity you intend "
            "before sending. Sending several emails at once? Use email_send_batch instead.",
            {
                "address": str | None, "password": str | None, "imapHost": str | None,
                "imapPort": int | None, "smtpHost": str | None, "smtpPort": int | None,
                "to": list, "subject": str, "from": str | None,
                "text": str | None, "html": str | None, "cc": list | None, "bcc": list | None,
                "attachments": list | None, "inReplyTo": str | None, "references": list | None,
            }, email_send,
        ),
    ],
)
