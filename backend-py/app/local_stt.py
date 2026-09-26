"""local_stt -- optional, fully local speech-to-text via faster-whisper
(CTranslate2), ported from d:/REPO/transcribe/app/transcriber.py's own
model-loading approach (GPU auto-detect, CPU fallback), trimmed to what
Caroline needs: one short voice message at a time, no diarization/chunking.

Off by default (explicit instruction, 2026-09-26: "тумблер в Настройках, по
умолчанию облако") -- is_local_stt_enabled()/set_local_stt_enabled() persist
the toggle the same way visual_mode.py's own enabled flag does. When on, it
is tried FIRST and any failure (model missing, load error, decode error)
falls back to Camerlengo's ai:stt transparently -- the same local-first,
cloud-fallback shape voice_api.py's synthesize_speech already uses for local
TTS (edge-tts before ai:tts).

The model itself ships INSIDE the installer (CarolineInstaller's own
WhisperModelInstaller, unconditionally -- explicit instruction: "зашита в
инсталлятор сразу", not downloaded on first enable), landing at a fixed
path the WPF launcher hands this backend via the CAROLINE_WHISPER_MODEL_PATH
env var (BackendProcess.cs, same pattern as CAROLINE_FFMPEG_PATH). If that
env var is unset or the path doesn't exist -- a dev-tree run, or an install
whose model download was skipped (low disk space, network hiccup, same
best-effort fallback ModelsInstaller.cs already has for Visual Mode's own
models) -- local STT is simply unavailable and the Settings toggle is
disabled/hinted accordingly; nothing here ever requires it.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from app.logging_setup import log_event

_settings_lock = threading.Lock()
_pipeline_lock = threading.Lock()
_pipeline = None  # lazily-loaded faster_whisper.BatchedInferencePipeline


def _settings_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "local-stt-settings.json"


def _load_settings(workspace_dir: str) -> dict:
    path = _settings_path(workspace_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log_event("engine", "local_stt_load_settings_failed", error=str(exc))
        return {}


def is_local_stt_enabled(workspace_dir: str) -> bool:
    """Default false per explicit instruction ("по умолчанию облако")."""
    return bool(_load_settings(workspace_dir).get("enabled", False))


def set_local_stt_enabled(workspace_dir: str, enabled: bool) -> None:
    with _settings_lock:
        _settings_path(workspace_dir).write_text(json.dumps({"enabled": enabled}, indent=2) + "\n", encoding="utf-8")
    log_event("engine", "local_stt_set_enabled", enabled=enabled)


def model_dir() -> Path | None:
    """Where CarolineInstaller's WhisperModelInstaller extracted the model,
    handed down via CAROLINE_WHISPER_MODEL_PATH -- None in a dev-tree run
    (no installer, no env var) or if that path is unset for any other
    reason. Callers must not assume this is set."""
    raw = os.environ.get("CAROLINE_WHISPER_MODEL_PATH")
    return Path(raw) if raw else None


def is_local_stt_available() -> bool:
    """The model is actually present on disk (not just enabled in
    Settings) -- the two are checked separately so the toggle can show
    "unavailable" for an install that skipped the model download, exactly
    like Visual Mode's own available/enabled split."""
    d = model_dir()
    return d is not None and (d / "model.bin").exists()


def _register_cuda_dll_dirs() -> None:
    """The nvidia-*-cu12 pip packages put their DLLs in site-packages/nvidia/*/bin
    without adding those directories to the search path. ctranslate2 may use
    plain LoadLibrary (PATH-based) rather than LoadLibraryEx with
    LOAD_LIBRARY_SEARCH_DEFAULT_DIRS (AddDllDirectory-based), so both are
    covered here -- ported verbatim from d:/REPO/transcribe/app/transcriber.py,
    which found this the hard way."""
    if os.name != "nt":
        return
    import importlib

    extra: list[str] = []
    for pkg in ("nvidia.cuda_runtime", "nvidia.cublas", "nvidia.cudnn"):
        try:
            module = importlib.import_module(pkg)
        except ImportError:
            continue
        pkg_dirs = list(getattr(module, "__path__", []) or [])
        if not pkg_dirs and getattr(module, "__file__", None):
            pkg_dirs = [str(Path(module.__file__).resolve().parent)]
        for pkg_dir in pkg_dirs:
            bin_dir = Path(pkg_dir) / "bin"
            if bin_dir.is_dir():
                bin_str = str(bin_dir)
                extra.append(bin_str)
                try:
                    os.add_dll_directory(bin_str)
                except (OSError, AttributeError):
                    pass
    if extra:
        current = os.environ.get("PATH", "")
        additions = os.pathsep.join(p for p in extra if p not in current)
        if additions:
            os.environ["PATH"] = additions + os.pathsep + current


def _get_pipeline():
    """Loads the model once and keeps it resident for the life of the
    backend process -- a genuinely large model (distil-large-v3, ~1.5GB)
    is not worth reloading per call. device="auto"/compute_type="auto"
    (same as transcribe's own config) pick CUDA when a usable GPU + the
    nvidia-*-cu12 packages are present, CPU otherwise -- no separate
    detection logic needed here, ctranslate2 does it internally."""
    global _pipeline
    with _pipeline_lock:
        if _pipeline is not None:
            return _pipeline
        d = model_dir()
        if d is None or not (d / "model.bin").exists():
            raise RuntimeError("local STT model is not installed")
        _register_cuda_dll_dirs()
        from faster_whisper import BatchedInferencePipeline, WhisperModel  # noqa: PLC0415 -- only imported when actually used

        model = WhisperModel(str(d), device="auto", compute_type="auto")
        _pipeline = BatchedInferencePipeline(model)
        log_event("engine", "local_stt_model_loaded", path=str(d))
        return _pipeline


def _transcribe_sync(audio_bytes: bytes, fmt: str) -> str:
    pipeline = _get_pipeline()
    suffix = f".{fmt}" if fmt and not fmt.startswith(".") else (fmt or ".bin")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name
    try:
        # language=None lets faster-whisper's own VAD-gated auto-detect run
        # (unlike transcribe's pinned-language batch-processing use case,
        # a single short ad-hoc voice message has no reason to assume one
        # fixed language up front).
        segments_iter, _info = pipeline.transcribe(tmp_path, vad_filter=True, batch_size=8)
        return " ".join(s.text.strip() for s in segments_iter).strip()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def transcribe_local(audio_base64: str, fmt: str) -> str:
    """Raises on any failure (model unavailable, decode error, etc.) --
    callers fall back to the cloud path, same contract voice_api.py's
    local TTS attempt already has."""
    import base64

    audio_bytes = base64.b64decode(audio_base64)
    text = await asyncio.to_thread(_transcribe_sync, audio_bytes, fmt)
    if not text:
        raise RuntimeError("local STT produced no text")
    return text
