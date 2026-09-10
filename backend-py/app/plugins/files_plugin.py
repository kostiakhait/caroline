"""files -- ports backend/src/files.ts's open_file tool: opens a local file
in the user's default Windows application, exactly like double-clicking it
in File Explorer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.plugins.loader import Plugin, PluginTool


def open_file_with_default_app(path: str) -> None:
    os.startfile(path)  # Windows-only, matching this whole codebase's platform.


async def open_file(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    path = args["path"]
    if not Path(path).exists():
        return {"text": f"No such file: {path}", "is_error": True}
    open_file_with_default_app(path)
    return {"text": f"Opened {path} in its default application."}


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
    ],
)
