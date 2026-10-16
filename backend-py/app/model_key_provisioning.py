"""Fetches Caroline's own auth token for talking to Camerlengo's OpenRouter
chat-completions proxy (see small_model_engine.py's OpenRouterAdapter
construction, and reforce's API/Api2OpenRouterProxy.py).

Until 2026-10-16 this handed out a DECRYPTED OpenRouter API key (reforce's
API/Api2AICommands.py, cmdV2GetModelKey) for Caroline to call OpenRouter
directly from the user's own machine -- per explicit correction (2026-09-12)
of the earlier ai:resolveAgenticStep redesign, which routed every model-call
STEP through squirrelwisdom.com instead of running the dialogue locally:
"подписка должна браться со squirrelwisdом.com, а не ключи" meant the KEY
should come from SW rather than being hardcoded/vendored, not that every turn
should be proxied through SW.

That direct-to-OpenRouter path made every Caroline install's OpenRouter spend
invisible to Camerlengo -- no way to see which user or which app was burning
the budget (2026-10-16 incident: tagging/logging OpenRouter spend). The fix
reuses the SAME one-time-per-session handout idea, just for a different
credential: instead of decrypting a real OpenRouter key, Caroline now gets a
composite "<CAROLINE_SW_KEY>.<session>" token -- the exact auth convention
Api2AnthropicProxy.py already established for /v1/messages -- and the
OpenRouterAdapter's base_url points at Camerlengo's own /v1/chat/completions
instead of openrouter.ai directly. The real OpenRouter key now never leaves
Camerlengo at all; the conversation content and tool-calling loop still never
cross the network to SW for anything else (sw_gate.py's own SW_GATED_FEATURES
list -- notes/email/ratatosk-owner/office-editor/consult -- is a completely
separate, unrelated set of tools).

No decryption needed anymore (the session token is meaningless to anyone who
doesn't also hold CAROLINE_SW_KEY, and Camerlengo verifies both server-side),
so this is now just "get a current session, cache the composite token."
"""

from __future__ import annotations

import asyncio
import time

from app.login_api import get_v2_session
from app.logging_setup import log_event
from app.plugins.sw_api import CAROLINE_SW_KEY, load_credentials

_cached_key: str | None = None
_cached_for_session: str | None = None
# The cache above never hit (2026-10-11, live): it is keyed by the session, and
# get_v2_session() logs in afresh on every call, so every call had a new session and
# fetched the key again -- two round trips to SquirrelWisdom, 1.2s typically and once
# 5.8s, in front of EVERY model call of the small-model path: the fast lane, each
# narrator draft, each tier ("v2_session_refreshed" 12 times and "model_key_fetched" 10
# times in one turn's log). A token minted for the same account less than this long ago
# is reused without asking. Short on purpose: a token the server has stopped honoring
# (logout elsewhere, an account change) is dropped within a minute.
MODEL_KEY_REUSE_S = 60.0
_cached_for_account: str | None = None
_cached_at: float | None = None


async def get_model_provider_key() -> str | None:
    """Returns the composite "<CAROLINE_SW_KEY>.<session>" token Camerlengo's
    /v1/chat/completions proxy expects as its x-api-key, fetching a session
    on first use (or after a session change) and caching in memory only.
    Returns None if not logged into SquirrelWisdom, or the session fetch
    fails for any reason -- callers treat that as "small model unavailable
    right now", same as small_model_engine.py's own "never a hard
    dependency" guarantee for every other failure mode in that path.

    Retries the session fetch once after a short delay (2026-10-03,
    confirmed live: a transient SW hiccup -- not a real logout, it worked
    seconds earlier and again seconds later -- made a real user request
    dead-end on the generic "can't answer right now" fallback instead of
    a real answer). Mirrors the Android companion's own retryOnce: one
    extra try, not a general retry policy -- a real logout still fails
    the second attempt exactly as it should."""
    global _cached_key, _cached_for_session, _cached_for_account, _cached_at
    try:
        creds = load_credentials()
    except Exception:  # noqa: BLE001 -- an unreadable file is "not logged in", handled below
        creds = None
    account = creds.get("email") if creds else None
    if (
        _cached_key and account and _cached_for_account == account
        and _cached_at is not None and time.monotonic() - _cached_at < MODEL_KEY_REUSE_S
    ):
        return _cached_key
    session: str | None = None
    for attempt in (1, 2):
        try:
            session = await get_v2_session()
            break
        except Exception as exc:
            log_event("engine", "model_key_no_session", attempt=attempt, error=str(exc))
            if attempt == 2:
                return None
            await asyncio.sleep(0.8)

    token = f"{CAROLINE_SW_KEY}.{session}"
    _cached_key = token
    _cached_for_session = session
    _cached_for_account, _cached_at = account, time.monotonic()
    log_event("engine", "model_key_fetched")
    return token
