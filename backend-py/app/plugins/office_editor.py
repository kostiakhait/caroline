"""Ports backend/src/officeEditor.ts -- replaces launching a real
soffice.exe process and reparenting its window by hand with the same
OnlyOffice Document Server integration Notes already uses in production.
Caroline's documents live on the user's own machine, not already on
reforce's storage, so this uploads the local file to a throwaway
random-named path first (the legacy "write" command), asks for an editor
session for THAT path (the v2 "document:openForEdit" command -- its own
separate scoped-key auth model, distinct from the legacy APP_KEY used for
verifyPassword/write/read/delete), and on close downloads whatever
OnlyOffice's own save callback wrote back to it before deleting the temp
copy.
"""

from __future__ import annotations

import base64
import re
import secrets
from pathlib import Path
from typing import Any

from app.logging_setup import log_event
from app.plugins.notes_api import APP_KEY as SQUIRRELWISDOM_APP_KEY
from app.plugins.notes_api import _post_json

V2_DOCUMENT_KEY = "m83G0G1mvtfT8gIMecDJY8oUaisIMiyfcgH2gvbDvzU"
SQUIRRELWISDOM_ORIGIN = "https://www.squirrelwisdom.com"

_CONFIG_FIELDS = ("documentType", "fileType", "editable", "key", "documentUrl", "onlyofficeUrl", "title", "callbackUrl")


class OfficeEditorError(Exception):
    pass


async def prepare_office_edit_session(local_path: str) -> tuple[dict[str, Any], str]:
    """Per explicit instruction (2026-09-26), after tracing why open_in_viewer
    required a SquirrelWisdom login at all: it never actually needed one.
    This used to call a _get_session() helper that raised OfficeEditorError
    ("Not logged in to SquirrelWisdom") when no local credentials existed,
    then attached the resulting session to every request below. Read
    against reforce's own source (d:\\REPO\\reforce): the legacy "write"/
    "read"/"delete" commands are authorized by `checkAPIKey` alone
    (Commands.py:507) -- `session` is logged (Commands.py:499-501) but
    never validated, and `_check_chat_membership` (FileCommands.py) only
    even looks at session for chats/{group_id}/... paths, never
    caroline_docs/... ones. The v2 "document:openForEdit" command is
    registered `auth="scope"` (Api2DocumentCommands.py + Api2Dispatcher.py
    process()), which checks only the `key` field via Api2Auth.resolve_key
    -- `session` isn't read at all in that code path either. So the login
    requirement was a Caroline-side habit, not a server-side boundary: the
    real (and only) access control on these temp copies is the long random
    path component below, same model Notes' own short-lived preview temp
    files already use. Session removed from all four calls in this module."""
    path = Path(local_path)
    ext = path.suffix.lstrip(".").lower()
    # Long random component is the only access control on this temp copy,
    # same model as Notes' own short-lived preview temp files -- acceptable
    # since it exists only for the duration of one editing session and is
    # deleted in finish_office_edit_session below.
    remote_path = f"caroline_docs/{secrets.token_hex(20)}.{ext}"
    log_event("plugin:office-editor", "prepare_edit_session_start", local_path=local_path, remote_path=remote_path)

    content_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    write_res = await _post_json({".command": "write", "key": SQUIRRELWISDOM_APP_KEY, "path": remote_path, "content": content_b64})
    if write_res.get(".status") != "ok":
        reason = str(write_res.get(".reason") or "Upload to SquirrelWisdom failed.")
        log_event("plugin:office-editor", "upload_failed", local_path=local_path, remote_path=remote_path, error=reason)
        raise OfficeEditorError(reason)
    log_event("plugin:office-editor", "upload_done", remote_path=remote_path, bytes=len(content_b64))

    edit_res = await _post_json({
        "command": "document:openForEdit", "key": V2_DOCUMENT_KEY,
        "path": remote_path, "title": path.name, "origin": SQUIRRELWISDOM_ORIGIN,
    })
    if edit_res.get(".status") != "ok":
        reason = str(edit_res.get(".reason") or "Could not open document for editing.")
        log_event("plugin:office-editor", "open_for_edit_failed", remote_path=remote_path, error=reason)
        raise OfficeEditorError(reason)

    # reforce's makeResponse() mutates the result dict in place and returns
    # it flat (no nested "result" key) -- edit_res itself carries these
    # fields alongside ".status"/".msgid".
    config = {k: edit_res.get(k) for k in _CONFIG_FIELDS}
    log_event("plugin:office-editor", "prepare_edit_session_done", remote_path=remote_path, document_type=config.get("documentType"))
    return config, remote_path


# Legacy quirk (confirmed live against production, ported as-is): cmdReadFile
# doesn't return a clean {".status":"ok", content:"<base64>"} -- it stuffs
# the path and base64 content INTO the ".status" string itself, delimited
# by "=====" markers.
_READ_STATUS_RE = re.compile(r"^ok\n=====\n([\s\S]*?)\n=====\n([\s\S]*?)\n======\n$")


async def finish_office_edit_session(remote_path: str, local_path: str) -> None:
    log_event("plugin:office-editor", "finish_edit_session_start", remote_path=remote_path, local_path=local_path)
    read_res = await _post_json({".command": "read", "key": SQUIRRELWISDOM_APP_KEY, "path": remote_path})
    status = read_res.get(".status")
    match = _READ_STATUS_RE.match(status) if isinstance(status, str) else None
    if match:
        data = base64.b64decode(match.group(2))
        Path(local_path).write_bytes(data)
        log_event("plugin:office-editor", "synced_back", local_path=local_path, bytes=len(data))
    else:
        log_event("plugin:office-editor", "read_back_unexpected_status", remote_path=remote_path, status=str(status)[:200])
    try:
        await _post_json({".command": "delete", "key": SQUIRRELWISDOM_APP_KEY, "path": remote_path})
        log_event("plugin:office-editor", "temp_copy_deleted", remote_path=remote_path)
    except Exception as exc:
        log_event("plugin:office-editor", "temp_copy_delete_failed", remote_path=remote_path, error=str(exc))
