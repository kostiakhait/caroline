"""ferret_editor -- Caroline's tools for the Ferret browser editor (ferret/plans/PLAN-PLAYER.md, section 10.3 and 10.6).

Only for that deployment. The module is inert everywhere else: it exposes no PLUGIN unless CAROLINE_FERRET_EDITOR=1 is set in the
environment of the backend process (the hosted backend the editor talks to), so the desktop apps, the Linux build, the Xopek
desktops and the Beroun OS neither load nor see any of it.

The tools work on the project the editor page holds (its files, its compiler, its viewer, the GPU jobs). The backend runs on a
server, so a call crosses to the page: the handler sends the frame {"type": "ferret_tool", "id", "name", "args"} over the tab's own
WebSocket and waits; the page answers with the control op `ferret_tool_result` {requestId, ok, result | error}, which main.py hands
to resolve() below. Nothing else of the page is reachable from here, and nothing outside the project is reachable from the page.

Files the person sends from their own computer (the paperclip of her chat) are saved by the backend in its workspace, `uploads/`; two tools of her
own (not relayed: the bytes are here) bring them into the project: uploads_list names them, project_import_upload sends one to the page, which writes it
into the project as a file (project_write_binary on the page's side).

The tool table is the one of the plan: project files (list, read, search, write, edit, delete, move), the Ferret API (docs, check,
build), the viewer (restart, travel to a level, the engine's commands, its log, a picture) and the GPU jobs. Skills are not here:
Caroline's own skills plugin gives them (use_skill, read_skill_file) from the release's folder (CAROLINE_SKILL_DIRS).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from app.logging_setup import log_event
from app.plugins.loader import Plugin, PluginTool
from app.session_context import get_send

ENABLED = os.environ.get("CAROLINE_FERRET_EDITOR") == "1"
CALL_TIMEOUT_S = 300.0                       # a build or a GPU wait can take minutes; the longest tool waits ~3 minutes itself

_TYPES: dict[str, Any] = {"string": str, "number": float, "boolean": bool, "object": dict, "string[]": list[str], "number[]": list[float]}

# name -> (description, input), the input as {field: "type" or "type?"}
_TABLE: list[tuple[str, str, dict[str, str]]] = [
    ("project_list", "List the files of the open project with their sizes. {dir?} limits to a folder.", {"dir": "string?"}),
    ("project_read", "Read a text file of the project. {path, from?, to?} (1-based line numbers, inclusive). Binary files give only their size.",
     {"path": "string", "from": "number?", "to": "number?"}),
    ("project_search", "Search the text files of the project. {query, regex?, dir?} -> up to 100 matching lines with path and line number.",
     {"query": "string", "regex": "boolean?", "dir": "string?"}),
    ("project_write", "Create a file or replace it whole. {path, content}.", {"path": "string", "content": "string"}),
    ("project_edit", "Replace exact text in a file. {path, old, new, all?}: `old` must occur once unless all is true.",
     {"path": "string", "old": "string", "new": "string", "all": "boolean?"}),
    ("project_delete", "Delete a file (ferret.json cannot be deleted). {path}", {"path": "string"}),
    ("project_move", "Rename or move a file. {from, to}", {"from": "string", "to": "string"}),
    ("api_docs", "The release's Ferret API docs and types. {topic?} lists the topics when omitted.", {"topic": "string?"}),
    ("ferret_check", "Check the project as the compiler does (manifest, levels, code, engine sources refused). -> {ok, issues}", {}),
    ("ferret_build", "Build the .fex of the project without downloading it. -> {ok, bytes, report} or the issues", {"standalone": "boolean?"}),
    ("ferret_levels", "Build a level package (.ferret) of some levels without downloading it. {levels: [names]} -> {ok, name, bytes}",
     {"levels": "string[]"}),
    ("viewer_restart", "Rebuild the project and restart the game in the viewer; waits until it runs or fails. -> {ok, level, errors}", {}),
    ("viewer_level", "Travel the viewer to a level of the project. {level}", {"level": "string"}),
    ("viewer_command", "Run one of the running game's engine commands. {name, args?}; 'commands.list' lists them all.",
     {"name": "string", "args": "object?"}),
    ("viewer_log", "The log under the viewer: build and game errors and messages. {n?} the last n lines (default 60).", {"n": "number?"}),
    ("viewer_screenshot", "A picture of the running game: -> the image", {"width": "number?", "height": "number?"}),
    ("gpu_render_page", "Render a page of the project in a browser with a GPU (WebGL, WebGPU, video) and bring back pictures or a video. "
     "{dir?, entry?, width?, height?, seconds?, fps?, format?, out?}: `dir` is the folder holding the page (default the whole project), "
     "`entry` its HTML file (default index.html); format 'png' (one picture after `seconds`, default) or 'mp4' (a recording of `seconds` "
     "at `fps`); results go to `out` (default renders/). Waits up to ~3 minutes, then answers with the job id for gpu_status.",
     {"dir": "string?", "entry": "string?", "width": "number?", "height": "number?", "seconds": "number?", "fps": "number?",
      "format": "string?", "out": "string?"}),
    ("gpu_video", "Make or convert a video on the GPU (ffmpeg with the hardware encoder). {inputs?: [project paths], images?: "
     "'frames/f_%04d.png' (a numbered picture sequence from the project), output: 'name.mp4', fps?, video?: {codec: "
     "h264|h265|av1|vp9|gif|copy, quality?, scale?: 'WxH', start?, duration?}, audio?: {codec: aac|opus|mp3|copy|none, kbps?}, out?}. "
     "The output goes to `out` (default renders/).",
     {"inputs": "string[]?", "images": "string?", "output": "string", "fps": "number?", "video": "object?", "audio": "object?", "out": "string?"}),
    ("gpu_blender", "Run Blender in the background on the GPU (Cycles/OptiX). {blend?: path of a .blend in the project, script?: path of "
     "a Python script in the project, include?: [extra project paths: textures, models], frame? or range?: [first, last] to render, "
     "engine?: CYCLES|BLENDER_EEVEE_NEXT|BLENDER_WORKBENCH, format?: PNG|JPEG|OPEN_EXR, device?: OPTIX|CUDA|CPU, out?}. A script writes "
     "its files into the folder `out/` (relative path) to get them back, e.g. a glTF export: "
     "bpy.ops.export_scene.gltf(filepath='out/model.glb'). Results go to `out` (default renders/).",
     {"blend": "string?", "script": "string?", "include": "string[]?", "frame": "number?", "range": "number[]?", "engine": "string?",
      "format": "string?", "device": "string?", "out": "string?"}),
    ("gpu_status", "GPU jobs of this person: {id?} one job (and, when it is done, its outputs are brought into the project), or all of "
     "them. -> state queued|starting|running|done|error|cancelled, seconds, usd (what it costs), outputs, log tail.",
     {"id": "string?", "out": "string?"}),
    ("gpu_cancel", "Cancel a GPU job. {id}", {"id": "string"}),
    # Unreal Engine through the importer app on the person's own computer (PLAN-PLAYER 10.7.1)
    ("ue_places", "The person's importer apps (the Ferret UE importer/exporter on their computers): bridge id, computer, online or not, "
     "busy, the installed UE versions and the UE projects found there (path, engine version, maps). If none is online, tell the person to "
     "install it from the editor's menu UE > Download the UE importer/exporter and sign in.", {}),
    ("ue_import", "Import a map of a UE project on the person's computer as a new level of the open project (geometry with baked materials, "
     "environment, collision, physics, landscape). {project: the .uproject path as ue_places gives it, map: '/Game/...', level?: the new "
     "level's name (default the map's name), what?: parts besides geometry (environment, collision, physics, landscape; default all), "
     "bridge?: when several apps are online}. Waits up to ~3 minutes; a longer job answers with its id for ue_status. The level's files "
     "come into levels/<level>/.",
     {"project": "string", "map": "string", "level": "string?", "what": "string[]?", "bridge": "string?"}),
    ("ue_export", "Export a level of the open project into a UE project on the person's computer as a new map (models and textures "
     "imported by Interchange, a PlayerStart per spawn). {level, project: the .uproject path as ue_places gives it, map?: "
     "'/Game/...' (default /Game/Ferret/<level>/<level>), bridge?}. Waits up to ~3 minutes, then answers with the id for ue_status.",
     {"level": "string", "project": "string", "map": "string?", "bridge": "string?"}),
    ("ue_status", "UE jobs started from this editor: {id?} one job (a finished import brings its level into the project), or all. -> state "
     "queued|running|done|error|cancelled, progress, result.", {"id": "string?"}),
    ("ue_cancel", "Stop a UE job. {id}", {"id": "string"}),
]

_waiting: dict[str, asyncio.Future] = {}


def _schema(fields: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, kind in fields.items():
        optional = kind.endswith("?")
        base = _TYPES[kind.rstrip("?")]
        out[name] = (base | None) if optional else base
    return out


def resolve(request_id: str, ok: bool, result: Any = None, error: str | None = None) -> bool:
    """The page's answer to a call (the control op `ferret_tool_result`). False when nothing waits for it any more."""
    future = _waiting.get(request_id)
    if future is None or future.done():
        return False
    future.set_result({"ok": bool(ok), "result": result, "error": error})
    return True


async def _call_page(name: str, args: dict[str, Any]) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _waiting[request_id] = future
    try:
        await get_send()({"type": "ferret_tool", "id": request_id, "name": name, "args": {k: v for k, v in args.items() if v is not None}})
        answer = await asyncio.wait_for(future, CALL_TIMEOUT_S)
    except asyncio.TimeoutError:
        log_event("engine", "ferret_tool_timeout", tool=name)
        return {"text": f"{name}: the editor did not answer within {int(CALL_TIMEOUT_S)} s (is the page still open?)", "is_error": True}
    finally:
        _waiting.pop(request_id, None)
    if not answer["ok"]:
        return {"text": f"{name}: {answer['error'] or 'failed'}", "is_error": True}
    result = answer["result"]
    if isinstance(result, dict) and isinstance(result.get("dataUrl"), str) and result["dataUrl"].startswith("data:image/"):
        header, _, data = result["dataUrl"].partition(",")
        return {"image_base64": data, "mime_type": header[5:].split(";")[0],
                "text": json.dumps({k: v for k, v in result.items() if k != "dataUrl"})}
    return {"text": json.dumps(result, ensure_ascii=False)}


def _tool(name: str, description: str, fields: dict[str, str]) -> PluginTool:
    async def handler(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
        return await _call_page(name, args)
    return PluginTool(name=name, description=description, input_schema=_schema(fields), handler=handler)


# ---- the person's files: what the paperclip of the chat saved on this server -> the project ------------------------------------------------------
MAX_IMPORT_BYTES = 25 * 1024 * 1024         # the chat takes files up to 20 MB
_UPLOAD_PREFIX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-")


def _uploads_dir() -> Path:
    from app import workspace_dir          # read when called: the workspace is the backend's own (CAROLINE_WORKSPACE_DIR)
    return Path(workspace_dir.WORKSPACE_DIR) / "uploads"


def _uploaded() -> list[Path]:
    """The saved files, the newest first."""
    d = _uploads_dir()
    files = [f for f in d.iterdir() if f.is_file()] if d.is_dir() else []
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def _find_upload(ref: str) -> Path:
    """`ref`: the path the chat's note gave, the stored name, or the name the person's file had (the newest with that name)."""
    root = _uploads_dir().resolve()
    files = _uploaded()
    if not ref or not ref.strip():
        raise ValueError("name the file: its name as uploads_list gives it, or the path from the note of the attachment")
    ref = ref.strip()
    p = Path(ref)
    if p.is_absolute():
        r = p.resolve()
        if r.parent == root and r.is_file():
            return r
        raise ValueError(f"\"{ref}\" is not one of the files the person sent (uploads_list names them)")
    for f in files:
        if f.name == ref:
            return f
    for f in files:
        if _UPLOAD_PREFIX.sub("", f.name) == ref:
            return f
    raise ValueError(f"no file \"{ref}\" among the files the person sent: " + (", ".join(_UPLOAD_PREFIX.sub("", f.name) for f in files[:20]) or "none yet"))


async def _uploads_list(_args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    rows = [{"name": _UPLOAD_PREFIX.sub("", f.name), "file": f.name, "bytes": f.stat().st_size,
             "sent": __import__("datetime").datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")} for f in _uploaded()[:100]]
    return {"text": json.dumps({"files": rows}, ensure_ascii=False)}


async def _project_import_upload(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    to = args.get("to")
    try:
        src = _find_upload(str(args.get("upload") or ""))
        size = src.stat().st_size
        if size > MAX_IMPORT_BYTES:
            raise ValueError(f"{_UPLOAD_PREFIX.sub('', src.name)} is {size} bytes: more than {MAX_IMPORT_BYTES} can be brought into the project")
        if not to or str(to).endswith("/"):                                      # no path, or a folder: the file keeps the name the person's computer gave it
            to = str(to or "") + _UPLOAD_PREFIX.sub("", src.name)
        data = base64.b64encode(src.read_bytes()).decode("ascii")
    except (ValueError, OSError) as exc:
        return {"text": f"project_import_upload: {exc}", "is_error": True}
    out = await _call_page("project_write_binary", {"path": to, "dataBase64": data})
    if not out.get("is_error"):
        out["text"] = out["text"][:2000]
    return out


_LOCAL = [
    PluginTool(name="uploads_list", description="The files the person sent from their own computer with the paperclip of the chat (newest first): name, bytes, when. "
               "Images and PDFs you were also shown in the chat; any other kind (a model, a texture, an archive) reaches the project only through project_import_upload.",
               input_schema=_schema({}), handler=_uploads_list),
    PluginTool(name="project_import_upload", description="Put a file the person sent (uploads_list) into the project as it is, bytes and all: {upload, to?}. "
               "`upload` is its name as uploads_list gives it (or the path from the note of the attachment); `to` the project path (default: the file's own name in the "
               "project's root; a path ending in / is a folder such as assets/ or levels/<name>/, the file keeps its name there). Replaces a file of that name. Up to 25 MB.",
               input_schema=_schema({"upload": "string", "to": "string?"}), handler=_project_import_upload),
]

PLUGIN = Plugin(
    name="ferret-editor",
    description="The open Ferret project in the browser editor: its files, check and build, the viewer, GPU jobs",
    always_loaded=True,
    tools=[_tool(*row) for row in _TABLE] + _LOCAL,
) if ENABLED else None
