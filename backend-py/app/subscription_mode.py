"""Ports backend/src/subscriptionMode.ts -- resolves which "chat source"
(own-Anthropic OAuth, a manually-pasted own-Anthropic key, the
SquirrelWisdom proxy, or none) a turn should use, the own-Anthropic
exhaustion fallback bookkeeping, Options.env construction, SW account
status, and top-up checkout creation.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
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

# Same scoped key minted for "caroline-desktop" (scopes: user:verify,
# anthropic:messages, wallet:getBalance) -- narrow grant, not the account
# password, same reasoning as every other hardcoded service key in this
# port.
SW_SERVICE_KEY = "fytZDwOTaBo8I173IS2DaY_qgzm0IFvqvnxJGvC5QrE"

ChatSource = Literal["own-anthropic-oauth", "own-anthropic-key", "sw-proxy", "none"]

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


# --- own-Anthropic exhaustion fallback --------------------------------------
# Own-Anthropic (OAuth, then a manually-pasted key) still always wins over
# the SquirrelWisdom proxy when it's actually USABLE. Once own-Anthropic is
# CONFIRMED exhausted (a real billing_error/rate_limit_event, never
# speculatively), fall back to sw-proxy -- but keep actively re-probing.
#
# Per explicit instruction (2026-09-09, after a live incident where one tab
# sat on a broken sw-proxy connection for minutes while own-Anthropic was
# actually available the whole time -- confirmed live, since this exact
# Claude Code conversation kept working throughout): each tab tracks its
# OWN exhaustion independently (keyed by tab_id, not a single shared
# module-level flag -- the previous design let whichever tab restarted
# first "use up" the one shared recheck window for everyone), and probes
# again on a flat, short, unconditional cadence -- never a long cooldown
# (no 30-minute default, and the SDK's own resetsAt is no longer trusted
# either -- real availability can flap on a much shorter cycle than
# resetsAt claims, and a stale multi-hour block was exactly what caused
# this incident).
OWN_ANTHROPIC_RECHECK_INTERVAL_MS = 90_000  # 90 seconds, per tab, unconditional.

_own_anthropic_blocked_since_by_tab: dict[str, float] = {}


def mark_own_anthropic_exhausted(tab_id: str) -> None:
    """Call once own-Anthropic has actually failed on a real request for
    THIS tab -- never speculatively. Per-tab: does not affect any other
    tab's own probing."""
    _own_anthropic_blocked_since_by_tab[tab_id] = time.time() * 1000
    log_event("engine", "own_anthropic_exhausted", tab_id=tab_id)


def clear_own_anthropic_exhausted(tab_id: str) -> None:
    """Call when THIS tab's session actually resolved to own-Anthropic and
    proved itself alive -- if this turns out to be wrong, the very next
    real request re-blocks it via mark_own_anthropic_exhausted, same
    self-heal as any other misjudged recovery in this module."""
    if tab_id not in _own_anthropic_blocked_since_by_tab:
        return
    log_event("engine", "own_anthropic_recovered", tab_id=tab_id)
    del _own_anthropic_blocked_since_by_tab[tab_id]


async def resolve_mode(workspace_dir: str, tab_id: str) -> ResolvedMode:
    """Neither own-Anthropic nor SW available -> "none": the caller
    (chat_session.py) checks for exactly this chatSource before creating
    query() and proactively opens the native login window itself -- this
    can't be left to the model to notice, since it can't run any tool
    call at all without a chat source to run it with."""
    sw_logged_in = load_credentials() is not None
    now = time.time() * 1000
    blocked_since = _own_anthropic_blocked_since_by_tab.get(tab_id)
    own_anthropic_blocked = blocked_since is not None and now - blocked_since < OWN_ANTHROPIC_RECHECK_INTERVAL_MS
    if own_anthropic_blocked and sw_logged_in:
        return ResolvedMode("sw-proxy", sw_logged_in)
    # Blocked but no SW to fall back to -- nothing to lose by trying
    # own-Anthropic anyway below, same as if it were never blocked.
    if await _has_own_anthropic_oauth(workspace_dir):
        return ResolvedMode("own-anthropic-oauth", sw_logged_in)
    if get_own_anthropic_api_key(workspace_dir):
        return ResolvedMode("own-anthropic-key", sw_logged_in)
    if sw_logged_in:
        return ResolvedMode("sw-proxy", sw_logged_in)
    return ResolvedMode("none", sw_logged_in)


async def build_options_env(workspace_dir: str, mode: ResolvedMode) -> dict[str, str] | None:
    """Options.env REPLACES the subprocess's environment entirely when
    provided (per the SDK's own contract), it does not merge with the
    process environment automatically -- every branch here that returns a
    value spreads os.environ itself; callers must not do it again on top
    of this."""
    if mode.chat_source == "own-anthropic-key":
        key = get_own_anthropic_api_key(workspace_dir)
        if not key:
            return None  # race: setting was cleared between resolve_mode() and here
        return {**os.environ, "ANTHROPIC_API_KEY": key}
    if mode.chat_source == "sw-proxy":
        creds = load_credentials()
        if not creds:
            return None
        session = await mint_v2_session(creds["email"], creds["password"])
        return {**os.environ, "ANTHROPIC_BASE_URL": SQUIRRELWISDOM_ORIGIN, "ANTHROPIC_API_KEY": f"{SW_SERVICE_KEY}.{session}"}
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
