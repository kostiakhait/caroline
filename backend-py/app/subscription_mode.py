"""Resolves which "chat source" a turn uses for the Claude Agent SDK.

Per explicit instruction (2026-09-10): Claude is ONLY ever reached through
the user's own subscription -- own-Anthropic OAuth (`claude` CLI login),
or a manually-pasted own-Anthropic API key. The SquirrelWisdom proxy is no
longer a Claude backend at all (it stays a client for Notes/email/ratatosk/
voice/consult -- those hit SW's own services, not the Agent SDK). There is
no cross-source fallback anymore: if the user's own quota is exhausted the
tab just shows "limited" and keeps retrying.

Also here: Options.env construction, SW wallet status (for the auxiliary
ai:* services), and top-up checkout.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Literal

import claude_agent_sdk
import httpx

# Our own subprocess spawns run under pythonw.exe, which has NO console of
# its own -- Windows auto-allocates a brand-new console window for any
# console-subsystem child (claude.exe included) unless this flag is passed
# explicitly. Confirmed live (2026-09-09): without it, every _run_loop()
# restart (dehydration forces one after EVERY turn, across every open tab)
# flashed a visible console window open-then-closed for this exact "claude
# auth status" call. getattr(..., 0) keeps this a no-op on non-Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

from app.logging_setup import log_event
from app.plugins.notes_api import load_credentials, verify_password
from app.plugins.sw_api import API_URL, mint_v2_session

SQUIRRELWISDOM_ORIGIN = "https://www.squirrelwisdom.com"

# Scoped key for "caroline-desktop" (scopes: user:verify, wallet:getBalance)
# -- narrow grant, not the account password. Only used now for the wallet
# status/top-up UI; NOT for reaching Claude.
SW_SERVICE_KEY = "fytZDwOTaBo8I173IS2DaY_qgzm0IFvqvnxJGvC5QrE"

ChatSource = Literal["own-anthropic-oauth", "own-anthropic-key", "none"]

# The Python SDK ships its own bundled claude.exe (a sibling package to the
# Node SDK's own copy) -- same binary family, just resolved via this
# package's own install location instead of a node_modules path.
CLAUDE_EXE = Path(claude_agent_sdk.__path__[0]) / "_bundled" / "claude.exe"


class ResolvedMode:
    __slots__ = ("chat_source", "sw_logged_in")

    def __init__(self, chat_source: ChatSource, sw_logged_in: bool) -> None:
        self.chat_source = chat_source
        self.sw_logged_in = sw_logged_in


# --- settings: a manually-pasted ANTHROPIC_API_KEY, alternative to the
# CLI's own OAuth login -----------------------------------------------------

def _settings_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "subscription.json"


def _load_settings(workspace_dir: str) -> dict[str, Any]:
    path = _settings_path(workspace_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log_event("engine", "subscription_settings_load_failed", error=str(exc))
        return {}


def get_own_anthropic_api_key(workspace_dir: str) -> str | None:
    key = _load_settings(workspace_dir).get("ownAnthropicApiKey")
    return key.strip() if isinstance(key, str) and key.strip() else None


def set_own_anthropic_api_key(workspace_dir: str, key: str | None) -> None:
    settings = _load_settings(workspace_dir)
    trimmed = key.strip() if key else None
    if trimmed:
        settings["ownAnthropicApiKey"] = trimmed
    else:
        settings.pop("ownAnthropicApiKey", None)
    path = _settings_path(workspace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


# --- mode resolve ------------------------------------------------------------

async def _has_own_anthropic_oauth(cwd: str) -> bool:
    """`claude auth status` prints JSON ({loggedIn, email,
    subscriptionType, apiProvider}) -- same shape the frontend already
    parses for the Settings UI."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(CLAUDE_EXE), "auth", "status", cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
        stdout, _stderr = await proc.communicate()
        if proc.returncode != 0:
            return False
        status = json.loads(stdout or b"{}")
        return status.get("loggedIn") is True
    except Exception as exc:
        log_event("engine", "has_own_anthropic_oauth_failed", error=str(exc))
        return False


async def resolve_mode(workspace_dir: str, tab_id: str) -> ResolvedMode:
    """own-Anthropic OAuth wins, then a manually-pasted own-Anthropic key,
    else "none". "none" -> the caller (chat_session.py) opens the native
    login window itself before creating query(), since the model can't run
    any tool call at all without a chat source. `tab_id` is unused now
    (kept in the signature so call sites don't churn) -- there's no
    per-tab exhaustion state anymore."""
    sw_logged_in = load_credentials() is not None
    if await _has_own_anthropic_oauth(workspace_dir):
        return ResolvedMode("own-anthropic-oauth", sw_logged_in)
    if get_own_anthropic_api_key(workspace_dir):
        return ResolvedMode("own-anthropic-key", sw_logged_in)
    return ResolvedMode("none", sw_logged_in)


async def build_options_env(workspace_dir: str, mode: ResolvedMode) -> dict[str, str] | None:
    """Options.env REPLACES the subprocess's environment entirely when
    provided (per the SDK's own contract), it does not merge with the
    process environment automatically -- the branch here that returns a
    value spreads os.environ itself; callers must not do it again on top
    of this. own-anthropic-oauth needs nothing (the CLI's own stored
    login is used) -> returns None."""
    if mode.chat_source == "own-anthropic-key":
        key = get_own_anthropic_api_key(workspace_dir)
        if not key:
            return None  # race: setting was cleared between resolve_mode() and here
        return {**os.environ, "ANTHROPIC_API_KEY": key}
    return None


# --- SW account status -------------------------------------------------------

class SwStatus:
    __slots__ = ("logged_in", "email", "balance_pia", "balance_error")

    def __init__(self, logged_in: bool, email: str | None, balance_pia: float | None, balance_error: str | None) -> None:
        self.logged_in = logged_in
        self.email = email
        self.balance_pia = balance_pia
        self.balance_error = balance_error


async def get_sw_status() -> SwStatus:
    """Backs the Settings "Account & Billing" section -- needs the actual
    PIA balance (a real v2 wallet:getBalance call), not just "is SW login
    configured at all"."""
    creds = load_credentials()
    if not creds:
        return SwStatus(False, None, None, None)
    email = creds["email"]
    try:
        session = await mint_v2_session(creds["email"], creds["password"])
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(API_URL, json={"command": "wallet:getBalance", "key": SW_SERVICE_KEY, "session": session})
            data = res.json()
        if data.get(".status") != "ok":
            return SwStatus(True, email, None, str(data.get(".reason") or "Balance check failed"))
        balance_pia = float((data.get("balances") or {}).get("PIA") or 0)
        return SwStatus(True, email, balance_pia, None)
    except Exception as exc:
        return SwStatus(True, email, None, str(exc))


# --- top-up / pay ------------------------------------------------------------

DEFAULT_TOPUP_AMOUNT_MINOR = 1000  # $10.00
DEFAULT_TOPUP_CURRENCY = "USD"


async def create_topup_checkout_url() -> str:
    """Creates a Revolut-hosted top-up checkout session and returns its
    checkout_url -- the payment viewer window just navigates straight at
    this. Uses the LEGACY session (verify_password's own v1-style
    session, not mint_v2_session) because /revolut/topups is a
    legacy-style REST endpoint resolving ?session= via the old
    Authenticator -- unrelated to the v2 Api2Auth store."""
    creds = load_credentials()
    if not creds:
        raise RuntimeError("Not logged in to SquirrelWisdom.")
    session = await verify_password(creds["email"], creds["password"])
    url = f"{SQUIRRELWISDOM_ORIGIN}/revolut/topups?session={urllib.parse.quote(session)}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(url, json={"currency": DEFAULT_TOPUP_CURRENCY, "amount_minor": DEFAULT_TOPUP_AMOUNT_MINOR, "purpose": "wallet_topup"})
        data = res.json()
    checkout_url = data.get("checkout_url")
    if not checkout_url:
        raise RuntimeError(f"Could not start a top-up: {data}")
    return checkout_url
