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

import base64
import os
from pathlib import Path
from typing import Any

from app.plugins.loader import Plugin, PluginTool

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

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


async def describe_local_image(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    """Per explicit instruction (2026-09-15): describe_image_cheap
    (voice_api.py) used to be wired up ONLY for app_browser's own
    screenshots -- not a deliberate restriction, just never generalized.
    Confirmed live as a real, costly gap: with no cheap way to check an
    arbitrary local image file, the model fell back to the native Read
    tool for routine "did this save correctly" checks -- Read puts the
    FULL image (raw base64) into Claude's own context permanently (a
    single real conversation accumulated ~48MB across 41 such reads this
    way). This is the same cheap Camerlengo ai:describeImage call
    app_browser_describe already uses, just for any local file instead of
    only a live embedded-browser screenshot."""
    p = Path(args["path"])
    if not p.exists():
        return {"text": f"No such file: {p}", "is_error": True}
    if p.suffix.lower() not in _IMAGE_EXT:
        return {"text": f"Not a recognized image file (by extension): {p}", "is_error": True}
    from app.plugins.voice_api import describe_image_cheap

    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    try:
        result = await describe_image_cheap(b64)
    except Exception as exc:
        return {"text": f"Could not describe {p}: {exc}", "is_error": True}
    return {"text": str(result.get("description") or "(no description returned)")}


MAX_DOCUMENT_PAGES_PER_CALL = 15


async def read_document_pages(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    """Per explicit instruction (2026-09-15): "Большие многостраничные
    документы должны анализировать по частям... Никогда документ целиком"
    -- the same reasoning as describe_local_image's own docstring, for
    multi-page documents instead of images: the native Read tool hands
    the model a WHOLE PDF's raw bytes/rendered pages at once with no
    per-page control, which is exactly what silently produced the
    41-image/~48.5MB context-bloat incident this same day (images there,
    but the identical failure shape applies to documents). Use this
    instead -- extracts and returns ONLY the requested page range's plain
    text, nothing else ever touches your context."""
    p = Path(args["path"])
    if not p.exists():
        return {"text": f"No such file: {p}", "is_error": True}
    if p.suffix.lower() != ".pdf":
        return {"text": f"Not a PDF (by extension): {p} -- use read_file for plain text or describe_local_image for images.", "is_error": True}
    page_start = int(args.get("page_start") or 1)
    requested_count = args.get("page_count")
    page_count = min(int(requested_count), MAX_DOCUMENT_PAGES_PER_CALL) if requested_count else MAX_DOCUMENT_PAGES_PER_CALL
    from app.pdf_pages import extract_pdf_page_texts, format_pages_for_model, PdfPageError

    try:
        page_texts, total_pages = extract_pdf_page_texts(p.read_bytes(), page_start=page_start, page_count=page_count)
    except PdfPageError as exc:
        return {"text": f"Could not read {p}: {exc}", "is_error": True}
    if not page_texts:
        return {"text": f"{p} has {total_pages} page(s) -- nothing at page {page_start}."}
    text = format_pages_for_model(page_texts, page_start, total_pages)
    next_page = page_start + len(page_texts)
    if next_page <= total_pages:
        text += f"\n\n[{total_pages - next_page + 1} more page(s) beyond this range -- call again with page_start={next_page} to continue.]"
    return {"text": text}


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
        PluginTool(
            "describe_local_image",
            "Get a cheap text description of a local image file (png/jpg/gif/webp/bmp) WITHOUT putting the "
            "actual image bytes into your own context -- prefer this over the Read tool whenever you're just "
            "sanity-checking that a file saved correctly, looks roughly right, or matches what you expect "
            "(e.g. after generating or saving an image). It costs a separate, cheap call instead of your own "
            "context/tokens, but Read is a real, unavoidable cost every time and stays in your context for the "
            "rest of the conversation. Only reach for Read on an image when you genuinely need to look closely "
            "yourself (fine visual detail, exact colors/layout, reading small text in the image) -- something a "
            "text description can't give you.",
            {"path": str}, describe_local_image,
        ),
        PluginTool(
            "read_document_pages",
            "Read a local multi-page PDF's TEXT ONLY, a limited page range at a time (default up to "
            f"{MAX_DOCUMENT_PAGES_PER_CALL} pages per call, starting at page_start, default 1) -- prefer this over "
            "the Read tool for any PDF, especially a multi-page one: Read hands you the whole document's raw "
            "bytes/rendered pages at once, permanently, with no per-page control, which is exactly what causes "
            "runaway context growth on any document of real size. Call it again with a later page_start to keep "
            "reading further pages -- the result tells you how many pages remain. Scanned/image-only pages "
            "return no extractable text (not OCR'd).",
            {"path": str, "page_start": int | None, "page_count": int | None}, read_document_pages,
        ),
    ],
)
