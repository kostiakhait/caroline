"""viewer -- ports backend/src/viewer.ts. Opens a local file in Caroline's
own floating viewer/editor window (not the OS default app -- see
files_plugin.py's open_file for that): images/video display directly;
office documents (docx/xlsx/pptx/pdf) open for real editing via an
embedded OnlyOffice editor (office_editor.py).

Returns immediately once the window is open -- it does NOT wait for the
user to finish, same reasoning as the original: blocking the turn on a
document edit that might sit open for a long time would freeze the whole
conversation. The real outcome arrives later as a separate "editor_result"
WS control op once the window closes (see take_viewer_request, wired up
in main.py's handle_control_request).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.plugins.office_editor import OfficeEditorError, prepare_office_edit_session
from app.policies import close_windows_after_task_instruction, read_content_not_headers_instruction
from app.session_context import get_send
from app.sw_gate import require_sw_or_prompt

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_VIDEO_EXT = {".mp4", ".webm", ".mov", ".avi", ".mkv"}

_open_requests: dict[str, dict[str, Any]] = {}


def take_viewer_request(request_id: str) -> dict[str, Any] | None:
    return _open_requests.pop(request_id, None)


def _kind_of(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _VIDEO_EXT:
        return "video"
    return "document"


async def open_in_viewer(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    path = args["path"]
    if not Path(path).exists():
        return {"text": f"No such file: {path}", "is_error": True}
    request_id = uuid.uuid4().hex
    kind = _kind_of(path)
    send = get_send()

    if kind == "document":
        gate = await require_sw_or_prompt(send)
        if not gate.ok:
            return {"text": gate.message, "is_error": True}
        try:
            config, remote_path = await prepare_office_edit_session(path)
        except OfficeEditorError as exc:
            return {"text": f"Could not open {path} for editing: {exc}", "is_error": True}
        _open_requests[request_id] = {"path": path, "remotePath": remote_path}
        await send({"type": "open_office_editor", "requestId": request_id, "path": path, "config": config})
    else:
        _open_requests[request_id] = {"path": path}
        await send({"type": "open_editor", "requestId": request_id, "path": path, "kind": kind})

    return {"text": f"Opened {path} in the viewer window."}


async def close_viewer(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    send = get_send()
    await send({"type": "close_editor", "path": args["path"]})
    return {"text": f"Closed the viewer for {args['path']}."}


def _usage_instructions() -> str:
    return "\n\n".join((read_content_not_headers_instruction(), close_windows_after_task_instruction()))


PLUGIN = Plugin(
    name="viewer",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "open_in_viewer",
            "Open a local image, video, or document in Caroline's own floating viewer window (separate from "
            "the chat). Images/video just display; documents (docx/xlsx/pptx/pdf) open for real editing via "
            "an embedded OnlyOffice editor -- this requires the user to be logged into SquirrelWisdom and a "
            "working internet connection, since the document is briefly uploaded there to be edited and "
            "synced back. Returns immediately -- it does not wait for them to finish, since that could take "
            "a while.",
            {"path": str}, open_in_viewer,
        ),
        PluginTool(
            "close_viewer",
            "Close the floating viewer window for a file you previously opened with open_in_viewer, without "
            "waiting for the user to do it themselves. For a document open for editing, this closes it the "
            "same way clicking Cancel does -- unsaved changes are discarded. Use this when you no longer need "
            "it open.",
            {"path": str}, close_viewer,
        ),
    ],
)
