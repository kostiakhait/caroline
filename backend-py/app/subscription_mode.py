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

from app.account_state import CachedAsyncValue
from app.logging_setup import log_event
from app.plugins.notes_api import load_credentials, verify_password
from app.plugins.sw_api import API_URL, mint_v2_session
from app.workspace_dir import WORKSPACE_DIR

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

# --- cached account state ----------------------------------------------------
#
# See app/account_state.py's docstring for the incident and the design: the
# Claude login state and the SW balance are computed ONCE (single-flight),
# kept warm in the background, and every reader -- Settings' auth_status/
# mode_get/chat_mode_get/sw_status, and every tab's session start -- just
# reads the cache instead of spawning its own `claude auth status` / making
# its own network call from scratch.

# How old a cached value may be before a reader triggers a background refresh
# (readers still get the old value INSTANTLY -- stale-while-revalidate).
AUTH_STATUS_TTL_S = 120.0
SW_STATUS_TTL_S = 60.0
# How often the background refresher re-checks both, so the cache is warm
# before anyone asks (Settings opening, a tab starting).
ACCOUNT_STATE_REFRESH_INTERVAL_S = 120.0
# `claude auth status` normally answers in ~3 s; anything past this is a
# wedged CLI, not a slow one -- killed (see cli_control._run), and the last
# good value stays in place (see account_state's stale-on-error).
AUTH_STATUS_TIMEOUT_S = 45.0


async def _fetch_claude_auth_status() -> dict[str, Any]:
    from app.cli_control import auth_status as cli_auth_status_raw

    return await cli_auth_status_raw(WORKSPACE_DIR, AUTH_STATUS_TIMEOUT_S)


_claude_auth_status_cache: CachedAsyncValue[dict[str, Any]] = CachedAsyncValue("claude_auth_status", _fetch_claude_auth_status)


async def get_claude_auth_status(*, serve_stale: bool = True) -> dict[str, Any]:
    """The raw `claude auth status` result ({code, stdout, stderr}), from the
    cache. Raises only if there is no value at all AND the very first fetch
    failed."""
    return await _claude_auth_status_cache.get(max_age_s=AUTH_STATUS_TTL_S, serve_stale=serve_stale)


def invalidate_claude_auth_status() -> None:
    """Call right after the user logs in or out of Claude on purpose --
    hard, so nothing shows the old state even for a moment."""
    _claude_auth_status_cache.invalidate(hard=True)


async def _has_own_anthropic_oauth(cwd: str) -> bool:
    """`claude auth status` prints JSON ({loggedIn, email,
    subscriptionType, apiProvider}) -- same shape the frontend already
    parses for the Settings UI. `cwd` is unused now: the login state is
    machine-wide, one cache serves every caller."""
    try:
        r = await get_claude_auth_status()
        if r.get("code") != 0:
            return False
        status = json.loads(r.get("stdout") or "{}")
        return status.get("loggedIn") is True
    except Exception as exc:
        log_event("engine", "has_own_anthropic_oauth_failed", error=str(exc))
        return False


async def chat_mode_eligible(workspace_dir: str, tab_id: str) -> bool:
    """Gates the per-tab "work via sw or claude" Settings toggle (see
    durability.py's load_chat_mode/save_chat_mode): per explicit
    instruction (2026-09-14), the toggle is only usable when BOTH
    subscriptions are active -- a real Claude subscription (own-anthropic
    OAuth or a pasted key; the small-model path still needs the full SDK
    available as its escalation target) AND a PAID SquirrelWisdom account
    (logged in with a positive PIA balance, not just logged in -- the
    small-model/Camerlengo path bills PIA per call, so a zero balance
    means "sw" mode would just fail immediately)."""
    mode = await resolve_mode(workspace_dir, tab_id)
    if mode.chat_source not in ("own-anthropic-oauth", "own-anthropic-key"):
        return False
    sw = await get_sw_status()
    return sw.logged_in and (sw.balance_pia or 0) > 0


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


async def _fetch_sw_status() -> SwStatus:
    """The real work (a session mint plus a v2 wallet:getBalance call --
    measured >= 9 s), uncached; see get_sw_status for the cached entry
    point everything else uses. A failure RAISES here (rather than being
    packaged as a SwStatus with balance_error) so the cache keeps the last
    GOOD balance instead of replacing it with an error for a whole TTL."""
    creds = load_credentials()
    if not creds:
        return SwStatus(False, None, None, None)
    email = creds["email"]
    session = await mint_v2_session(creds["email"], creds["password"])
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.post(API_URL, json={"command": "wallet:getBalance", "key": SW_SERVICE_KEY, "session": session})
        data = res.json()
    if data.get(".status") != "ok":
        raise RuntimeError(str(data.get(".reason") or "Balance check failed"))
    balance_pia = float((data.get("balances") or {}).get("PIA") or 0)
    return SwStatus(True, email, balance_pia, None)


_sw_status_cache: CachedAsyncValue[SwStatus] = CachedAsyncValue("sw_status", _fetch_sw_status)


def invalidate_sw_status() -> None:
    """Call when the balance is about to change on purpose (a top-up) --
    hard, so the next read waits for the real new number."""
    _sw_status_cache.invalidate(hard=True)


async def get_sw_status(*, serve_stale: bool = True) -> SwStatus:
    """Backs the Settings "Account & Billing" section -- needs the actual
    PIA balance (a real v2 wallet:getBalance call), not just "is SW login
    configured at all". Served from the cache (see account_state.py) --
    only the "is anyone logged in / as whom" part is read fresh, from the
    local credentials file, every call, so a logout (or a login as a
    different account) shows up instantly without any invalidation."""
    creds = load_credentials()
    if not creds:
        return SwStatus(False, None, None, None)
    email = creds["email"]
    try:
        cached = await _sw_status_cache.get(max_age_s=SW_STATUS_TTL_S, serve_stale=serve_stale)
        if cached.email != email:
            # A different account than the one the cached balance belongs to.
            _sw_status_cache.invalidate(hard=True)
            cached = await _sw_status_cache.get(max_age_s=SW_STATUS_TTL_S, serve_stale=False)
        return cached
    except Exception as exc:
        return SwStatus(True, email, None, str(exc))


async def run_account_state_refresher() -> None:
    """Started once at backend startup (main.py): computes both cached values
    immediately -- so by the time anyone opens Settings or a tab starts, the
    answer is already there -- and re-checks every
    ACCOUNT_STATE_REFRESH_INTERVAL_S so it stays warm. A failure of either
    is logged by the cache itself and simply retried next round."""
    while True:
        try:
            await _claude_auth_status_cache.refresh()
        except Exception:
            pass
        if load_credentials():
            try:
                await _sw_status_cache.refresh()
            except Exception:
                pass
        await asyncio.sleep(ACCOUNT_STATE_REFRESH_INTERVAL_S)


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
