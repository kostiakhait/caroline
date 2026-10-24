"""The "continuous dialogue" setting (2026-10-24, explicit instruction): while it is on, a message the
user sends -- typed as well as spoken -- is a voice turn: the answer is spoken (or shown by the animated
window when Visual Mode is on) and the continuous narration runs, exactly as for a message sent with
the microphone. Off by default. One switch for the whole app, kept in the workspace like the Visual
Mode switch (visual_mode.py); changed from the Settings panel (control ops continuous_dialogue_get/set)
and from the REST endpoint /api/continuous-dialogue."""

from __future__ import annotations

import json
from pathlib import Path

from app.logging_setup import log_event


def _path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "continuousDialogue.json"


def is_enabled(workspace_dir: str) -> bool:
    try:
        return bool(json.loads(_path(workspace_dir).read_text(encoding="utf-8")).get("enabled"))
    except (OSError, ValueError):
        return False


def set_enabled(workspace_dir: str, enabled: bool) -> None:
    path = _path(workspace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabled": bool(enabled)}), encoding="utf-8")
    log_event("engine", "continuous_dialogue_set", enabled=bool(enabled))
