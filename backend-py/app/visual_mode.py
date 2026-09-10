"""Ports backend/src/visualMode.ts -- Settings' Visual Mode toggle plus
resolving which .xcfa talking-head model (if any) backs it for the current
persona. Rendering itself is a separate, untouched feature living in the
WPF shell (VisualModeManager) -- this module only owns the on/off setting
and which model file to point it at.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.logging_setup import log_event
from app.persona import get_persona

VisualModelSource = Literal["caroline", "peter"]


def _settings_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "visualMode.json"


def _load_settings(workspace_dir: str) -> dict:
    path = _settings_path(workspace_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log_event("engine", "visual_mode_load_settings_failed", error=str(exc))
        return {}


def is_visual_mode_enabled(workspace_dir: str) -> bool:
    """Default true per explicit instruction ("по умолчанию он включен")."""
    enabled = _load_settings(workspace_dir).get("enabled", True)
    log_event("engine", "visual_mode_is_enabled", enabled=enabled)
    return bool(enabled)


def set_visual_mode_enabled(workspace_dir: str, enabled: bool) -> None:
    log_event("engine", "visual_mode_set_enabled", enabled=enabled)
    _settings_path(workspace_dir).write_text(json.dumps({"enabled": enabled}, indent=2) + "\n", encoding="utf-8")


def _models_dir() -> str:
    """CAROLINE_MODELS_DIR is set by BackendProcess.cs on every spawn -- models
    live as a sibling of the app dir, tens of GB each, installed once by
    CarolineInstaller, never re-downloaded on a routine app update. Falls
    back to the dev-tree-relative guess when the env var is unset/missing --
    covers a plain source-tree dev run."""
    from_env = os.environ.get("CAROLINE_MODELS_DIR")
    if from_env and Path(from_env).exists():
        return from_env
    return str(Path.cwd() / ".." / "art" / "models")


def resolve_visual_model(workspace_dir: str) -> dict | None:
    """Which .xcfa model backs Visual Mode right now, or None if unavailable --
    either the profile is "custom" (no model exists for a user-authored
    identity) or the resolved model file isn't actually present on disk.
    Day-parity (even day-of-month -> "A", odd -> "B") is resolved fresh every
    call rather than cached -- deliberately cheap to call repeatedly."""
    persona = get_persona(workspace_dir)
    if persona.profile_key not in ("caroline", "peter"):
        log_event("engine", "visual_mode_resolve_model", profile_key=persona.profile_key, available=False)
        return None

    variant = "A" if datetime.now().day % 2 == 0 else "B"
    file_name = f"{'Caroline' if persona.profile_key == 'caroline' else 'Peter'}{variant}.xcfa"
    model_path = str(Path(_models_dir()) / file_name)
    if not Path(model_path).exists():
        log_event("engine", "visual_mode_resolve_model", model_path=model_path, available=False)
        return None

    log_event("engine", "visual_mode_resolve_model", source=persona.profile_key, variant=variant, model_path=model_path, available=True)
    return {"source": persona.profile_key, "variant": variant, "modelPath": model_path}


def is_visual_mode_available(workspace_dir: str) -> bool:
    return resolve_visual_model(workspace_dir) is not None
