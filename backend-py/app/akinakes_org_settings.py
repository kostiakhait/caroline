"""Applies the organization's settings to this agent's Caroline before it works.

The organization owns three things, set at registration and changed in akinakes' settings (auth_svc): its SquirrelWisdom
account (email + password), an optional Anthropic API key, and its name. The agent reads them from auth_svc's internal
endpoint (the per-org Docker network is the boundary, no port is published) and writes them where desktop Caroline keeps
the same things: credentials.json (sw_api.save_credentials), subscription.json (subscription_mode), owner_profile.json.
Run at startup and before every task, so a changed key needs no restart.

Role identity comes from env, not from the organization: AGENT_PERSONA_NAME / AGENT_PERSONA_BIO fill persona.json's
"custom" profile. The owner is the organization (its name from auth_svc) unless AKINAKES_OWNER_NAME is set, which is
the person who added the agent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from app.logging_setup import log_event

ENDPOINT = "/api/internal/agent-settings"


def apply_persona_from_env(workspace_dir: str) -> bool:
    name = (os.environ.get("AGENT_PERSONA_NAME") or "").strip()
    if not name:
        return False
    from app.persona import _persona_path  # noqa: PLC2701 -- the file this env is the source of
    custom = {"name": name, "gender": os.environ.get("AGENT_PERSONA_GENDER", "").strip(), "age": os.environ.get("AGENT_PERSONA_AGE", "").strip(), "bio": os.environ.get("AGENT_PERSONA_BIO", "").strip()}
    wanted = {"profileKey": "custom", "custom": custom}
    path: Path = _persona_path(workspace_dir)
    try:
        if json.loads(path.read_text(encoding="utf-8")) == wanted:
            return False
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(wanted, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log_event("plugin:akinakes-org", "persona_written", name=name)
    return True


async def fetch_org_settings() -> dict[str, Any] | None:
    base = (os.environ.get("AUTH_SVC_URL") or "").strip().rstrip("/")
    if not base:
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{base}{ENDPOINT}", headers={"X-Internal-Token": os.environ.get("INTERNAL_API_TOKEN", "")})
        resp.raise_for_status()
        return resp.json()


async def apply_org_settings(workspace_dir: str) -> None:
    settings = await fetch_org_settings()
    if settings is None:
        return
    from app.plugins.sw_api import load_credentials
    from app.login_api import verify_and_save_login

    email = (settings.get("sw_email") or "").strip().lower()
    password = settings.get("sw_password") or ""
    current = load_credentials()
    if email and password and (current is None or current.get("email") != email or (current.get("password") and current.get("password") != password)):
        result = await verify_and_save_login(email, password)
        log_event("plugin:akinakes-org", "sw_login_applied", ok=result.ok, error=result.error)

    from app.subscription_mode import get_own_anthropic_api_key, invalidate_claude_auth_status, set_own_anthropic_api_key
    key = (settings.get("anthropic_api_key") or "").strip() or None
    if key != get_own_anthropic_api_key(workspace_dir):
        set_own_anthropic_api_key(workspace_dir, key)
        invalidate_claude_auth_status()
        log_event("plugin:akinakes-org", "anthropic_key_applied", is_set=bool(key))

    from app.owner_profile import get_owner_profile, save_owner_profile
    owner = (os.environ.get("AKINAKES_OWNER_NAME") or settings.get("org_name") or "").strip()
    if owner and get_owner_profile(workspace_dir).name != owner:
        save_owner_profile(workspace_dir, name=owner)
        log_event("plugin:akinakes-org", "owner_applied", owner=owner)
