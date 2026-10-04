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

import base64
import uuid
from pathlib import Path
from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.plugins.office_editor import OfficeEditorError, prepare_office_edit_session
from app.policies import close_windows_after_task_instruction, read_content_not_headers_instruction
from app.session_context import get_send, get_tab_id
from app.window_registry import register_window, unregister_window

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_VIDEO_EXT = {".mp4", ".webm", ".mov", ".avi", ".mkv"}
_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
_CODE_LANGUAGE = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".json": "json", ".cs": "csharp", ".html": "html",
    ".css": "css", ".md": "markdown", ".xml": "xml", ".sql": "sql", ".sh": "shell", ".ps1": "powershell",
    ".go": "go", ".rs": "rust", ".java": "java", ".cpp": "cpp", ".c": "c", ".yml": "yaml", ".yaml": "yaml",
}
_MAX_SLIDES = 50
_MAX_SLIDESHOW_BYTES = 20 * 1024 * 1024

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
    purpose = args["purpose"]
    if not Path(path).exists():
        return {"text": f"No such file: {path}", "is_error": True}
    request_id = uuid.uuid4().hex
    kind = _kind_of(path)
    send = get_send()

    if kind == "document":
        # Per explicit instruction (2026-09-26): this used to gate on
        # require_sw_or_prompt (SquirrelWisdom login) before even trying --
        # removed once tracing prepare_office_edit_session down into
        # reforce's own source (see that function's docstring) confirmed
        # none of the calls it makes actually require a logged-in session
        # server-side. OfficeEditorError below still surfaces a real
        # failure (e.g. reforce itself unreachable) plainly to the model.
        try:
            config, remote_path = await prepare_office_edit_session(path)
        except OfficeEditorError as exc:
            return {"text": f"Could not open {path} for editing: {exc}", "is_error": True}
        _open_requests[request_id] = {"path": path, "remotePath": remote_path}
        await send({"type": "open_office_editor", "requestId": request_id, "path": path, "config": config})
    else:
        _open_requests[request_id] = {"path": path}
        await send({"type": "open_editor", "requestId": request_id, "path": path, "kind": kind})

    register_window(f"viewer:{path}", kind=f"viewer_{kind}", label=path, purpose=purpose, tab_id=get_tab_id())
    return {"text": f"Opened {path} in the viewer window."}


async def close_viewer(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    send = get_send()
    await send({"type": "close_editor", "path": args["path"]})
    unregister_window(f"viewer:{args['path']}")
    return {"text": f"Closed the viewer for {args['path']}."}


async def show_code(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    path = str(args.get("path") or "")
    code = args.get("code")
    language = str(args.get("language") or "")
    title = str(args.get("title") or "")
    if path:
        file_path = Path(path)
        if not file_path.is_file():
            return {"text": f"No such file: {path}", "is_error": True}
        code = file_path.read_text(encoding="utf-8", errors="replace")
        title = title or file_path.name
        language = language or _CODE_LANGUAGE.get(file_path.suffix.lower(), "plaintext")
    elif code is None:
        return {"text": "Pass either path (to view and edit a file) or code (to view text).", "is_error": True}
    language = language or "plaintext"
    request_id = uuid.uuid4().hex
    send = get_send()
    await send({
        "type": "open_editor", "requestId": request_id, "path": path, "kind": "code",
        "title": title, "language": language, "code": str(code), "editable": bool(path),
    })
    register_window(f"viewer:code:{path or request_id}", kind="viewer_code", label=title or "code", purpose="showing code", tab_id=get_tab_id())
    return {"text": f"Opened the code view for {title or 'the snippet'}" + (" (editable; saving writes the file)." if path else " (read-only).")}


async def show_slideshow(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    paths = args.get("paths")
    if not isinstance(paths, list) or not paths:
        return {"text": "paths must be a non-empty list of image file paths.", "is_error": True}
    if len(paths) > _MAX_SLIDES:
        return {"text": f"A slideshow can hold at most {_MAX_SLIDES} images.", "is_error": True}
    images: list[dict[str, str]] = []
    total = 0
    for raw in paths:
        file_path = Path(str(raw))
        mime = _IMAGE_MIME.get(file_path.suffix.lower())
        if mime is None:
            return {"text": f"Not a supported image file: {raw}", "is_error": True}
        if not file_path.is_file():
            return {"text": f"No such file: {raw}", "is_error": True}
        data = file_path.read_bytes()
        total += len(data)
        if total > _MAX_SLIDESHOW_BYTES:
            return {"text": f"The images together exceed {_MAX_SLIDESHOW_BYTES // (1024 * 1024)} MB.", "is_error": True}
        images.append({"name": file_path.name, "dataUrl": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"})
    try:
        interval = max(0.0, min(float(args.get("intervalSec") or 0), 3600.0))
    except (TypeError, ValueError):
        interval = 0.0
    request_id = uuid.uuid4().hex
    send = get_send()
    await send({
        "type": "open_editor", "requestId": request_id, "path": "", "kind": "slideshow",
        "title": f"Slideshow ({len(images)})", "images": images, "intervalSec": interval,
    })
    register_window(f"viewer:slideshow:{request_id}", kind="viewer_slideshow", label=f"slideshow ({len(images)})", purpose="showing a slideshow", tab_id=get_tab_id())
    return {"text": f"Showing {len(images)} image(s) in the slideshow window" + (f", advancing every {interval:g}s." if interval else ".")}


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
            "an embedded OnlyOffice editor -- this requires a working internet connection, since the document "
            "is briefly uploaded to a throwaway temp path to be edited and synced back (no SquirrelWisdom "
            "login needed). Returns immediately -- it does not wait for them to finish, since that could take "
            "a while. purpose is a short note on why you're opening it (e.g. \"showing the user the generated "
            "invoice\") -- recorded so list_my_windows can later tell you (or the user) what this window is "
            "for and why it's still open.",
            {"path": str, "purpose": str}, open_in_viewer,
        ),
        PluginTool(
            "close_viewer",
            "Close the floating viewer window for a file you previously opened with open_in_viewer, without "
            "waiting for the user to do it themselves. For a document open for editing, this closes it the "
            "same way clicking Cancel does -- unsaved changes are discarded. Use this when you no longer need "
            "it open.",
            {"path": str}, close_viewer,
        ),
        PluginTool(
            "show_code",
            "Show code in a syntax-highlighted editor window. Pass path to view a file -- it becomes editable and "
            "Save writes the changes back to that file (the previous version is kept as <file>.bak). Pass code "
            "instead to show a snippet read-only. language is optional (inferred from the file extension).",
            {"path": str | None, "code": str | None, "language": str | None, "title": str | None}, show_code,
        ),
        PluginTool(
            "show_slideshow",
            "Show a list of local image files as a slideshow window with previous/next controls. intervalSec, if "
            "given, advances automatically every that many seconds. Up to 50 images, 20 MB total.",
            {"paths": list, "intervalSec": float | None}, show_slideshow,
        ),
    ],
)
