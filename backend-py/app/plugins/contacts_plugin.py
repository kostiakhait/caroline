"""contacts -- Caroline's address book. Built by analogy with notes_plugin.py
(see that file's own docstring) but talks the NEWER v2 protocol directly via
sw_api.py's own call_v2/SessionManager -- unlike Notes (a v1 "Plugins.py"
module predating v2 entirely), there's no separate older-protocol client to
maintain here, so this one file covers what notes_api.py + notes_plugin.py
together cover for Notes.

Caroline is a PRIMARY user of this service, not just a passive UI backend --
see this module's own usage instructions below: every contact detail she
learns anywhere (email, SMS, conversation) gets written here unprompted, and
she checks here before ever asking the user for someone's contact info (the
concrete, tool-level expression of policies.py's self_sufficiency_instruction).
"""

from __future__ import annotations

import json
import secrets
import string
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.plugins.sw_api import SessionManager, SwApiError, call_v2

_sessions = SessionManager()

_ID_ALPHABET = string.ascii_letters + string.digits
# Same effective ceiling as notes_plugin.py's MAX_ATTACHMENT_BYTES -- nginx
# caps the request body at 64MB, base64 inflates raw bytes by ~33%.
MAX_PHOTO_BYTES = 47 * 1024 * 1024


def _gen_id(length: int) -> str:
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _summarize(contact: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": contact["id"],
        "name": contact.get("name") or "",
        "company": contact.get("company") or "",
        "tags": contact.get("tags") or [],
        "emails": [e.get("value") for e in (contact.get("emails") or []) if e.get("value")],
        "phones": [p.get("value") for p in (contact.get("phones") or []) if p.get("value")],
        "updatedAt": contact.get("updatedAt"),
        "deleted": bool(contact.get("deleted")),
    }


# ---------------------------------------------------------------------------
# CRUD, mirroring notes_plugin.py's own shape over the contact: v2 commands.
# ---------------------------------------------------------------------------

async def _list_index(session: str) -> dict[str, dict[str, Any]]:
    result = await call_v2("contact:list", session=session)
    return (result or {}).get("contacts") or {}


async def list_contacts(session: str, tag: str | None = None, include_deleted: bool = False) -> list[dict[str, Any]]:
    index = await _list_index(session)
    entries = [
        {"id": cid, **c}
        for cid, c in index.items()
        if (include_deleted or not c.get("deleted"))
        and (tag is None or tag in (c.get("tags") or []))
    ]
    entries.sort(key=lambda e: (e.get("name") or "").lower())
    return entries


async def search_contacts(session: str, query: str, tag: str | None = None, include_deleted: bool = False) -> list[dict[str, Any]]:
    q = query.lower()

    def _matches(c: dict[str, Any]) -> bool:
        haystack = " ".join([
            c.get("name") or "", c.get("company") or "", c.get("title") or "", c.get("notes") or "",
            " ".join(c.get("tags") or []),
            " ".join(e.get("value", "") for e in (c.get("emails") or [])),
            " ".join(p.get("value", "") for p in (c.get("phones") or [])),
            " ".join(a.get("value", "") for a in (c.get("addresses") or [])),
        ]).lower()
        return q in haystack

    index = await _list_index(session)
    entries = [
        {"id": cid, **c}
        for cid, c in index.items()
        if (include_deleted or not c.get("deleted"))
        and (tag is None or tag in (c.get("tags") or []))
        and _matches(c)
    ]
    entries.sort(key=lambda e: (e.get("name") or "").lower())
    return entries


async def get_contact(session: str, contact_id: str) -> dict[str, Any]:
    result = await call_v2("contact:get", session=session, id=contact_id)
    contact = (result or {}).get("contact")
    if not contact:
        raise SwApiError(f'Contact "{contact_id}" not found.')
    return {"id": contact_id, **contact}


async def _save(session: str, contact_id: str, contact: dict[str, Any]) -> dict[str, Any]:
    result = await call_v2("contact:save", session=session, id=contact_id, contact=contact)
    if result and result.get("applied") is False:
        # A newer version is already stored server-side (CAS, see Api2ContactCommands.py's
        # own doc comment) -- surface the conflict instead of pretending the write took
        # effect, same discipline the backend itself documents.
        raise SwApiError(f'Contact "{contact_id}" was updated elsewhere since last read -- re-fetch and retry.')
    return {"id": contact_id, **contact}


async def create_contact(
    session: str, name: str, emails: list[dict[str, str]] | None = None, phones: list[dict[str, str]] | None = None,
    addresses: list[dict[str, str]] | None = None, company: str | None = None, title: str | None = None,
    birthday: str | None = None, notes: str | None = None, tags: list[str] | None = None,
) -> dict[str, Any]:
    contact_id = _gen_id(12)
    contact = {
        "name": name, "emails": emails or [], "phones": phones or [], "addresses": addresses or [],
        "company": company or "", "title": title or "", "birthday": birthday or "", "notes": notes or "",
        "tags": tags or [], "updatedAt": _now_ms(), "deleted": False,
    }
    return await _save(session, contact_id, contact)


async def update_contact(session: str, contact_id: str, **fields: Any) -> dict[str, Any]:
    current = await get_contact(session, contact_id)
    defined = {k: v for k, v in fields.items() if v is not None}
    contact = {**current, **defined, "updatedAt": _now_ms()}
    contact.pop("id", None)
    return await _save(session, contact_id, contact)


async def delete_contact(session: str, contact_id: str) -> dict[str, Any]:
    return await update_contact(session, contact_id, deleted=True)


# ---------------------------------------------------------------------------
# Photos -- same attachment shape Notes uses, keyed by contactId.
# ---------------------------------------------------------------------------

async def attach_photo(session: str, contact_id: str, local_file_path: str, original_name: str | None = None) -> dict[str, Any]:
    import base64
    p = Path(local_file_path)
    size = p.stat().st_size
    if size > MAX_PHOTO_BYTES:
        raise SwApiError(
            f"File is {size / 1024 / 1024:.1f}MB, which exceeds the ~{MAX_PHOTO_BYTES / 1024 / 1024:.0f}MB "
            "effective photo-attachment limit (base64 inflation over the 64MB request-body cap)."
        )
    data = p.read_bytes()
    filename = _gen_id(16)
    uploaded = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    await call_v2(
        "contact:saveAttachment", session=session, filename=filename,
        content=base64.b64encode(data).decode("ascii"), originalName=original_name or p.name,
        contactId=contact_id, uploaded=uploaded,
    )
    return {"filename": filename, "originalName": original_name or p.name, "uploaded": uploaded, "contactId": contact_id}


async def list_photos(session: str, contact_id: str | None = None) -> list[dict[str, Any]]:
    extra = {"contactId": contact_id} if contact_id else {}
    result = await call_v2("contact:listAttachments", session=session, **extra)
    return (result or {}).get("attachments") or []


async def download_photo(session: str, filename: str, save_path: str) -> int:
    import base64
    result = await call_v2("contact:readAttachment", session=session, filename=filename)
    content_b64 = (result or {}).get("content")
    if not isinstance(content_b64, str):
        raise SwApiError(f'Photo "{filename}" not found.')
    data = base64.b64decode(content_b64)
    Path(save_path).write_bytes(data)
    return len(data)


async def remove_photo(session: str, filename: str) -> None:
    await call_v2("contact:removeAttachment", session=session, filename=filename)


# ---------------------------------------------------------------------------
# Tool handlers.
# ---------------------------------------------------------------------------

_EMAIL_PHONE_SCHEMA = {"type": "array", "items": {"type": "object", "properties": {
    "type": {"type": "string", "description": "e.g. work/personal/mobile/home/other"},
    "value": {"type": "string"},
}, "required": ["value"]}}
_ADDRESS_SCHEMA = {"type": "array", "items": {"type": "object", "properties": {
    "type": {"type": "string", "description": "e.g. home/work/other"},
    "value": {"type": "string", "description": "freeform street/city/etc."},
}, "required": ["value"]}}


async def contact_list(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    entries = await _sessions.with_session(lambda s: list_contacts(s, tag=args.get("tag"), include_deleted=bool(args.get("includeDeleted"))))
    return {"text": json.dumps([_summarize(e) for e in entries], indent=2, ensure_ascii=False)}


async def contact_search(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    entries = await _sessions.with_session(lambda s: search_contacts(s, args["query"], tag=args.get("tag"), include_deleted=bool(args.get("includeDeleted"))))
    return {"text": json.dumps([_summarize(e) for e in entries], indent=2, ensure_ascii=False)}


async def contact_get(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    contact = await _sessions.with_session(lambda s: get_contact(s, args["id"]))
    return {"text": json.dumps(contact, indent=2, ensure_ascii=False)}


async def contact_create(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    contact = await _sessions.with_session(lambda s: create_contact(
        s, args["name"], emails=args.get("emails"), phones=args.get("phones"), addresses=args.get("addresses"),
        company=args.get("company"), title=args.get("title"), birthday=args.get("birthday"),
        notes=args.get("notes"), tags=args.get("tags"),
    ))
    return {"text": json.dumps(_summarize(contact), indent=2, ensure_ascii=False)}


async def contact_update(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    fields = {k: args.get(k) for k in ("name", "emails", "phones", "addresses", "company", "title", "birthday", "notes", "tags")}
    contact = await _sessions.with_session(lambda s: update_contact(s, args["id"], **fields))
    return {"text": json.dumps(_summarize(contact), indent=2, ensure_ascii=False)}


async def contact_delete(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    contact = await _sessions.with_session(lambda s: delete_contact(s, args["id"]))
    return {"text": json.dumps(_summarize(contact), indent=2, ensure_ascii=False)}


async def contact_attach_photo(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    entry = await _sessions.with_session(lambda s: attach_photo(s, args["contactId"], args["filePath"], args.get("originalName")))
    return {"text": json.dumps(entry, indent=2, ensure_ascii=False)}


async def contact_list_photos(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    entries = await _sessions.with_session(lambda s: list_photos(s, args.get("contactId")))
    return {"text": json.dumps(entries, indent=2, ensure_ascii=False)}


async def contact_download_photo(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    byte_count = await _sessions.with_session(lambda s: download_photo(s, args["filename"], args["savePath"]))
    return {"text": f'Downloaded {byte_count} byte(s) to "{args["savePath"]}".'}


async def contact_remove_photo(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda s: remove_photo(s, args["filename"]))
    return {"text": f'Removed photo "{args["filename"]}".'}


# Per explicit instruction (2026-10-02): same not-hardcoded-into-every-prompt
# convention every other plugin follows -- this is the concrete, tool-level
# expression of policies.py's self_sufficiency_instruction for contacts
# specifically: Caroline is a PRIMARY user of this address book, not just a
# UI backend for the human. Two obligations, stated here (fetched on demand,
# the same way notes_plugin.py's own usage instructions already work),
# rather than ALWAYS_ON_INSTRUCTIONS, which would cost every single turn
# regardless of whether contacts ever come up:
_USAGE_INSTRUCTIONS = (
    "Whenever you learn ANY contact detail about a real person -- an email address, phone number, "
    "physical address, company/title, birthday, or anything else that would belong on a contact card -- "
    "and it isn't already recorded here (or is recorded but now stale/incomplete), save or update it with "
    "contact_create/contact_update yourself, unprompted. Don't wait to be asked, and don't just let it sit "
    "in conversation history where it'll be hard to find again. Before asking the user for someone's "
    "contact info, ALWAYS check here first with contact_search/contact_get -- only ask if it's genuinely "
    "not here. Tags (e.g. \"family\", \"work\", \"bank\") are the organizing concept here, not folders -- "
    "apply the ones that fit, don't invent an elaborate taxonomy. Photos attach via contact_attach_photo, "
    "same local-file-path convention as notes_attach. Your OWNER'S OWN contact entry, if you create or find "
    "one, gets the tag \"self\" (create it if none exists and you learn their name/etc. -- a real person's "
    "assistant would have their own boss's own card too) -- keep its name/emails/phones in sync with the "
    "small owner_profile_get/owner_profile_set cache and \"Caroline:Profile\" Notes whenever any one of the "
    "three changes."
)


PLUGIN = Plugin(
    name="contacts",
    usage_instructions=_USAGE_INSTRUCTIONS,
    tools=[
        PluginTool(
            "contact_list",
            "Lists contacts, optionally filtered to one tag.",
            {"tag": str | None, "includeDeleted": bool | None}, contact_list,
        ),
        PluginTool(
            "contact_search",
            "Case-insensitive substring search over a contact's name/company/title/notes/tags/emails/phones/"
            "addresses, optionally filtered to one tag. Check here BEFORE asking the user for someone's "
            "contact info.",
            {"query": str, "tag": str | None, "includeDeleted": bool | None}, contact_search,
        ),
        PluginTool(
            "contact_get",
            "Fetches one contact's full record by id.",
            {"id": str}, contact_get,
        ),
        PluginTool(
            "contact_create",
            "Creates a new contact. Save anything you learn about a real person here unprompted -- see this "
            "plugin's own usage instructions.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "emails": _EMAIL_PHONE_SCHEMA, "phones": _EMAIL_PHONE_SCHEMA, "addresses": _ADDRESS_SCHEMA,
                    "company": {"type": "string"}, "title": {"type": "string"},
                    "birthday": {"type": "string", "description": "YYYY-MM-DD"},
                    "notes": {"type": "string", "description": "freeform"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name"],
            },
            contact_create,
        ),
        PluginTool(
            "contact_update",
            "Updates a contact. Only provided fields are changed; array fields (emails/phones/addresses/tags) "
            "are replaced wholesale, not merged -- fetch the current record with contact_get first if you need "
            "to add to an existing list rather than overwrite it.",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "emails": _EMAIL_PHONE_SCHEMA, "phones": _EMAIL_PHONE_SCHEMA, "addresses": _ADDRESS_SCHEMA,
                    "company": {"type": "string"}, "title": {"type": "string"},
                    "birthday": {"type": "string"}, "notes": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id"],
            },
            contact_update,
        ),
        PluginTool(
            "contact_delete",
            "Soft-deletes a contact (marks it deleted; it is never physically removed).",
            {"id": str}, contact_delete,
        ),
        PluginTool(
            "contact_attach_photo",
            "Uploads a local image file and attaches it to a contact as a photo. Effective size limit ~47MB.",
            {"contactId": str, "filePath": str, "originalName": str | None}, contact_attach_photo,
        ),
        PluginTool(
            "contact_list_photos",
            "Lists photo attachments, optionally filtered to one contact.",
            {"type": "object", "properties": {"contactId": {"type": "string"}}, "required": []}, contact_list_photos,
        ),
        PluginTool(
            "contact_download_photo",
            "Downloads a photo's raw bytes to a local file path -- the only way to fetch a photo's content.",
            {"filename": str, "savePath": str}, contact_download_photo,
        ),
        PluginTool(
            "contact_remove_photo",
            "Removes a photo from a contact.",
            {"filename": str}, contact_remove_photo,
        ),
    ],
)
