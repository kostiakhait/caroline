"""Caroline's own documents live on the user's machine, not on reforce's
storage, so opening one in OnlyOffice first uploads it to a throwaway
random-named caroline_docs/ path (file:write), asks for an editor session for
that path (document:openForEdit), and on close downloads whatever OnlyOffice's
save callback wrote back (file:read) before deleting the temp copy (file:delete).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from pathlib import Path
from typing import Any

from app.logging_setup import log_event
from app.reforce_v2 import ReforceError
from app.reforce_v2 import call as reforce_call

# Bug fix (2026-10-08), confirmed live: this must match the vhost
# reforce_v2.REFORCE_URL actually sends file:write to (beautysqrl.com),
# not just "the squirrelwisdom.com domain" in general -- squirrelwisdom.com
# and beautysqrl.com resolve to the same server IP but are separate nginx
# vhosts with separate document roots. Left pointed at squirrelwisdom.com
# (stale since some earlier point, before/independent of reforce_v2's own
# migration), document:openForEdit built a documentUrl on the WRONG vhost
# -- the file was really sitting in beautysqrl.com's own caroline_docs/,
# so OnlyOffice's own fetch of that URL 404'd ("Download failed."),
# confirmed directly via curl against both.
SQUIRRELWISDOM_ORIGIN = "https://beautysqrl.com"

_CONFIG_FIELDS = ("documentType", "fileType", "editable", "key", "documentUrl", "onlyofficeUrl", "title", "callbackUrl")


class OfficeEditorError(Exception):
    pass


async def prepare_office_edit_session(local_path: str) -> tuple[dict[str, Any], str]:
    path = Path(local_path)
    ext = path.suffix.lstrip(".").lower()
    # The long random component is the only access control on this temp copy;
    # it exists only for one editing session and is deleted in finish_office_edit_session.
    remote_path = f"caroline_docs/{secrets.token_hex(20)}.{ext}"
    log_event("plugin:office-editor", "prepare_edit_session_start", local_path=local_path, remote_path=remote_path)

    content_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    try:
        await reforce_call("file:write", {"path": remote_path, "content": content_b64})
    except ReforceError as exc:
        log_event("plugin:office-editor", "upload_failed", local_path=local_path, remote_path=remote_path, error=str(exc))
        raise OfficeEditorError(str(exc)) from exc
    log_event("plugin:office-editor", "upload_done", remote_path=remote_path, bytes=len(content_b64))

    try:
        edit_res = await reforce_call("document:openForEdit", {"path": remote_path, "title": path.name, "origin": SQUIRRELWISDOM_ORIGIN})
    except ReforceError as exc:
        log_event("plugin:office-editor", "open_for_edit_failed", remote_path=remote_path, error=str(exc))
        raise OfficeEditorError(str(exc)) from exc

    config = {k: edit_res.get(k) for k in _CONFIG_FIELDS}
    log_event("plugin:office-editor", "prepare_edit_session_done", remote_path=remote_path, document_type=config.get("documentType"))
    return config, remote_path


# Bug fix (2026-10-13), confirmed live: a document edited in the viewer
# lost its last edits on close. OnlyOffice's own document editing service
# waits ~5s (plus conversion time, ~10s total -- see OnlyOffice's "Saving
# file" docs) after the editor's connection drops before it decides editing
# has stopped and calls our save-back webhook -- finish_office_edit_session
# used to read the temp copy back IMMEDIATELY on close, racing that delay
# and reading whatever was last saved before it, not the final edit.
# document:forceSave (DocumentCommands.cmdForceSave, reforce-side) makes
# OnlyOffice flush and call the webhook right away instead of waiting for
# that natural timeout, but the request only triggers it -- it still
# returns before the webhook has actually landed and overwritten the temp
# copy. So this still polls file:read a few times, accepting the first
# read whose content differs from what the session started with (or just
# the last attempt, if nothing ever changes -- a no-op edit still needs to
# return eventually).
_FORCE_SAVE_POLL_INTERVAL_S = 1.0
_FORCE_SAVE_MAX_ATTEMPTS = 8


async def finish_office_edit_session(remote_path: str, local_path: str, key: str | None = None) -> str | None:
    """`key` is only passed for an editABLE session (see viewer_plugin.py's
    own open_viewer, which withholds it for a view-only document, e.g. a
    PDF) -- a view-only document was never editable in the first place, so
    there is nothing to force-save or wait on; this keeps that case to its
    original single immediate read, not a pointless multi-second wait for
    a change that can never arrive.

    Issue #6 (2026-10-24): returns None once the document is safely back in
    `local_path`, else what went wrong. The server copy -- the only one with
    the user's edits until then -- is deleted only after the local file is
    written; on any failure (an error answer, no or undecodable content, a
    failed local write) it is kept, and the caller says so."""
    log_event("plugin:office-editor", "finish_edit_session_start", remote_path=remote_path, local_path=local_path)
    try:
        if not key:
            data = await _read_remote(remote_path)
            attempt = 1
        else:
            original_sha = hashlib.sha256(Path(local_path).read_bytes()).hexdigest() if Path(local_path).exists() else None
            try:
                await reforce_call("document:forceSave", {"key": key})
                log_event("plugin:office-editor", "force_save_requested", remote_path=remote_path, key=key)
            except ReforceError as exc:
                log_event("plugin:office-editor", "force_save_failed", remote_path=remote_path, key=key, error=str(exc))
            data = b""
            attempt = 0
            for attempt in range(1, _FORCE_SAVE_MAX_ATTEMPTS + 1):
                data = await _read_remote(remote_path)
                changed = original_sha is None or hashlib.sha256(data).hexdigest() != original_sha
                if changed or attempt == _FORCE_SAVE_MAX_ATTEMPTS:
                    break
                await asyncio.sleep(_FORCE_SAVE_POLL_INTERVAL_S)
        _write_atomically(Path(local_path), data)
        log_event("plugin:office-editor", "synced_back", local_path=local_path, bytes=len(data), attempts=attempt)
    except Exception as exc:  # noqa: BLE001 -- reported to the caller; the server copy stays
        error = f"{type(exc).__name__}: {exc}"
        log_event("plugin:office-editor", "read_back_failed", remote_path=remote_path, local_path=local_path,
                  error=error, remote_copy_kept=True)
        return error
    await _delete_temp_copy(remote_path)
    return None


async def _read_remote(remote_path: str) -> bytes:
    """The server copy's bytes; raises on an error answer or missing/undecodable content."""
    read_res = await reforce_call("file:read", {"path": remote_path})
    content = read_res.get("content") if isinstance(read_res, dict) else None
    if not isinstance(content, str):
        raise ReforceError(f"file:read returned no content for {remote_path}")
    return base64.b64decode(content, validate=True)


def _write_atomically(path: Path, data: bytes) -> None:
    """A failed write leaves the old local file as it was."""
    tmp = path.with_name(path.name + ".caroline-sync")
    tmp.write_bytes(data)
    tmp.replace(path)


async def _delete_temp_copy(remote_path: str) -> None:
    try:
        await reforce_call("file:delete", {"path": remote_path})
        log_event("plugin:office-editor", "temp_copy_deleted", remote_path=remote_path)
    except ReforceError as exc:
        log_event("plugin:office-editor", "temp_copy_delete_failed", remote_path=remote_path, error=str(exc))
