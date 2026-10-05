"""Shared workspace-directory resolution -- ALL tabs currently share one
workspace dir (see main.py's own WORKSPACE_DIR), matching server.ts's
single-workspace-per-install design. Factored out here so a plugin that
needs it (scheduler) doesn't have to import app.main (would create a
circular import: main -> chat_session -> plugins.loader -> a plugin ->
main)."""

from __future__ import annotations

import os
import sys


def _default_workspace_dir() -> str:
    """Linux port (2026-10-05): %LOCALAPPDATA% is a Windows-only env var --
    os.path.expandvars leaves it literally un-expanded on Linux rather than
    raising, which would have silently produced a bogus literal path. XDG
    Base Directory spec's own fallback chain (XDG_DATA_HOME, else
    ~/.local/share) is the direct Linux analog of "per-user local app data"."""
    if sys.platform == "win32":
        return os.path.expandvars(r"%LOCALAPPDATA%\Caroline\workspace")
    xdg_data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(xdg_data_home, "caroline", "workspace")


WORKSPACE_DIR = os.environ.get("CAROLINE_WORKSPACE_DIR", _default_workspace_dir())
