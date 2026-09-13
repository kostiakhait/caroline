"""files -- ports backend/src/files.ts's open_file tool: opens a local file
in the user's default Windows application, exactly like double-clicking it
in File Explorer. Also exposes generic read_file/write_file (2026-09-13):
the small-model primary path (small_model_engine.py) only ever sees tools
collected here via discover_plugins() -- unlike the full Claude Agent SDK
path, it has no built-in Read/Write/Edit of its own, so without these two
tools it has literally no way to create or read an arbitrary file. Confirmed
live as a real capability gap (2026-09-13): asked to produce a .docx, the
small model correctly reported it had no generic file-write tool at all and
just told the user to paste the text into Word themselves instead of
escalating."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.plugins.loader import Plugin, PluginTool

# Matches the Read tool's own read cap (roughly) so a huge log/data file
# doesn't blow up the model's context in one call -- truncated rather than
# refused, since a partial read is still useful and the model can always
# ask for a further range.
MAX_READ_BYTES = 200_000


def open_file_with_default_app(path: str) -> None:
    os.startfile(path)  # Windows-only, matching this whole codebase's platform.


async def open_file(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    path = args["path"]
    if not Path(path).exists():
        return {"text": f"No such file: {path}", "is_error": True}
    open_file_with_default_app(path)
    return {"text": f"Opened {path} in its default application."}


async def read_file(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    p = Path(args["path"])
    if not p.exists():
        raise FileNotFoundError(f"No such file: {p}")
    if not p.is_file():
        raise IsADirectoryError(f"Not a file: {p}")
    data = p.read_bytes()
    truncated = len(data) > MAX_READ_BYTES
    text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    if truncated:
        text += f"\n\n[... truncated, file is {len(data)} bytes, only the first {MAX_READ_BYTES} shown ...]"
    return {"text": text}


async def write_file(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    p = Path(args["path"])
    content = args["content"]
    append = bool(args.get("append", False))
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a" if append else "w", encoding="utf-8", newline="") as f:
        f.write(content)
    verb = "Appended to" if append else "Wrote"
    return {"text": f"{verb} {p} ({len(content)} characters)."}


async def list_directory(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    p = Path(args["path"])
    if not p.exists():
        raise FileNotFoundError(f"No such directory: {p}")
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {p}")
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    lines = []
    for e in entries:
        try:
            size = e.stat().st_size if e.is_file() else None
        except OSError:
            size = None
        lines.append(f"{'[dir] ' if e.is_dir() else ''}{e.name}" + (f" ({size} bytes)" if size is not None else ""))
    if not lines:
        return {"text": f"{p} is empty."}
    return {"text": "\n".join(lines)}


PLUGIN = Plugin(
    name="files",
    tools=[
        PluginTool(
            "open_file",
            "Open a local file in the user's default Windows application for that file type (image viewer, "
            "video player, PDF reader, Office, etc.) -- exactly like double-clicking it in File Explorer. Use "
            "this when the user asks you to open/show a file that isn't one of your own shipped photos, or "
            "when you want to show them something you just created.",
            {"path": str}, open_file,
        ),
        PluginTool(
            "read_file",
            "Read a local text file's contents. Works on any plain-text file (code, logs, .md/.txt/.csv, etc.). "
            "Large files are truncated with a note rather than refused.",
            {"path": str}, read_file,
        ),
        PluginTool(
            "write_file",
            "Create or overwrite a local text file with the given content, creating any missing parent "
            "directories. Set append=true to add to an existing file instead of replacing it. This is your "
            "general-purpose way to produce a real file on disk (a draft, a data export, a generated document's "
            "source text, etc.) -- use it directly instead of telling the user you can't create files.",
            {"path": str, "content": str, "append": bool | None}, write_file,
        ),
        PluginTool(
            "list_directory",
            "List the files and subdirectories directly inside a local directory (not recursive), with file "
            "sizes. Use this to see what's already there before reading/writing a file, or to find a file "
            "whose exact name you don't know.",
            {"path": str}, list_directory,
        ),
    ],
)
