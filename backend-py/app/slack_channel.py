"""app/slack_channel.py -- Slack Socket Mode connection, proactively
notifying the user of new Slack DMs/mentions. Same "background loop
pushes a proactive note into the primary session" shape as
ratatosk_channel.py's owner channel. Per docs/MESSENGER_INTEGRATIONS_PLAN.md
(2026-10-06), first of the planned messenger integrations -- the one
meant to prove the whole pattern (plugin + background loop + encrypted
secrets + proactive inject) before repeating it for Telegram/Discord/
WhatsApp/Signal.

TWO identities sharing ONE Socket Mode connection (decision 2026-10-11,
same "self"/"owner" split as Telegram's, adapted to how Slack's own OAuth
actually works -- this is genuinely different in shape from Telegram/
WhatsApp/Signal's two fully independent connections, not just the same
pattern copy-pasted): a single Slack App installation can request BOTH a
bot token (`scope=`) and a user token (`user_scope=`) in ONE OAuth
authorization, and Slack's own token-exchange response then hands back
both in one round trip -- no separate install step needed. "self" (the
BOT token, xoxb-) is Caroline's own separate identity in the workspace --
she may read/reply there as herself (whatever channels/DMs the bot itself
was invited to), no extra permission needed. "owner" (the USER token,
xoxp-, the ONLY one this file supported before 2026-10-11) acts AS the
human owner -- their own DMs and every channel they belong to; read
freely, but only send as them when they've explicitly asked for that
specific message. Socket Mode's own websocket (the app-level token,
xapp-) is shared app-wide infrastructure, not tied to either identity, so
there is only ONE background connection task here, not two -- which
identity an inbound event belongs to is read off the event's own
"authorizations" entry (is_bot), not from which connection it arrived on.

Three tokens total, per Slack's own Socket Mode design:
- app-level token (xapp-...): the Socket Mode WEBSOCKET CONNECTION itself
  (needs the connections:write scope). Never used for any Web API call.
- user token (xoxp-...): Web API calls acting AS the owner (their own DMs
  and channels).
- bot token (xoxb-...): Web API calls acting as Caroline's own separate
  bot identity -- only sees channels/DMs the bot itself was invited to.

NOT yet exercised against a live Slack App (the Caroline Slack App isn't
registered at api.slack.com yet, docs/MESSENGER_INTEGRATIONS_PLAN.md,
"Пока не регистрируем, реализуем" -- 2026-10-06) -- the bot-token half of
the exchange additionally needs reforce's own slack:oauthExchange command
to actually request/return a botToken alongside userToken/appToken, which
has NOT been confirmed server-side; this file's own client-side request
for it (the `scope` param below) is ready regardless, same as every other
one-time external prerequisite in this codebase.

All three tokens stored DPAPI-encrypted via app/secret_store.py, under
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

ACCOUNTS = ("self", "owner")
DEFAULT_ACCOUNT = "self"
_ACCOUNT_LABEL = {"self": "your own", "owner": "the owner's"}

# Not secret -- a Slack OAuth client_id is meant to ship inside the client,
# unlike client_secret (held server-side only, see reforce's own
# API/Api2SlackCommands.py doc comment). Empty until the Caroline Slack App
# is actually registered at api.slack.com (docs/MESSENGER_INTEGRATIONS_PLAN.md,
# "Пока не регистрируем, реализуем" -- 2026-10-06) -- slack_login fails
# clearly, not silently, until this is filled in.
SLACK_CLIENT_ID = ""

# User Token Scopes -- acting AS the owner, per slack_plugin.py's own doc
# comment. Matches what slack_plugin.py's tools actually call:
# conversations.list/history, chat.postMessage, search.messages.
SLACK_USER_SCOPES = (
    "channels:history,channels:read,chat:write,groups:history,groups:read,"
    "im:history,im:read,im:write,mpim:history,mpim:read,search:read,users:read,"
    "users:read.email"
)
# Bot Token Scopes -- Caroline's own separate "self" identity (2026-10-11).
# Deliberately the same capability set as the user scopes above, minus
# search:read (Slack has no bot-token search.messages equivalent at all) --
# a bot only ever sees channels/DMs it was itself invited to, same
# limitation already documented for the user-token path's own comments.
#
# im:write/users:read.email added (2026-10-16), after a real incident:
# resolution only ever covered conversations already surfaced by
# conversations.list -- there was no way to reach a workspace member Caroline
# had never DMed before, forcing an unnecessary "wait for them to message
# first" workaround even though Slack's own conversations.open (im:write)
# opens a DM cold with anyone in the workspace, and users.lookupByEmail
# (users:read.email) finds a member by email with no prior contact needed --
# see slack_plugin.py's _resolve_slack_target.
SLACK_BOT_SCOPES = (
    "channels:history,channels:read,chat:write,groups:history,groups:read,"
    "im:history,im:read,im:write,mpim:history,mpim:read,users:read,"
    "users:read.email"
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


def _bot_token_path(workspace_dir: str) -> Path:
    return messenger_secrets_dir(workspace_dir, "slack") / "bot_token.bin"


def get_app_token(workspace_dir: str) -> str | None:
    return decrypt_text_from_file(_app_token_path(workspace_dir))


def get_user_token(workspace_dir: str) -> str | None:
    return decrypt_text_from_file(_user_token_path(workspace_dir))


def get_bot_token(workspace_dir: str) -> str | None:
    return decrypt_text_from_file(_bot_token_path(workspace_dir))


def get_token_for_account(workspace_dir: str, account: str) -> str | None:
    return get_bot_token(workspace_dir) if account == "self" else get_user_token(workspace_dir)


def has_slack_tokens(workspace_dir: str) -> bool:
    """Whether the ONE shared Socket Mode connection can run at all -- the
    app-level token plus at least one of the two identity tokens (bot or
    user; either alone is enough for the connection to come up and start
    delivering whichever identity's events it's scoped for)."""
    return get_app_token(workspace_dir) is not None and (get_user_token(workspace_dir) is not None or get_bot_token(workspace_dir) is not None)


def set_slack_tokens(workspace_dir: str, app_token: str, user_token: str | None, bot_token: str | None = None) -> None:
    encrypt_text_to_file(_app_token_path(workspace_dir), app_token.strip(), "Slack app-level token (xapp-)")
    if user_token:
        encrypt_text_to_file(_user_token_path(workspace_dir), user_token.strip(), "Slack user token (xoxp-)")
    if bot_token:
        encrypt_text_to_file(_bot_token_path(workspace_dir), bot_token.strip(), "Slack bot token (xoxb-)")
    log_event("plugin:slack", "tokens_set", has_user_token=bool(user_token), has_bot_token=bool(bot_token))


async def run_oauth_login(workspace_dir: str, on_progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """The client half of Caroline's Slack login: opens the user's own
    browser to Slack's real OAuth consent screen, catches the redirect on
    a one-shot local HTTP server, and hands the resulting code to
    reforce's slack:oauthExchange (which holds client_secret and does the
    actual exchange -- see that command's own doc comment for why this
    split exists). Requests BOTH bot scopes (`scope`) and user scopes
    (`user_scope`) in the one authorization -- Slack's own OAuth v2 flow
    returns both a bot and a user token from a single exchange when both
    are requested, no separate install step needed for "self" vs "owner".
    Stores whichever tokens come back encrypted on success. Raises
    SlackLoginError for anything that stops it short -- callers show that
    message directly, no further wrapping needed."""
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
            {"client_id": SLACK_CLIENT_ID, "scope": SLACK_BOT_SCOPES, "user_scope": SLACK_USER_SCOPES, "redirect_uri": redirect_uri, "state": state}
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
    bot_token = result.get("botToken")
    app_token = result.get("appToken")
    if not user_token and not bot_token:
        raise SlackLoginError("The server didn't return a Slack user token or bot token.")
    if not app_token:
        raise SlackLoginError("Got an identity token but no app-level token back -- Slack isn't fully configured on the server yet.")
    set_slack_tokens(workspace_dir, app_token, user_token, bot_token)
    log_event("plugin:slack", "oauth_login_succeeded", team=result.get("team"))
    return {"team": result.get("team"), "userId": result.get("userId"), "hasBotToken": bool(bot_token)}


@dataclass
class SlackChannelStatus:
    """In-memory snapshot for diagnostics -- same reasoning as
    ratatosk_channel.py's own ChannelStatus: a socket connection with no
    introspection is nearly impossible to diagnose after the fact."""

    linked: bool = False
    connected: bool = False
    own_user_id: str | None = None
    own_bot_user_id: str | None = None
    last_event_at_iso: str | None = None
    last_error: str | None = None
    last_error_at_iso: str | None = None


_status = SlackChannelStatus()


def get_slack_channel_status() -> dict[str, Any]:
    return asdict(_status)


def _account_for_event(payload: dict[str, Any]) -> str:
    """Which identity an inbound event belongs to -- read off the event
    envelope's own "authorizations" list (each entry has "is_bot"), not
    off which connection it arrived on, since both identities share the
    ONE Socket Mode connection (see this module's own doc comment for
    why). Defaults to "owner" when that can't be determined -- the
    established, previously-working identity -- rather than guessing
    "self" and potentially under-stating who an action would be on behalf
    of; NOT yet confirmed against a live event, same honesty as the rest
    of this file."""
    for auth in payload.get("authorizations") or []:
        if isinstance(auth, dict) and auth.get("is_bot"):
            return "self"
    return "owner"


async def _run_client(workspace_dir: str, inject_proactive: Callable[[str], None]) -> None:
    app_token = get_app_token(workspace_dir)
    user_token = get_user_token(workspace_dir)
    bot_token = get_bot_token(workspace_dir)
    assert app_token and (user_token or bot_token)  # caller (start_slack_channel's loop) already checked has_slack_tokens

    own_user_id = None
    own_bot_user_id = None
    # auth_test() with whichever token(s) are actually set -- either may be
    # absent if only one identity has ever been linked.
    if user_token:
        auth = await AsyncWebClient(token=user_token).auth_test()
        own_user_id = auth["user_id"]
        _status.own_user_id = own_user_id
        log_event("plugin:slack", "authenticated", account="owner", user_id=own_user_id, team=auth.get("team"))
    if bot_token:
        bot_auth = await AsyncWebClient(token=bot_token).auth_test()
        own_bot_user_id = bot_auth["user_id"]
        _status.own_bot_user_id = own_bot_user_id
        log_event("plugin:slack", "authenticated", account="self", user_id=own_bot_user_id, team=bot_auth.get("team"))

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
        account = _account_for_event(req.payload)
        own_id = own_bot_user_id if account == "self" else own_user_id
        if own_id and event.get("user") == own_id:
            return  # our own message, echoed back -- not something to react to
        text = (event.get("text") or "").strip()
        if not text:
            return
        channel = event.get("channel") or "unknown"
        kind = "DM" if event.get("channel_type") == "im" else "mention"
        _status.last_event_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        log_event("plugin:slack", "message_received", account=account, channel=channel, kind=kind)
        note = f'[New Slack {kind} in {_ACCOUNT_LABEL[account]} Slack identity, channel "{channel}" (account="{account}"):\n{text}'
        if account == "self":
            note += (
                "\nThis is Caroline's own separate Slack bot identity, not the user's -- replying here is "
                "Caroline's own correspondence, not something done on the user's behalf, and needs no extra "
                f'permission. Reply with slack_send_message(channel="{channel}", text=..., account="self") if '
                "it needs a response.]"
            )
        else:
            note += (
                "\nThis is the OWNER'S OWN correspondence, not yours -- read it and tell the owner about it if "
                "it matters, but do not reply as them unless they have explicitly asked you to send this "
                f'specific reply (slack_send_message(channel="{channel}", text=..., account="owner")).]'
            )
        inject_proactive(note)

    client = SocketModeClient(app_token=app_token, web_client=AsyncWebClient(token=bot_token or user_token))
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
    """A single background task, not one per account (unlike Telegram/
    WhatsApp/Signal) -- Slack's Socket Mode connection is one shared
    app-wide websocket regardless of which identity (bot/user) a given
    event or send belongs to; see this module's own doc comment."""
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
