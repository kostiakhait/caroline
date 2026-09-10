"""Shared workspace-directory resolution -- ALL tabs currently share one
workspace dir (see main.py's own WORKSPACE_DIR), matching server.ts's
single-workspace-per-install design. Factored out here so a plugin that
needs it (scheduler) doesn't have to import app.main (would create a
circular import: main -> chat_session -> plugins.loader -> a plugin ->
main)."""

from __future__ import annotations

import os

WORKSPACE_DIR = os.environ.get(
    "CAROLINE_WORKSPACE_DIR",
    os.path.expandvars(r"%LOCALAPPDATA%\Caroline\workspace"),
)
