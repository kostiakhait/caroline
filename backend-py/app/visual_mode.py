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
from app.session_context import get_send

VisualModelSource = Literal["caroline", "peter"]

# Per explicit instruction (2026-10-05): these models are tens of GB and
# account for ~90% of a fresh install's disk footprint, yet most users never
# turn Visual Mode on -- CarolineInstaller used to fetch all four
# unconditionally regardless. Now downloaded on demand, triggered by
# enabling the Settings toggle (see main.py's visual_mode_set), the exact
# same resumable/verified mechanism Windows/Caroline/Native/
# VisualModelDownloader.cs already uses for a purchased model -- just
# pointed at this base instead of the purchasable catalog's CDN (see
# plugins/visual_models_plugin.py's own docstring for why those are
# deliberately separate subtrees of the same host). Matches
# Windows/CarolineInstaller/ModelsInfo.cs's own (now-removed) BaseUrl.
BUILTIN_MODELS_BASE_URL = "https://downloader.multi-portal.org/apps/caroline/models"
BUILTIN_MODEL_NAMES = frozenset({"CarolineA", "CarolineB", "PeterA", "PeterB"})


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
    """Default FALSE (per explicit instruction, 2026-10-05, superseding the
    old "по умолчанию он включен" default) -- the models are tens of GB and
    the installer no longer fetches them unconditionally (see
    BUILTIN_MODELS_BASE_URL's own doc comment), so defaulting to enabled
    would just leave the toggle on with nothing downloaded. Only affects a
    workspace that has never written visualMode.json at all -- an existing
    user's own persisted choice (this file already exists for them) is
    untouched either way."""
    enabled = _load_settings(workspace_dir).get("enabled", False)
    log_event("engine", "visual_mode_is_enabled", enabled=enabled)
    return bool(enabled)


def set_visual_mode_enabled(workspace_dir: str, enabled: bool) -> None:
    log_event("engine", "visual_mode_set_enabled", enabled=enabled)
    data = _load_settings(workspace_dir)
    data["enabled"] = enabled
    data.pop("enableWhenReady", None)  # the choice is made: nothing is waiting for the download any more
    _settings_path(workspace_dir).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def set_enable_when_ready(workspace_dir: str, waiting: bool) -> None:
    """The Settings toggle was turned on while the models were still missing: Visual Mode is switched on once they have
    arrived (see main.py's visual_model_download_done). Kept apart from `enabled`, so that the background download of the
    models (which runs whether or not Visual Mode is on) never switches the feature on by itself."""
    data = _load_settings(workspace_dir)
    if waiting:
        data["enableWhenReady"] = True
    else:
        data.pop("enableWhenReady", None)
    _settings_path(workspace_dir).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def is_enable_when_ready(workspace_dir: str) -> bool:
    return bool(_load_settings(workspace_dir).get("enableWhenReady", False))


def models_dir() -> str:
    """CAROLINE_MODELS_DIR is set by BackendProcess.cs on every spawn -- models
    live as a sibling of the app dir, tens of GB each, installed once by
    CarolineInstaller, never re-downloaded on a routine app update. Falls
    back to the dev-tree-relative guess when the env var is unset/missing --
    covers a plain source-tree dev run. Public (not _models_dir) -- also
    used by visual_models_plugin.py to know where a purchased model's
    downloaded file should land/already exist."""
    from_env = os.environ.get("CAROLINE_MODELS_DIR")
    # (2026-10-27) Used whether or not the folder exists yet: it is created by the first download, and the models now come in
    # the background on a fresh install, where it does not exist at the start. Falling back to the dev-tree guess in that case
    # looked for the finished files in the wrong place, so they were never seen.
    if from_env:
        return from_env
    return str(Path.cwd() / ".." / "art" / "models")


def _active_model_override_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "visual-model-override.json"


def get_active_visual_model_override(workspace_dir: str) -> str | None:
    """A purchased Visual Mode model (visual_models_plugin.py), activated by
    name -- deliberately NOT part of persona.py's ProfileKey/character
    system. Confirmed live (2026-09-23): the purchasable catalog's own
    files have no day-parity A/B pair the way CarolineA/B and PeterA/B do
    (one file per name, e.g. Alice.xcfa) and no bio/gender/name identity
    attached at all -- it's a pure visual skin swap, independent of which
    character (Caroline/Peter/custom) is actually talking. Whatever
    persona.profile_key already is stays exactly as it was; only which
    .xcfa file renders changes."""
    try:
        return json.loads(_active_model_override_path(workspace_dir).read_text(encoding="utf-8")).get("modelName")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def set_active_visual_model_override(workspace_dir: str, model_name: str | None) -> None:
    path = _active_model_override_path(workspace_dir)
    if model_name is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps({"modelName": model_name}, indent=2) + "\n", encoding="utf-8")


def resolve_visual_model(workspace_dir: str) -> dict | None:
    """Which .xcfa model backs Visual Mode right now, or None if unavailable --
    either the profile is "custom" (no model exists for a user-authored
    identity) or the resolved model file isn't actually present on disk.
    Day-parity (even day-of-month -> "A", odd -> "B") is resolved fresh every
    call rather than cached -- deliberately cheap to call repeatedly.

    A purchased-model override (see get_active_visual_model_override) is
    checked FIRST and, if set and its file is actually present, wins
    outright -- it's an independent visual skin, not tied to profile_key at
    all (see that function's own doc comment)."""
    override_name = get_active_visual_model_override(workspace_dir)
    if override_name:
        override_path = Path(models_dir()) / f"{override_name}.xcfa"
        if override_path.exists():
            log_event("engine", "visual_mode_resolve_model", source="purchased", model_name=override_name, model_path=str(override_path), available=True)
            return {"source": "purchased", "variant": None, "modelPath": str(override_path)}
        # Activated but the file isn't actually on disk yet (download still
        # pending/in-flight, or failed) -- fall through to the normal
        # Caroline/Peter resolution below rather than silently going dark.
        log_event("engine", "visual_mode_resolve_model", source="purchased", model_name=override_name, available=False, reason="file_missing")

    persona = get_persona(workspace_dir)
    if persona.profile_key not in ("caroline", "peter"):
        log_event("engine", "visual_mode_resolve_model", profile_key=persona.profile_key, available=False)
        return None

    variant = "A" if datetime.now().day % 2 == 0 else "B"
    file_name = f"{'Caroline' if persona.profile_key == 'caroline' else 'Peter'}{variant}.xcfa"
    model_path = str(Path(models_dir()) / file_name)
    if not Path(model_path).exists():
        log_event("engine", "visual_mode_resolve_model", model_path=model_path, available=False)
        return None

    log_event("engine", "visual_mode_resolve_model", source=persona.profile_key, variant=variant, model_path=model_path, available=True)
    return {"source": persona.profile_key, "variant": variant, "modelPath": model_path}


def is_visual_mode_available(workspace_dir: str) -> bool:
    return resolve_visual_model(workspace_dir) is not None


def profile_supports_visual_mode(workspace_dir: str) -> bool:
    """Whether Visual Mode could EVER apply to the current persona --
    independent of whether the model files are actually downloaded yet (see
    resolve_visual_model for that). Only "custom" has no model at all; for
    caroline/peter, a missing file just means the Settings toggle needs to
    download it first (main.py's visual_mode_set), not that it's
    unavailable."""
    return get_persona(workspace_dir).profile_key in ("caroline", "peter")


def _builtin_names_for_profile(workspace_dir: str) -> list[str]:
    profile_key = get_persona(workspace_dir).profile_key
    if profile_key == "caroline":
        return ["CarolineA", "CarolineB"]
    if profile_key == "peter":
        return ["PeterA", "PeterB"]
    return []


def missing_builtin_models(workspace_dir: str) -> list[str]:
    """Which of the current persona's two day-parity files (both needed --
    see resolve_visual_model's own A/B rotation) aren't on disk yet."""
    base = Path(models_dir())
    return [name for name in _builtin_names_for_profile(workspace_dir) if not (base / f"{name}.xcfa").exists()]


def background_download_order(workspace_dir: str) -> list[str]:
    """All four built-in models, the current persona's own two first (they are the ones Visual Mode needs soonest)."""
    first = _builtin_names_for_profile(workspace_dir)
    return first + sorted(BUILTIN_MODEL_NAMES - set(first))


def missing_background_models(workspace_dir: str) -> list[str]:
    """The built-in models that are not on disk yet, in the order they are fetched in."""
    base = Path(models_dir())
    return [name for name in background_download_order(workspace_dir) if not (base / f"{name}.xcfa").exists()]


def builtin_download_message(name: str) -> dict:
    """The host message that starts the download of one built-in model (the host's downloader resumes a partial file and
    checks the sha256)."""
    return {
        "type": "visual_model_download_start",
        "modelName": name,
        "url": f"{BUILTIN_MODELS_BASE_URL}/{name}.xcfa",
        "shaUrl": f"{BUILTIN_MODELS_BASE_URL}/{name}.xcfa.sha256",
    }


class BackgroundDownloads:
    """The built-in models come to the machine on their own, one after the other, whether or not Visual Mode is turned on
    (they used to come only when the Settings toggle was turned on, so a user who tried it later waited for tens of GB, and
    a machine that never did had no model at all). Started at every start of the backend (once per process, at the moment
    the first window connects) for whatever is not on disk yet; each finished file starts the next. A failed file stops the chain (no network, no disk space): the next
    start of Caroline tries again from where the partial file ended. Files the Settings toggle already started are not
    started twice."""

    def __init__(self) -> None:
        self.queue: list[str] = []
        self.current: str | None = None
        self.skip: set[str] = set()   # being fetched by the toggle (or a flushed pending download) in this process

    def begin(self, workspace_dir: str, already_started: set[str]) -> str | None:
        self.skip = set(already_started)
        self.queue = [n for n in missing_background_models(workspace_dir) if n not in self.skip]
        self.current = None
        return self._next()

    def _next(self) -> str | None:
        self.current = self.queue.pop(0) if self.queue else None
        return self.current

    def done(self, name: str, ok: bool) -> str | None:
        """A model finished (or failed). The name of the next one to start, or None."""
        if name != self.current:
            return None   # not ours: a purchased model, or one the toggle started
        if not ok:
            self.queue.clear()
            self.current = None
            return None
        return self._next()

    def taken_over(self, name: str) -> None:
        """The Settings toggle asks for this one right now: it is fetched on its own, not twice."""
        self.skip.add(name)
        if name in self.queue:
            self.queue.remove(name)


background_downloads = BackgroundDownloads()


def _pending_builtin_downloads_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "visual-mode-pending-builtin-downloads.json"


def load_pending_builtin_downloads(workspace_dir: str) -> list[str]:
    try:
        return json.loads(_pending_builtin_downloads_path(workspace_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_pending_builtin_downloads(workspace_dir: str, names: list[str]) -> None:
    _pending_builtin_downloads_path(workspace_dir).write_text(json.dumps(names, indent=2) + "\n", encoding="utf-8")


def add_pending_builtin_download(workspace_dir: str, name: str) -> None:
    pending = load_pending_builtin_downloads(workspace_dir)
    if name not in pending:
        pending.append(name)
        _save_pending_builtin_downloads(workspace_dir, pending)


def remove_pending_builtin_download(workspace_dir: str, name: str) -> None:
    pending = load_pending_builtin_downloads(workspace_dir)
    if name in pending:
        pending.remove(name)
        _save_pending_builtin_downloads(workspace_dir, pending)


async def trigger_builtin_download_now(name: str) -> bool:
    """Best-effort immediate trigger over this turn's own WS connection, same
    shape/caveats as visual_models_plugin.py's own _try_trigger_download_now
    (never raises -- a headless session with no real websocket just means
    the caller's own pending-list + main.py's startup flush deliver it
    later)."""
    try:
        send = get_send()
        await send({
            "type": "visual_model_download_start",
            "modelName": name,
            "url": f"{BUILTIN_MODELS_BASE_URL}/{name}.xcfa",
            "shaUrl": f"{BUILTIN_MODELS_BASE_URL}/{name}.xcfa.sha256",
        })
        log_event("engine", "visual_model_builtin_download_triggered", model_name=name)
        return True
    except Exception as exc:
        log_event("engine", "visual_model_builtin_download_trigger_failed", model_name=name, error=str(exc))
        return False
