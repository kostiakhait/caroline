"""Ports backend/src/login.ts -- the shared SquirrelWisdom login used by
Notes/SMS/email/Ratatosk/consult/OnlyOffice and, most importantly, by
chat_session.py's own chatSource=="none" check (there is literally no
Claude account AND no SquirrelWisdom login -- Caroline can't respond at
all until one of those exists).

Credentials live in the SAME file every other SW-backed tool already
reads/writes (sw_api.py's load_credentials/save_credentials,
~/.mcp-notes/credentials.json) -- reused directly here, not duplicated.
The legacy verifyPassword call is likewise reused from notes_api.py
(same ".command": "verifyPassword" protocol, same APP_KEY) rather than
reimplemented a third time.

The password NEVER flows through the model's own context: the native
login form posts credentials straight to this backend over the app's own
WebSocket "login_submit" control op (see main.py), not through a tool
call -- open_login_request only pushes a WS event asking the frontend to
open that form and returns immediately, same reasoning as
viewer_plugin.py's open_in_viewer not blocking the turn.
"""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from app.logging_setup import log_event
from app.plugins import notes_api
from app.plugins.notes_api import NotesApiError
from app.plugins.sw_api import _post_json as _sw_post_json  # noqa: F401 -- reuse the same retrying POST helper
from app.plugins.sw_api import V2_LOGIN_SERVICE_KEY, load_credentials, mint_v2_session, save_credentials

SendFn = Callable[[dict[str, Any]], Awaitable[None]]


class LoginResult:
    __slots__ = ("ok", "error")

    def __init__(self, ok: bool, error: str | None = None) -> None:
        self.ok = ok
        self.error = error


def clear_credentials() -> None:
    """Logging out here logs Notes/SMS/email out too -- same shared
    account either way (see this module's own docstring)."""
    from app.plugins.sw_api import CREDENTIALS_PATH
    log_event("engine", "credentials_cleared")
    CREDENTIALS_PATH.unlink(missing_ok=True)
    reset_sw_auto_prompt_flag()


# --- auto-prompt-once-per-logout bookkeeping (see sw_gate.py's own use) ----
_sw_auto_prompt_shown = False


def has_auto_prompted_sw_login() -> bool:
    return _sw_auto_prompt_shown


def mark_sw_auto_prompt_shown() -> None:
    global _sw_auto_prompt_shown
    _sw_auto_prompt_shown = True


def reset_sw_auto_prompt_flag() -> None:
    global _sw_auto_prompt_shown
    _sw_auto_prompt_shown = False


def is_logged_in() -> bool:
    return load_credentials() is not None


def logged_in_email() -> str | None:
    creds = load_credentials()
    return creds["email"] if creds else None


async def verify_and_save_login(email: str, password: str) -> LoginResult:
    log_event("engine", "login_attempt", email=email)
    try:
        await notes_api.verify_password(email, password)
    except NotesApiError as exc:
        log_event("engine", "login_failed", email=email, error=str(exc))
        return LoginResult(False, str(exc))
    save_credentials(email, password)
    reset_sw_auto_prompt_flag()
    log_event("engine", "login_succeeded", email=email)
    return LoginResult(True)


async def register_account_only(email: str, password: str) -> LoginResult:
    """Self-service registration via the v2 "user:add" command (auth=public,
    no key/session needed) -- same underlying account store as
    verify_and_save_login's legacy verifyPassword (Auth.Authorizer, shared
    by both API generations), so the account this creates works for both
    without any extra step. Deliberately does NOT touch the shared
    credentials file -- factored out so ratatosk_own_account.py can
    register Caroline's OWN separate account the same way without it
    overwriting the user's own login (mirrors login.ts's own split)."""
    log_event("engine", "register_account_attempt", email=email)
    data = await _sw_post_json({"command": "user:add", "path": "/users", "user": email, "password": password})
    if data.get(".status") != "ok" or not data.get("session"):
        reason = str(data.get(".reason") or "Registration failed")
        log_event("engine", "register_account_failed", email=email, error=reason)
        return LoginResult(False, reason)
    log_event("engine", "register_account_succeeded", email=email)
    return LoginResult(True)


async def register_and_save_login(email: str, password: str) -> LoginResult:
    result = await register_account_only(email, password)
    if result.ok:
        save_credentials(email, password)
        reset_sw_auto_prompt_flag()
        log_event("engine", "register_and_save_login_saved", email=email)
    return result


async def get_session() -> str:
    """Legacy (v1-style) session -- refreshed fresh on every call rather
    than persisted (dies after a server-side idle timeout, this process
    may live longer or restart)."""
    creds = load_credentials()
    if not creds:
        raise NotesApiError("Not logged in to SquirrelWisdom -- call ensure_squirrelwisdom_login first.")
    session = await notes_api.verify_password(creds["email"], creds["password"])
    log_event("engine", "legacy_session_refreshed", email=creds["email"])
    return session


async def get_v2_session() -> str:
    creds = load_credentials()
    if not creds:
        raise NotesApiError("Not logged in to SquirrelWisdom -- call ensure_squirrelwisdom_login first.")
    session = await mint_v2_session(creds["email"], creds["password"])
    log_event("engine", "v2_session_refreshed", email=creds["email"])
    return session


# --- native login-window request tracking -----------------------------------
# requestId bookkeeping only, for symmetry with viewer_plugin.py's
# _open_requests -- the login form doesn't need to describe anything by
# path once it's done.
_open_requests: set[str] = set()


def take_login_request(request_id: str) -> bool:
    found = request_id in _open_requests
    _open_requests.discard(request_id)
    log_event("engine", "login_request_taken", request_id=request_id, found=found)
    return found


async def open_login_request(send: SendFn, no_ai_at_all: bool = False) -> str:
    """Opens the native login form -- factored out so a direct user action
    (Settings' "Log in" button, main.py's "open_login_from_settings"
    control op) can trigger the exact same flow without going through a
    chat tool call.

    no_ai_at_all: True only for chat_session.py's own chatSource=="none"
    check -- the one case where Caroline genuinely cannot talk at all (no
    Claude account AND no SquirrelWisdom login), as opposed to a single
    SW-gated feature being unavailable while chat itself works fine. Lets
    the native window show honest, context-specific copy instead of
    always claiming this is "needed for Notes and other features"."""
    request_id = uuid.uuid4().hex
    _open_requests.add(request_id)
    event: dict[str, Any] = {"type": "open_login", "requestId": request_id}
    if no_ai_at_all:
        event["noAiAtAll"] = True
    log_event("engine", "open_login_request", request_id=request_id, no_ai_at_all=no_ai_at_all)
    await send(event)
    return request_id
