"""Caroline's own documents live on the user's machine, not on reforce's
storage, so opening one in OnlyOffice first uploads it to a throwaway
random-named caroline_docs/ path (file:write), asks for an editor session for
that path (document:openForEdit), and on close downloads whatever OnlyOffice's
save callback wrote back (file:read) before deleting the temp copy (file:delete).
"""

from __future__ import annotations

import base64
import secrets
from pathlib import Path
from typing import Any

from app.logging_setup import log_event
from app.reforce_v2 import ReforceError
from app.reforce_v2 import call as reforce_call

SQUIRRELWISDOM_ORIGIN = "https://www.squirrelwisdom.com"

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


async def finish_office_edit_session(remote_path: str, local_path: str) -> None:
    log_event("plugin:office-editor", "finish_edit_session_start", remote_path=remote_path, local_path=local_path)
    try:
        read_res = await reforce_call("file:read", {"path": remote_path})
        data = base64.b64decode(read_res.get("content", ""))
        Path(local_path).write_bytes(data)
        log_event("plugin:office-editor", "synced_back", local_path=local_path, bytes=len(data))
    except ReforceError as exc:
        log_event("plugin:office-editor", "read_back_failed", remote_path=remote_path, error=str(exc))
    try:
        await reforce_call("file:delete", {"path": remote_path})
        log_event("plugin:office-editor", "temp_copy_deleted", remote_path=remote_path)
    except ReforceError as exc:
        log_event("plugin:office-editor", "temp_copy_delete_failed", remote_path=remote_path, error=str(exc))
