"""Fetches Caroline's own model-provider (OpenRouter) API key for the
small-model primary path (see small_model_engine.py) -- per explicit
correction (2026-09-12) of the earlier ai:resolveAgenticStep redesign,
which routed every model-call STEP through squirrelwisdom.com instead of
running the dialogue locally: "подписка должна браться со
squirrelwisdom.com, а не ключи" meant the KEY should come from SW rather
than being hardcoded/vendored, not that every turn should be proxied
through SW. SquirrelWisdom's role in the dialogue path is exactly this
one-time (well, once-per-process) key handout, gated by the user's own
login -- the actual conversation content and tool-calling loop never
cross the network to SW at all; that's what sw_gate.py's own
SW_GATED_FEATURES list (notes/email/ratatosk-owner/office-editor/consult)
is for, a completely separate, unrelated set of tools.

Encrypted server-side (reforce's API/Api2AICommands.py, cmdV2GetModelKey)
with a key derived from the caller's OWN session token -- deliberately not
a shared secret baked into this client: only whoever already holds this
exact session (this process, right now) can decrypt what comes back. The
decrypted key is cached in memory only for this process's lifetime, never
written to disk -- a fresh session (new login, or a new process) fetches
again.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.login_api import get_v2_session
from app.logging_setup import log_event
from app.plugins.sw_api import CAROLINE_SW_KEY, call_v2

# Must match reforce's API/Api2AICommands.py's own _derive_fernet_key exactly --
# both sides derive the same key from the same session token independently.
_KDF_PREFIX = "caroline-model-key-v1:"

_cached_key: str | None = None
_cached_for_session: str | None = None


def _derive_fernet_key(session: str) -> bytes:
    digest = hashlib.sha256(f"{_KDF_PREFIX}{session}".encode()).digest()
    return base64.urlsafe_b64encode(digest)


async def get_model_provider_key() -> str | None:
    """Returns the decrypted OpenRouter key for this process, fetching on
    first use (or after a session change) and caching in memory only.
    Returns None if not logged into SquirrelWisdom, or the fetch/decrypt
    fails for any reason -- callers treat that as "small model unavailable
    right now" and escalate to the full SDK, same as small_model_engine.py's
    own "never a hard dependency" guarantee for every other failure mode
    in that path."""
    global _cached_key, _cached_for_session
    try:
        session = await get_v2_session()
    except Exception as exc:
        log_event("engine", "model_key_no_session", error=str(exc))
        return None

    if _cached_key and _cached_for_session == session:
        return _cached_key

    try:
        envelope = await call_v2("ai:getModelKey", key=CAROLINE_SW_KEY, session=session)
    except Exception as exc:
        log_event("engine", "model_key_fetch_failed", error=str(exc))
        return None

    encrypted = envelope.get("encryptedKey")
    if not isinstance(encrypted, str) or not encrypted:
        log_event("engine", "model_key_fetch_empty_response")
        return None

    try:
        key = Fernet(_derive_fernet_key(session)).decrypt(encrypted.encode()).decode()
    except InvalidToken:
        log_event("engine", "model_key_decrypt_failed")
        return None

    _cached_key = key
    _cached_for_session = session
    log_event("engine", "model_key_fetched", provider=envelope.get("provider"))
    return key
