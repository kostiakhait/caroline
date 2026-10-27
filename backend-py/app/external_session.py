"""Signed in to SquirrelWisdom with a session the host hands us, not with a password.

Inside a Xopek desktop the user already signed in on navlink.net; the desktop manager passes the Camerlengo v2 session in
CAROLINE_SW_USER / CAROLINE_SW_SESSION. In that mode there is no password anywhere: load_credentials() (sw_api.py) reports
the user with an empty password, and the two functions that turn credentials into a session (sw_api.mint_v2_session,
notes_api.verify_password) hand back this session when they are called with that empty password. Every caller of those
stays unchanged.

The session stays valid while it is used, and it is also refreshed by user:refreshSession every REFRESH_INTERVAL_S. When
Camerlengo rejects it (the password was changed, the session was ended) the mode is left: load_credentials() then falls back
to the credentials file, or to "not logged in", and the usual login window opens. The session itself is never written to disk.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from app.logging_setup import log_event

REFRESH_INTERVAL_S = 30 * 60
RETRY_AFTER_NETWORK_ERROR_S = 5 * 60

# Where "this process left the mode" is remembered: local to the container, so a backend restarted by the supervisor does not
# re-enter a mode that was already left (the env still holds the old session), and a new container starts clean.
_LEFT_MARKER = Path(tempfile.gettempdir()) / "caroline-external-session-left"

_user: str | None = None
_session: str | None = None
_active = False


def _load_from_env() -> None:
    global _user, _session, _active
    user = (os.environ.get("CAROLINE_SW_USER") or "").strip().lower()
    session = (os.environ.get("CAROLINE_SW_SESSION") or "").strip()
    _user, _session = user or None, session or None
    _active = bool(user and session and not _LEFT_MARKER.exists())
    if _active:
        log_event("engine", "external_session_active", email=user)


_load_from_env()


def active() -> bool:
    return _active


def user() -> str | None:
    return _user if _active else None


def session() -> str | None:
    return _session if _active else None


def matches(email: str) -> bool:
    """True when `email` is the user this mode signs in as."""
    return _active and bool(_user) and (email or "").strip().lower() == _user


def leave(reason: str) -> None:
    """Back to the credentials file (or to "not logged in")."""
    global _active
    if not _active:
        return
    _active = False
    try:
        _LEFT_MARKER.write_text(reason[:200], encoding="utf-8")
    except OSError:
        pass
    log_event("engine", "external_session_left", reason=reason[:200])


def _is_rejection(envelope: dict) -> bool:
    """Camerlengo says the session is not valid (as opposed to being unreachable or erroring for another reason)."""
    reason = str(envelope.get(".reason") or "").lower()
    return envelope.get(".status") != "ok" and "session" in reason


async def refresh_once() -> bool:
    """One user:refreshSession. False when the session was rejected (and the mode left); True otherwise, including when
    Camerlengo could not be reached, because an unreachable server says nothing about the session."""
    if not _active:
        return False
    from app.plugins.sw_api import _post_json
    try:
        envelope = await _post_json({"command": "user:refreshSession", "session": _session})
    except Exception as exc:  # noqa: BLE001 -- network trouble, retried later
        log_event("engine", "external_session_refresh_unreachable", error=str(exc)[:200])
        return True
    if envelope.get(".status") == "ok":
        return True
    if _is_rejection(envelope):
        leave(str(envelope.get(".reason")))
        return False
    log_event("engine", "external_session_refresh_error", reason=str(envelope.get(".reason"))[:200])
    return True


async def run_refresh_forever() -> None:
    """Started once at backend startup: checks the session right away, then keeps it alive."""
    if not _active:
        return
    delay = 0.0
    while _active:
        await asyncio.sleep(delay)
        if not _active:
            break
        ok = await refresh_once()
        if not ok:
            break
        delay = REFRESH_INTERVAL_S
    log_event("engine", "external_session_refresh_stopped")


def on_session_error(session_token: str | None) -> None:
    """A call made with the session failed as an invalid session: find out now whether it is really gone, rather than at
    the next scheduled refresh."""
    if _active and session_token and session_token == _session:
        try:
            asyncio.get_running_loop().create_task(refresh_once())
        except RuntimeError:
            pass
