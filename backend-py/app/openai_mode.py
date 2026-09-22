"""OpenAI as a third answer source next to Claude and SW, run by the Codex
app-server (see engines/codex_engine.py).

This module answers "can this tab use OpenAI right now" and builds the engine's
options. Codex is bundled by the installer (runtime\\codex\\codex-app-server.exe)
and its location is handed to the backend as CAROLINE_CODEX_PATH. Its state
(ChatGPT/device sign-in, threads) lives in the user's own shared ~/.codex, the
same one `codex login` or the ChatGPT desktop app would use -- matching how
Claude's OAuth login is shared machine-wide (see subscription_mode.py).

A pasted API key is a SEPARATE, independent path, exactly like Claude's own
own-anthropic-key: stored only in Caroline's own subscription.json, never
touches ~/.codex/auth.json, and is handed to the spawned codex-app-server
process as the OPENAI_API_KEY environment variable (confirmed live: Codex
picks this up for real requests without needing `codex login`, and it neither
reads nor overwrites the shared ChatGPT sign-in). Priority mirrors Claude's
resolve_mode(): a real ChatGPT/device sign-in wins; the pasted key is only
used as a fallback when there is none.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.durability import openai_transcripts_dir
from app.engines.codex_engine import CodexOptions
from app.logging_setup import log_event
from app.subscription_mode import _load_settings, _settings_path, get_model_override

# Test/dev knob: a JSON list of raw `-c key=value` Codex config overrides, e.g. to
# point Codex at a local model provider. With a custom provider no OpenAI
# sign-in is needed, so its presence also counts as "signed in".
CONFIG_OVERRIDES_ENV = "CAROLINE_CODEX_CONFIG_OVERRIDES"


def codex_exe_path() -> str | None:
    path = os.environ.get("CAROLINE_CODEX_PATH")
    return path if path and Path(path).is_file() else None


def codex_home_dir(workspace_dir: str) -> str:
    """Deliberately the user's own real ~/.codex (CODEX_HOME's documented
    default), NOT a Caroline-private directory -- matches how the Claude
    engine picks up whatever `claude auth login` state already exists on the
    machine (claude auth status reads the same shared ~/.claude regardless of
    which claude.exe invokes it) instead of requiring a separate sign-in.
    workspace_dir is unused now (kept so call sites don't need to change if
    this ever needs to differ per workspace)."""
    override = os.environ.get("CAROLINE_CODEX_HOME")
    if override:
        return override
    return str(Path.home() / ".codex")


def _config_overrides() -> list[str]:
    raw = os.environ.get(CONFIG_OVERRIDES_ENV)
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return [str(v) for v in value] if isinstance(value, list) else []
    except Exception:
        log_event("engine", "codex_config_overrides_invalid")
        return []


def has_chatgpt_or_device_login(workspace_dir: str) -> bool:
    """The shared ~/.codex already has credentials -- from `codex login` run
    directly, the ChatGPT desktop app, or a prior Caroline ChatGPT/device
    sign-in (all write to the same place), or a custom provider is configured
    that needs none."""
    if _config_overrides():
        return True
    return (Path(codex_home_dir(workspace_dir)) / "auth.json").is_file()


def get_own_openai_api_key(workspace_dir: str) -> str | None:
    key = _load_settings(workspace_dir).get("ownOpenaiApiKey")
    return key.strip() if isinstance(key, str) and key.strip() else None


def set_own_openai_api_key(workspace_dir: str, key: str | None) -> None:
    settings = _load_settings(workspace_dir)
    trimmed = key.strip() if key else None
    if trimmed:
        settings["ownOpenaiApiKey"] = trimmed
    else:
        settings.pop("ownOpenaiApiKey", None)
    path = _settings_path(workspace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + chr(10), encoding="utf-8")


# Codex's built-in "openai" provider is reserved (config cannot override it)
# and, confirmed live, only ever authenticates via a ChatGPT/device account --
# it never reads OPENAI_API_KEY, silently sending every request with no
# Authorization header at all when only a bare key is set (a real mistake
# caught by testing with a real key, not assumed: a fake AND a real key both
# produced the identical generic "missing bearer" error, which is what
# exposed this). A custom, non-reserved provider pointed at the same real API
# over plain HTTPS (supports_websockets=false, so it doesn't need the
# ChatGPT-only websocket transport) with env_key declared DOES pick up the
# key -- confirmed live: the real key found in d:\REPO\reforce\Config.py
# reached OpenAI's own billing check ("You have no credits remaining"), a
# response that only a genuinely authenticated request gets.
_API_KEY_PROVIDER_NAME = "openai-key"


def api_key_provider_overrides() -> list[str]:
    provider = (
        f'model_providers.{_API_KEY_PROVIDER_NAME}={{name="{_API_KEY_PROVIDER_NAME}",'
        'base_url="https://api.openai.com/v1",wire_api="responses",env_key="OPENAI_API_KEY",'
        "requires_openai_auth=false,supports_websockets=false}"
    )
    return [provider, f'model_provider="{_API_KEY_PROVIDER_NAME}"']


def resolve_openai_source(workspace_dir: str) -> str:
    """"chatgpt-or-device" (wins if present), "key", or "none" -- same
    priority shape as subscription_mode.resolve_mode."""
    if has_chatgpt_or_device_login(workspace_dir):
        return "chatgpt-or-device"
    if get_own_openai_api_key(workspace_dir):
        return "key"
    return "none"


def has_openai_login(workspace_dir: str) -> bool:
    return resolve_openai_source(workspace_dir) != "none"


def openai_available(workspace_dir: str) -> bool:
    return codex_exe_path() is not None and has_openai_login(workspace_dir)


def openai_unavailable_reason(workspace_dir: str) -> str | None:
    if codex_exe_path() is None:
        return "OpenAI support is not installed (Codex is missing). Re-run the Caroline installer."
    if not has_openai_login(workspace_dir):
        return "Sign in to OpenAI in Settings first."
    return None


def build_codex_options(
    workspace_dir: str, system_prompt: str | None, mcp_servers: dict[str, Any], resume_thread_id: str | None,
) -> CodexOptions:
    exe = codex_exe_path()
    if exe is None:
        raise RuntimeError("Codex is not installed")
    home = codex_home_dir(workspace_dir)
    os.makedirs(home, exist_ok=True)
    extra_env: dict[str, str] = {}
    config_overrides = _config_overrides()
    if resolve_openai_source(workspace_dir) == "key":
        key = get_own_openai_api_key(workspace_dir)
        if key:
            extra_env["OPENAI_API_KEY"] = key
            config_overrides = [*config_overrides, *api_key_provider_overrides()]
    return CodexOptions(
        codex_exe=exe, codex_home=home, cwd=str(Path(workspace_dir).resolve()),
        base_instructions=system_prompt, mcp_servers=mcp_servers,
        model=get_model_override(workspace_dir, "openai"),
        resume_thread_id=resume_thread_id, config_overrides=config_overrides,
        transcript_dir=str(openai_transcripts_dir(workspace_dir)), extra_env=extra_env,
    )
