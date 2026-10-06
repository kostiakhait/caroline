"""app/slack_channel.py -- Slack Socket Mode connection, proactively
notifying the user of new Slack DMs/mentions. Same "background loop
pushes a proactive note into the primary session" shape as
ratatosk_channel.py's owner channel. Per docs/MESSENGER_INTEGRATIONS_PLAN.md
(2026-10-06), first of the planned messenger integrations -- the one
meant to prove the whole pattern (plugin + background loop + encrypted
secrets + proactive inject) before repeating it for Telegram/Discord/
WhatsApp/Signal.

Two DIFFERENT tokens, per Slack's own Socket Mode design -- this is not
optional/simplifiable to one token:
- app-level token (xapp-...): the Socket Mode WEBSOCKET CONNECTION
  itself (needs the connections:write scope). Never used for any Web
  API call.
- user token (xoxp-...): every actual Web API call (chat.postMessage,
  search.messages, conversations.list, ...) -- acting AS the user
  (their own DMs and channels), not as a separate bot identity, per
  explicit instruction (2026-10-06). A bot token (xoxb-...) would only
  see channels/DMs the bot itself was invited to.

Both stored DPAPI-encrypted via app/secret_store.py, under
workspace_dir/messengers/slack/ -- the first secrets in this codebase
stored encrypted at rest rather than plaintext JSON (see secret_store.py's
own doc comment).
"""

from __future__ import annotations

import asyncio
import secrets
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from aiohttp import web
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from app.logging_setup import log_event
from app.reforce_v2 import ReforceError, call as reforce_call
from app.secret_store import decrypt_text_from_file, encrypt_text_to_file, messenger_secrets_dir
from app.task_supervisor import supervise

# Not secret -- a Slack OAuth client_id is meant to ship inside the client,
# unlike client_secret (held server-side only, see reforce's own
# API/Api2SlackCommands.py doc comment). Empty until the Caroline Slack App
# is actually registered at api.slack.com (docs/MESSENGER_INTEGRATIONS_PLAN.md,
# "Пока не регистрируем, реализуем" -- 2026-10-06) -- slack_login fails
# clearly, not silently, until this is filled in.
SLACK_CLIENT_ID = ""

# User Token Scopes (not Bot Token Scopes) -- acting AS the user, per
# slack_plugin.py's own doc comment. Matches what slack_plugin.py's tools
# actually call: conversations.list/history, chat.postMessage, search.messages.
SLACK_USER_SCOPES = (
    "channels:history,channels:read,chat:write,groups:history,groups:read,"
    "im:history,im:read,mpim:history,mpim:read,search:read,users:read"
)

# Fixed local port for the OAuth redirect -- must match exactly what's
# registered as a Redirect URL on the Slack App (api.slack.com), so it has
# to be a constant, not a randomly chosen free port.
OAUTH_CALLBACK_PORT = 17623
OAUTH_LOGIN_TIMEOUT_S = 300.0


class SlackLoginError(Exception):
    """Raised by run_oauth_login for anything that stops a login from
    completing -- caught by slack_plugin.py's slack_login tool and turned
    into a plain is_error reply, never an uncaught crash mid-turn."""

# How often the loop re-checks for tokens when not yet linked -- cheap (two
# local file reads), no point checking faster than a human could plausibly
# finish the one-time setup and call slack_set_tokens.
UNLINKED_RECHECK_INTERVAL_S = 30.0


def _app_token_path(workspace_dir: str) -> Path:
    return messenger_secrets_dir(workspace_dir, "slack") / "app_token.bin"


def _user_token_path(workspace_dir: str) -> Path:
    return messenger_secrets_dir(workspace_dir, "slack") / "user_token.bin"


def get_app_token(workspace_dir: str) -> str | None:
    return decrypt_text_from_file(_app_token_path(workspace_dir))


def get_user_token(workspace_dir: str) -> str | None:
    return decrypt_text_from_file(_user_token_path(workspace_dir))


def has_slack_tokens(workspace_dir: str) -> bool:
    return get_app_token(workspace_dir) is not None and get_user_token(workspace_dir) is not None


def set_slack_tokens(workspace_dir: str, app_token: str, user_token: str) -> None:
    encrypt_text_to_file(_app_token_path(workspace_dir), app_token.strip(), "Slack app-level token (xapp-)")
    encrypt_text_to_file(_user_token_path(workspace_dir), user_token.strip(), "Slack user token (xoxp-)")
    log_event("plugin:slack", "tokens_set")


async def run_oauth_login(workspace_dir: str, on_progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """The client half of Caroline's Slack login: opens the user's own
    browser to Slack's real OAuth consent screen, catches the redirect on
    a one-shot local HTTP server, and hands the resulting code to
    reforce's slack:oauthExchange (which holds client_secret and does the
    actual exchange -- see that command's own doc comment for why this
    split exists). Stores the resulting tokens encrypted on success.
    Raises SlackLoginError for anything that stops it short -- callers
    show that message directly, no further wrapping needed."""
    if not SLACK_CLIENT_ID:
        raise SlackLoginError(
            "Slack isn't set up in this build yet -- the Caroline Slack App hasn't been registered/configured. "
            "Nothing to do here until that happens."
        )

    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://127.0.0.1:{OAUTH_CALLBACK_PORT}/callback"
    code_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()

    async def _handle_callback(request: web.Request) -> web.Response:
        if request.query.get("state") != state:
            if not code_future.done():
                code_future.set_exception(SlackLoginError("Slack redirected with a mismatched state -- possible CSRF, aborting."))
            return web.Response(text="Something went wrong (state mismatch) -- you can close this tab.", status=400)
        error = request.query.get("error")
        if error:
            if not code_future.done():
                code_future.set_exception(SlackLoginError(f"Slack login wasn't completed: {error}"))
            return web.Response(text="Login was cancelled -- you can close this tab.")
        code = request.query.get("code")
        if not code:
            if not code_future.done():
                code_future.set_exception(SlackLoginError("Slack redirected with no code and no error -- unexpected response shape."))
            return web.Response(text="Something went wrong -- you can close this tab.", status=400)
        if not code_future.done():
            code_future.set_result(code)
        return web.Response(text="Signed in to Slack -- you can close this tab and go back to Caroline.", content_type="text/html")

    app = web.Application()
    app.router.add_get("/callback", _handle_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", OAUTH_CALLBACK_PORT)
    await site.start()
    try:
        authorize_url = "https://slack.com/oauth/v2/authorize?" + urlencode(
            {"client_id": SLACK_CLIENT_ID, "user_scope": SLACK_USER_SCOPES, "redirect_uri": redirect_uri, "state": state}
        )
        log_event("plugin:slack", "oauth_login_started")
        if on_progress:
            on_progress("Opening your browser to log in to Slack...")
        webbrowser.open(authorize_url)
        try:
            code = await asyncio.wait_for(code_future, timeout=OAUTH_LOGIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise SlackLoginError(f"No response after {int(OAUTH_LOGIN_TIMEOUT_S)}s -- the browser login wasn't completed in time.")
    finally:
        await runner.cleanup()

    if on_progress:
        on_progress("Got the login -- finishing setup...")
    try:
        response = await reforce_call("slack:oauthExchange", {"code": code, "redirectUri": redirect_uri})
    except ReforceError as exc:
        raise SlackLoginError(str(exc)) from exc
    result = response.get("result") or {}
    user_token = result.get("userToken")
    app_token = result.get("appToken")
    if not user_token:
        raise SlackLoginError("The server didn't return a Slack user token.")
    if not app_token:
        raise SlackLoginError("Got a user token but no app-level token back -- Slack isn't fully configured on the server yet.")
    set_slack_tokens(workspace_dir, app_token, user_token)
    log_event("plugin:slack", "oauth_login_succeeded", team=result.get("team"))
    return {"team": result.get("team"), "userId": result.get("userId")}


@dataclass
class SlackChannelStatus:
    """In-memory snapshot for diagnostics -- same reasoning as
    ratatosk_channel.py's own ChannelStatus: a socket connection with no
    introspection is nearly impossible to diagnose after the fact."""

    linked: bool = False
    connected: bool = False
    own_user_id: str | None = None
    last_event_at_iso: str | None = None
    last_error: str | None = None
    last_error_at_iso: str | None = None


_status = SlackChannelStatus()


def get_slack_channel_status() -> dict[str, Any]:
    return asdict(_status)


async def _run_client(workspace_dir: str, inject_proactive: Callable[[str], None]) -> None:
    app_token = get_app_token(workspace_dir)
    user_token = get_user_token(workspace_dir)
    assert app_token and user_token  # caller (start_slack_channel's loop) already checked has_slack_tokens

    web_client = AsyncWebClient(token=user_token)
    auth = await web_client.auth_test()
    own_user_id = auth["user_id"]
    _status.own_user_id = own_user_id
    log_event("plugin:slack", "authenticated", user_id=own_user_id, team=auth.get("team"))

    async def _on_request(client: SocketModeClient, req: SocketModeRequest) -> None:
        # Acknowledge EVERY request, not just events_api -- an
        # unacknowledged envelope gets redelivered and piles up (Slack's
        # own documented retry behavior).
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        event = req.payload.get("event") or {}
        event_type = event.get("type")
        if event_type not in ("message", "app_mention"):
            return
        if event.get("subtype") is not None:
            # message_changed/message_deleted/channel_join/bot_message/... --
            # not a new real message a human just sent.
            return
        if event.get("user") == own_user_id:
            return  # our own message, echoed back -- not something to react to
        text = (event.get("text") or "").strip()
        if not text:
            return
        channel = event.get("channel") or "unknown"
        kind = "DM" if event.get("channel_type") == "im" else "mention"
        _status.last_event_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        log_event("plugin:slack", "message_received", channel=channel, kind=kind)
        inject_proactive(
            f'[New Slack {kind} in channel "{channel}" -- reply with '
            f'slack_send_message(channel="{channel}", text=...) if it needs a response:\n{text}]'
        )

    client = SocketModeClient(app_token=app_token, web_client=web_client)
    client.socket_mode_request_listeners.append(_on_request)
    await client.connect()
    _status.connected = True
    log_event("plugin:slack", "connected")
    try:
        # Runs forever -- SocketModeClient manages its own reconnection
        # internally (auto_reconnect_enabled, default True). This only
        # returns/raises on a genuinely unrecoverable failure, which
        # start_slack_channel's own loop below catches and retries after
        # a pause, same as every other loop in this codebase.
        await asyncio.Event().wait()
    finally:
        _status.connected = False


def start_slack_channel(workspace_dir: str, inject_proactive: Callable[[str], None]) -> asyncio.Task[None]:
    log_event("plugin:slack", "starting_channel")

    async def _loop() -> None:
        while True:
            linked = has_slack_tokens(workspace_dir)
            _status.linked = linked
            if not linked:
                await asyncio.sleep(UNLINKED_RECHECK_INTERVAL_S)
                continue
            try:
                await _run_client(workspace_dir, inject_proactive)
            except Exception as exc:
                _status.connected = False
                _status.last_error = str(exc)
                _status.last_error_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                log_event("plugin:slack", "connection_failed", error=str(exc))
                await asyncio.sleep(5.0)  # flat retry, same convention as every other loop here -- no backoff

    return supervise("slack_channel", _loop)
