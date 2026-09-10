"""notes -- ports mcp-servers-src/notes/src/{index,notes,attachments}.ts (17
tools) to notes_api.py's httpx-based client for the older, dot-envelope
Camerlengo protocol (see notes_api.py's own docstring for why this is a
separate client from sw_api.py rather than a shared one)."""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.plugins.loader import Plugin, PluginTool
from app.plugins.notes_api import (
    MAX_ATTACHMENT_BYTES,
    NotesApiError,
    SessionManager,
    call_plugin,
    gen_attachment_filename,
    gen_note_id,
)

_sessions = SessionManager()

FOLDER_MEMORY_NOTE = (
    ' For Claude\'s own long-term memory (not asked for by the user), use folder "Claude Memory" '
    "unless the user directs otherwise. This tool works with any note/folder the user names, too."
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_ms(ms: Any) -> str:
    return datetime.fromtimestamp((ms or 0) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _title_of(text: str) -> str:
    return text.split("\n", 1)[0]


def _in_folder_scope(folder: str | None, scope: str) -> bool:
    f = folder or ""
    return f == scope or f.startswith(scope + "/")


def _summarize(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry["id"],
        "title": _title_of(entry.get("text", "")),
        "folder": entry.get("folder") or "",
        "updatedAt": entry.get("updatedAt"),
        "updatedAtIso": _iso_ms(entry.get("updatedAt")),
        "deleted": bool(entry.get("deleted")),
    }


# ---------------------------------------------------------------------------
# notes.ts equivalent -- readIndex/writeNoteFile/patchIndex-backed CRUD.
# ---------------------------------------------------------------------------

async def _read_index(session: str) -> dict[str, dict[str, Any]]:
    result = await call_plugin("readIndex", session)
    return (result or {}).get("notes") or {}


async def _read_note_file(session: str, note_id: str) -> dict[str, Any] | None:
    result = await call_plugin("getNote", session, id=note_id)
    return (result or {}).get("note")


async def _write_note_file(session: str, note_id: str, note: dict[str, Any]) -> None:
    await call_plugin("writeNoteFile", session, id=note_id, note=note)


async def _patch_index(session: str, entries: dict[str, dict[str, Any]]) -> None:
    # Merges just the given {id: note} pairs into index.json server-side --
    # safer than read-modify-write, can't race a concurrent writer's own
    # patchIndex call.
    await call_plugin("patchIndex", session, entries=entries)


async def list_notes(session: str, folder: str | None = None, include_deleted: bool = False) -> list[dict[str, Any]]:
    index = await _read_index(session)
    entries = [
        {"id": note_id, **note}
        for note_id, note in index.items()
        if (include_deleted or not note.get("deleted"))
        and (folder is None or (note.get("folder") or "") == folder)
        and not note.get("isFolderMarker")
    ]
    entries.sort(key=lambda e: e.get("updatedAt", 0), reverse=True)
    return entries


async def search_notes(session: str, query: str, folder: str | None = None, include_deleted: bool = False) -> list[dict[str, Any]]:
    q = query.lower()
    index = await _read_index(session)
    entries = [
        {"id": note_id, **note}
        for note_id, note in index.items()
        if (include_deleted or not note.get("deleted"))
        and not note.get("isFolderMarker")
        and (folder is None or _in_folder_scope(note.get("folder"), folder))
        and q in note.get("text", "").lower()
    ]
    entries.sort(key=lambda e: e.get("updatedAt", 0), reverse=True)
    return entries


async def get_note(session: str, note_id: str) -> dict[str, Any]:
    note = await _read_note_file(session, note_id)
    if not note:
        raise NotesApiError(f'Note "{note_id}" not found.')
    return {"id": note_id, **note}


async def _save_note(session: str, note_id: str, note: dict[str, Any]) -> dict[str, Any]:
    # Individual file first, then the index entry -- so a crash mid-op
    # leaves the individual file (the source of truth) consistent.
    await _write_note_file(session, note_id, note)
    await _patch_index(session, {note_id: note})
    return {"id": note_id, **note}


async def create_note(session: str, text: str, folder: str | None = None, is_folder_marker: bool = False) -> dict[str, Any]:
    note_id = gen_note_id()
    note = {"text": text, "updatedAt": _now_ms(), "deleted": False, "folder": folder or "", "isFolderMarker": is_folder_marker}
    return await _save_note(session, note_id, note)


async def _patch_note(session: str, note_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    current = await _read_note_file(session, note_id)
    if not current:
        raise NotesApiError(f'Note "{note_id}" not found.')
    defined_patch = {k: v for k, v in patch.items() if v is not None}
    note = {**current, **defined_patch, "updatedAt": _now_ms()}
    return await _save_note(session, note_id, note)


async def update_note(session: str, note_id: str, text: str | None = None, folder: str | None = None) -> dict[str, Any]:
    return await _patch_note(session, note_id, {"text": text, "folder": folder})


async def delete_note(session: str, note_id: str) -> dict[str, Any]:
    return await _patch_note(session, note_id, {"deleted": True})


async def move_note(session: str, note_id: str, folder: str) -> dict[str, Any]:
    return await _patch_note(session, note_id, {"folder": folder})


async def list_folders(session: str) -> list[str]:
    index = await _read_index(session)
    folders: set[str] = set()
    for note in index.values():
        if note.get("deleted"):
            continue
        folder = note.get("folder") or ""
        if not folder:
            continue
        segments = folder.split("/")
        for i in range(1, len(segments) + 1):
            folders.add("/".join(segments[:i]))
    return sorted(folders)


async def create_folder(session: str, path: str) -> None:
    await create_note(session, "", path, is_folder_marker=True)


async def _batch_move_folder(
    session: str,
    matches: Callable[[str], bool],
    apply_patch: Callable[[dict[str, Any]], dict[str, Any]],
) -> int:
    # Rewrites every affected note individually, then merges them all into
    # the index with a single patchIndex call.
    index = await _read_index(session)
    affected = [(note_id, note) for note_id, note in index.items() if not note.get("deleted") and matches(note.get("folder") or "")]
    changed: dict[str, dict[str, Any]] = {}
    for note_id, note in affected:
        patched = {**note, **apply_patch(note), "updatedAt": _now_ms()}
        await _write_note_file(session, note_id, patched)
        changed[note_id] = patched
    if affected:
        await _patch_index(session, changed)
    return len(affected)


async def rename_folder(session: str, old_path: str, new_path: str) -> int:
    return await _batch_move_folder(
        session,
        lambda folder: folder == old_path or folder.startswith(old_path + "/"),
        lambda note: {"folder": new_path + (note.get("folder") or "")[len(old_path):]},
    )


async def delete_folder(session: str, path: str) -> int:
    return await _batch_move_folder(
        session,
        lambda folder: folder == path or folder.startswith(path + "/"),
        lambda _note: {"deleted": True},
    )


# ---------------------------------------------------------------------------
# attachments.ts equivalent.
# ---------------------------------------------------------------------------

async def attach_file(session: str, note_id: str, local_file_path: str, original_name: str | None = None) -> dict[str, Any]:
    p = Path(local_file_path)
    size = p.stat().st_size
    if size > MAX_ATTACHMENT_BYTES:
        raise NotesApiError(
            f"File is {size / 1024 / 1024:.1f}MB, which exceeds the ~{MAX_ATTACHMENT_BYTES / 1024 / 1024:.0f}MB "
            "effective attachment limit (base64 inflation over the 64MB request-body cap)."
        )
    data = p.read_bytes()
    filename = gen_attachment_filename()
    entry = {
        "filename": filename,
        "originalName": original_name or p.name,
        "uploaded": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "noteId": note_id,
    }
    # Single call -- writes the raw bytes and the meta entry atomically server-side.
    await call_plugin("saveAttachment", session, filename=filename, content=base64.b64encode(data).decode("ascii"), originalName=entry["originalName"], noteId=note_id)
    return entry


async def list_attachments(session: str, note_id: str | None = None) -> list[dict[str, Any]]:
    result = await call_plugin("listAttachments", session)
    attachments: list[dict[str, Any]] = (result or {}).get("attachments") or []
    return [a for a in attachments if a.get("noteId") == note_id] if note_id else attachments


async def remove_attachment(session: str, note_id: str, filename: str) -> None:
    # The backend's removeAttachment always drops both the meta entry and
    # the underlying blob in one call -- no supported "detach but keep the
    # file" action.
    existing = await list_attachments(session, note_id)
    if not any(a.get("filename") == filename for a in existing):
        raise NotesApiError(f'Attachment "{filename}" on note "{note_id}" not found.')
    await call_plugin("removeAttachment", session, filename=filename)


async def download_attachment(session: str, filename: str, save_path: str) -> int:
    # readAttachment (the same authenticated plugins:call envelope every
    # other Notes action goes through) is the only way to fetch an
    # attachment's bytes -- there is deliberately no plain download URL.
    result = await call_plugin("readAttachment", session, filename=filename)
    content_b64 = (result or {}).get("content")
    if not isinstance(content_b64, str):
        raise NotesApiError(f'Attachment "{filename}" not found (readAttachment returned no content).')
    data = base64.b64decode(content_b64)
    Path(save_path).write_bytes(data)
    return len(data)


# ---------------------------------------------------------------------------
# index.ts equivalent -- the 17 tool handlers.
# ---------------------------------------------------------------------------

async def notes_login(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    email, h16, _session = await _sessions.login(args["email"], args["password"])
    return {"text": f"Logged in as {email} (hash16={h16}). Credentials saved for automatic re-login."}


async def notes_whoami(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    email, h16, _session = await _sessions.ensure_session()
    return {"text": json.dumps({"email": email, "hash16": h16}, indent=2, ensure_ascii=False)}


async def notes_list(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    entries = await _sessions.with_session(lambda session: list_notes(session, folder=args.get("folder"), include_deleted=bool(args.get("includeDeleted"))))
    return {"text": json.dumps([_summarize(e) for e in entries], indent=2, ensure_ascii=False)}


async def notes_search(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    entries = await _sessions.with_session(lambda session: search_notes(session, args["query"], folder=args.get("folder"), include_deleted=bool(args.get("includeDeleted"))))
    return {"text": json.dumps([_summarize(e) for e in entries], indent=2, ensure_ascii=False)}


async def notes_get(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    note = await _sessions.with_session(lambda session: get_note(session, args["id"]))
    return {"text": json.dumps({"id": note["id"], "text": note.get("text", ""), "folder": note.get("folder") or "", "updatedAt": note.get("updatedAt"), "deleted": bool(note.get("deleted"))}, indent=2, ensure_ascii=False)}


async def notes_create(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    note = await _sessions.with_session(lambda session: create_note(session, args["text"], folder=args.get("folder")))
    return {"text": json.dumps(_summarize(note), indent=2, ensure_ascii=False)}


async def notes_update(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    note = await _sessions.with_session(lambda session: update_note(session, args["id"], text=args.get("text"), folder=args.get("folder")))
    return {"text": json.dumps(_summarize(note), indent=2, ensure_ascii=False)}


async def notes_delete(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    note = await _sessions.with_session(lambda session: delete_note(session, args["id"]))
    return {"text": json.dumps(_summarize(note), indent=2, ensure_ascii=False)}


async def notes_move(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    note = await _sessions.with_session(lambda session: move_note(session, args["id"], args["folder"]))
    return {"text": json.dumps(_summarize(note), indent=2, ensure_ascii=False)}


async def notes_list_folders(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    folders = await _sessions.with_session(list_folders)
    return {"text": json.dumps(folders, indent=2, ensure_ascii=False)}


async def notes_create_folder(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda session: create_folder(session, args["path"]))
    return {"text": f'Created folder "{args["path"]}".'}


async def notes_rename_folder(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    count = await _sessions.with_session(lambda session: rename_folder(session, args["oldPath"], args["newPath"]))
    return {"text": f'Renamed "{args["oldPath"]}" to "{args["newPath"]}" ({count} note(s) moved).'}


async def notes_delete_folder(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    count = await _sessions.with_session(lambda session: delete_folder(session, args["path"]))
    return {"text": f'Deleted folder "{args["path"]}" ({count} note(s) soft-deleted).'}


async def notes_attach(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    entry = await _sessions.with_session(lambda session: attach_file(session, args["noteId"], args["filePath"], args.get("originalName")))
    return {"text": json.dumps(entry, indent=2, ensure_ascii=False)}


async def notes_list_attachments(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    entries = await _sessions.with_session(lambda session: list_attachments(session, args.get("noteId")))
    return {"text": json.dumps(entries, indent=2, ensure_ascii=False)}


async def notes_download_attachment(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    _email, _h16, session = await _sessions.ensure_session()
    byte_count = await download_attachment(session, args["filename"], args["savePath"])
    return {"text": f'Downloaded {byte_count} byte(s) to "{args["savePath"]}".'}


async def notes_remove_attachment(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    await _sessions.with_session(lambda session: remove_attachment(session, args["noteId"], args["filename"]))
    return {"text": f'Removed attachment "{args["filename"]}" from note "{args["noteId"]}" and deleted the file.'}


PLUGIN = Plugin(
    name="notes",
    tools=[
        PluginTool(
            "notes_login",
            "One-time login with the user's Squirrel Wisdom email/password. Verifies the credentials, saves "
            "them locally (~/.mcp-notes/credentials.json) so the server can silently re-login on every future "
            "start (sessions expire after 24h idle), and establishes the account for this process. Call again "
            "to switch accounts.",
            {"email": str, "password": str}, notes_login,
        ),
        PluginTool(
            "notes_whoami",
            "Ensures a session is established (auto-login from saved credentials if needed) and reports which "
            "account is active.",
            {}, notes_whoami,
        ),
        PluginTool(
            "notes_list",
            'Lists notes, optionally scoped to an exact folder path ("/"-separated; omit for all folders).',
            {"folder": str | None, "includeDeleted": bool | None}, notes_list,
        ),
        PluginTool(
            "notes_search",
            "Case-insensitive substring search over note text (title + body), optionally scoped to a folder "
            "and its subfolders.",
            {"query": str, "folder": str | None, "includeDeleted": bool | None}, notes_search,
        ),
        PluginTool(
            "notes_get",
            "Fetches a single note's full text and metadata by id.",
            {"id": str}, notes_get,
        ),
        PluginTool(
            "notes_create",
            "Creates a new note. The first line of `text` is treated as the title." + FOLDER_MEMORY_NOTE,
            {"text": str, "folder": str | None}, notes_create,
        ),
        PluginTool(
            "notes_update",
            "Updates a note's text and/or folder. Only provided fields are changed.",
            {"id": str, "text": str | None, "folder": str | None}, notes_update,
        ),
        PluginTool(
            "notes_delete",
            "Soft-deletes a note (marks it deleted; it is never physically removed).",
            {"id": str}, notes_delete,
        ),
        PluginTool(
            "notes_move",
            'Moves a note to a different folder (use folder: "" to move it to the root).',
            {"id": str, "folder": str}, notes_move,
        ),
        PluginTool(
            "notes_list_folders",
            "Lists every folder path that currently has at least one active note (including ancestor folders "
            "implied by nested paths).",
            {}, notes_list_folders,
        ),
        PluginTool(
            "notes_create_folder",
            "Creates an empty folder by writing a hidden marker note. Not needed if you're about to create a "
            "real note in that folder anyway.",
            {"path": str}, notes_create_folder,
        ),
        PluginTool(
            "notes_rename_folder",
            "Renames a folder and moves every note inside it (recursively) to the new path.",
            {"oldPath": str, "newPath": str}, notes_rename_folder,
        ),
        PluginTool(
            "notes_delete_folder",
            "Soft-deletes every note inside a folder, recursively (the folder itself has no separate storage "
            "to delete).",
            {"path": str}, notes_delete_folder,
        ),
        PluginTool(
            "notes_attach",
            "Uploads a local file and attaches it to a note. Effective size limit ~47MB. There is no "
            "downloadable URL for the result -- fetch its bytes with notes_download_attachment instead.",
            {"noteId": str, "filePath": str, "originalName": str | None}, notes_attach,
        ),
        PluginTool(
            "notes_list_attachments",
            "Lists attachments, optionally filtered to a single note. There is no downloadable URL for any of "
            "them -- fetch bytes with notes_download_attachment.",
            {"noteId": str | None}, notes_list_attachments,
        ),
        PluginTool(
            "notes_download_attachment",
            "Downloads an attachment's raw bytes to a local file path. This is the ONLY way to fetch an "
            "attachment's content -- there is no plain downloadable URL for it.",
            {"filename": str, "savePath": str}, notes_download_attachment,
        ),
        PluginTool(
            "notes_remove_attachment",
            "Removes an attachment from a note -- drops it from the attachment list and deletes the uploaded "
            "file itself (the backend does both in one step; there's no way to detach without deleting).",
            {"noteId": str, "filename": str}, notes_remove_attachment,
        ),
    ],
)
