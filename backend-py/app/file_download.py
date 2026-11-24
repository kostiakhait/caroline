"""file_download: a file Caroline gives the person, downloaded straight from the chat (2026-11-24).

Per explicit instruction: when Caroline hands over a file, the chat must carry a link to download it. chat.js renders
every [name](file:///path) link with a download button; the click asks for the bytes with the control op
file_download over the tab's own WebSocket -- the one channel every host has (the Ferret editor reaches her backend
only through it) -- in chunks, so a big file never makes one huge frame.

A link's path comes from Markdown: `file:///C:/x/y.docx` on Windows, but `file:///var/lib/x.fex` on Linux arrives as
`var/lib/x.fex` (the third slash is the link's own); a relative path is taken in her workspace."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

CHUNK_BYTES = 4 * 1024 * 1024


def resolve_path(raw: str, workspace_dir: str) -> Path | None:
    """The existing file a chat link names, or None."""
    text = (raw or "").strip()
    if text.lower().startswith("file://"):
        text = text[len("file://"):]
        if text.startswith("/") and len(text) > 2 and text[2] == ":":
            text = text[1:]                                   # file:///C:/... -> C:/...
    if not text:
        return None
    candidates = [Path(text)]
    if not Path(text).is_absolute():
        candidates += [Path("/" + text), Path(workspace_dir) / text]
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def read_chunk(path: Path, offset: int) -> dict[str, Any]:
    size = path.stat().st_size
    offset = max(0, min(int(offset or 0), size))
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(CHUNK_BYTES)
    return {
        "name": path.name,
        "size": size,
        "offset": offset,
        "mimeType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "dataBase64": base64.b64encode(data).decode("ascii"),
        "done": offset + len(data) >= size,
    }
