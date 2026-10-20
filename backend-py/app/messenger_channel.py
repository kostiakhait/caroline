"""app/messenger_channel.py -- Facebook Messenger personal-account
connections via a Node sidecar (fca-unofficial; see companion-apps/
messenger-sidecar/index.js's own doc comment for the full wire protocol and
why Node, not Python -- there is no official API for a personal Messenger
inbox, only for business Pages). Fifth and last of the planned messenger
integrations (docs/MESSENGER_INTEGRATIONS_PLAN.md, 2026-10-06), closely
modeled on app/whatsapp_channel.py (same sidecar transport, same two-account
split) with one real difference: login.

TWO separate accounts, same split as every other messenger here: "self" is
Caroline's OWN Messenger account -- she may read and reply there as herself,
no extra permission needed; "owner" is the human owner's own personal
Messenger account, linked so Caroline can read it and keep them posted, but
may send AS the owner only when they have explicitly asked her to, for that
specific message.

Login is browser-based (changed 2026-10-19 from an earlier email/password
design, after the user pointed out 2FA is essentially universal now and
that direct login needs to go through a real browser): messenger_login
opens Caroline's own embedded WebView2 window (app/plugins/
app_browser_plugin.py's AppBrowserHost, label "facebook_<account>") on
facebook.com and the USER logs in there BY HAND -- they see and answer
whatever Facebook actually asks for themselves (password, 2FA code,
checkpoint, anything), the same way they would in any ordinary browser.
Caroline never sees or stores the password at all. Once logged in, this
module polls that window's own cookie jar (AppBrowserWindow.GetCookiesAsync,
via AppBrowserHost's new /get_cookies -- WebView2's native CookieManager,
not document.cookie/ExecuteScriptAsync, since Facebook's own session cookie
("xs") is httpOnly and invisible to page JS by design) until a "c_user"
cookie appears, then exports the whole cookie jar as fca-unofficial's own
documented "appState" shape (an array of {key, value, domain, path}
objects -- precisely AppBrowserHost's /get_cookies response shape, no
reshaping needed) and saves it to disk. The browser window then closes and
the sidecar reconnects with that appState, same as every later start.

This replaced an earlier direct email+password design (see git history) once
actual package inspection showed fca-unofficial's own login() call has no
working mid-call verification-code step for that path at all -- browser-
based login sidesteps the whole problem instead of needing one, since the
human completes Facebook's own real login UI themselves.

Plaintext on disk, same precedent as whatsapp_channel.py's own Baileys
auth_<account>/ folder (the underlying library owns that file's format and
writes it directly in Node, not individually DPAPI-encrypted like a single
token) -- still under the same messengers/ root secret_store.py uses for
everything else.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.logging_setup import log_event
from app.plugins.app_browser_plugin import _call as _app_browser_call
from app.secret_store import messenger_secrets_dir
from app.sidecar_process import SidecarProcess
from app.task_supervisor import supervise

FACEBOOK_URL = "https://www.facebook.com/login"
# GetCookiesAsync (AppBrowserHost.cs) scopes to one URL's cookie jar; the
# bare origin (no /login) is what Facebook's own session cookies are set
# against regardless of which page on the site you land on.
FACEBOOK_ORIGIN = "https://www.facebook.com"
# Polling interval while waiting for the user to finish logging in by hand
# in the embedded browser window -- same "human-paced, not a stall" shape as
# whatsapp_channel.py's own QR-wait loop, just checking cookies instead of a
# sidecar event.
LOGIN_POLL_INTERVAL_S = 4.0

ACCOUNTS = ("self", "owner")
DEFAULT_ACCOUNT = "self"
_ACCOUNT_LABEL = {"self": "your own", "owner": "the owner's"}

NODE_EXE = os.environ.get("CAROLINE_NODE_PATH", "")
SIDECAR_DIR = os.environ.get("CAROLINE_MESSENGER_SIDECAR_DIR", "")
SEND_TIMEOUT_S = 30.0
DOWNLOAD_TIMEOUT_S = 60.0

# Same reasoning as whatsapp_channel.py's own CHANNEL_HEARTBEAT_TIMEOUT_S --
# generous because this watches the _run_sidecar coroutine specifically
# (the appState-based connect itself), not the human-paced browser-login
# wait (that's a separate, deliberately unbounded poll -- see
# start_messenger_channel's own loop).
CHANNEL_HEARTBEAT_TIMEOUT_S = 180.0
HEARTBEAT_PUMP_INTERVAL_S = 10.0


class MessengerError(Exception):
    pass


def is_valid_account(account: str) -> bool:
    return account in ACCOUNTS


def _check_account(account: str) -> str:
    if account not in ACCOUNTS:
        raise MessengerError(f'"account" must be one of {", ".join(ACCOUNTS)}, not {account!r}.')
    return account


def _appstate_path(workspace_dir: str, account: str) -> Path:
    return messenger_secrets_dir(workspace_dir, "messenger") / f"appstate_{account}.json"


def _has_prior_session(workspace_dir: str, account: str) -> bool:
    return _appstate_path(workspace_dir, account).is_file()


def browser_label(account: str) -> str:
    return f"facebook_{account}"


@dataclass
class MessengerChannelStatus:
    account: str = ""
    linked: bool = False
    connected: bool = False
    own_user_id: str | None = None
    last_event_at_iso: str | None = None
    last_error: str | None = None


_status: dict[str, MessengerChannelStatus] = {account: MessengerChannelStatus(account=account) for account in ACCOUNTS}
_sidecar: dict[str, SidecarProcess | None] = {account: None for account in ACCOUNTS}
_pending: dict[str, dict[str, asyncio.Future[Any]]] = {account: {} for account in ACCOUNTS}
# Set by login_start, cleared once the background loop's cookie-poll
# captures a working appState (or the user never finishes -- stays true
# indefinitely, same "human-paced, not a stall" reasoning as WhatsApp's own
# QR-wait flag). Same gate-whether-to-even-look shape as WhatsApp's own
# _login_requested; this one gates polling the browser window instead of
# spawning a sidecar directly.
_login_requested: dict[str, bool] = {account: False for account in ACCOUNTS}


def get_messenger_channel_status() -> dict[str, dict[str, Any]]:
    return {account: asdict(status) for account, status in _status.items()}


def is_connected(account: str) -> bool:
    sidecar = _sidecar.get(account)
    return sidecar is not None and not sidecar.closed and _status[account].connected


async def _call(account: str, cmd: str, *, timeout: float = SEND_TIMEOUT_S, **fields: Any) -> dict[str, Any]:
    """Same request/correlation-id dance as whatsapp_channel.py's own _call."""
    if not is_connected(account):
        raise MessengerError(f'Messenger ("{account}") isn\'t connected yet -- use messenger_login(account="{account}") first.')
    op_id = uuid.uuid4().hex
    fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
    _pending[account][op_id] = fut
    sidecar = _sidecar[account]
    assert sidecar is not None
    sidecar.send({"cmd": cmd, "id": op_id, **fields})
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _pending[account].pop(op_id, None)
        raise MessengerError(f"No response from Messenger within {int(timeout)}s.")


def _require_ok(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        raise MessengerError(result.get("error") or "unknown error")
    return result


async def send_message(account: str, to: str, text: str) -> None:
    _require_ok(await _call(account, "sendMessage", to=to, text=text))


async def list_chats(account: str) -> list[dict[str, Any]]:
    result = await _call(account, "listChats")
    return result.get("chats") or []


async def set_block(account: str, user_id: str, block: bool) -> None:
    _require_ok(await _call(account, "setBlock", userID=user_id, block=block))


async def read_thread_history(account: str, thread_id: str, amount: int) -> list[dict[str, Any]]:
    """Confirmed live (2026-10-20) against a real account: fca-unofficial's
    getThreadHistory really does return real past message content for a
    thread -- closing the exact gap the user asked about ("А доступ к
    содержимому?") after Caroline correctly reported she could only see the
    thread LIST, never a thread's own messages. `amount` is never defaulted
    or capped here -- per explicit instruction, every caller (messenger_
    plugin.py's own tool) must pass an explicit value; this module picks no
    number of its own."""
    result = await _call(account, "readHistory", timeout=DOWNLOAD_TIMEOUT_S, threadID=thread_id, amount=amount)
    return result.get("messages") or []


MESSENGER_DOWNLOADS_DIR_NAME = "messenger_downloads"


async def download_attachment(workspace_dir: str, account: str, ref: str, save_path: str | None = None) -> str:
    if not save_path:
        downloads_dir = Path(workspace_dir) / MESSENGER_DOWNLOADS_DIR_NAME
        downloads_dir.mkdir(parents=True, exist_ok=True)
        save_path = str(downloads_dir / ref.replace(":", "_"))
    result = _require_ok(await _call(account, "downloadAttachment", timeout=DOWNLOAD_TIMEOUT_S, ref=ref, savePath=save_path))
    return result.get("path") or save_path


async def send_file(account: str, to: str, path: str, caption: str | None = None) -> None:
    if not Path(path).is_file():
        raise MessengerError(f"No such local file: {path}")
    _require_ok(await _call(account, "sendFile", timeout=DOWNLOAD_TIMEOUT_S, to=to, path=path, caption=caption))


async def create_group(account: str, subject: str, participants: list[str]) -> str:
    result = _require_ok(await _call(account, "createGroup", subject=subject, participants=participants))
    return result.get("threadID") or ""


async def update_group_participants(account: str, thread_id: str, participants: list[str], action: str) -> None:
    _require_ok(await _call(account, "groupParticipants", threadID=thread_id, participants=participants, action=action))


async def leave_group(account: str, thread_id: str) -> None:
    _require_ok(await _call(account, "leaveGroup", threadID=thread_id))


async def delete_group(account: str, thread_id: str) -> None:
    """Messenger has no documented true "delete this group" call, same gap as
    WhatsApp -- the sidecar's own deleteGroup command removes every other
    participant it can, then leaves, which empties the group and makes it
    inaccessible to everyone including this account. Not a true delete;
    messenger_plugin.py's tool description says so."""
    _require_ok(await _call(account, "deleteGroup", threadID=thread_id))


async def login_start(account: str) -> str:
    """Opens Caroline's own embedded browser window on facebook.com for this
    account and sets the flag that lets start_messenger_channel's loop start
    polling its cookie jar -- see this module's own doc comment for the full
    flow. Returns immediately; the user does the actual logging-in by hand,
    at their own pace, in that window."""
    _check_account(account)
    if not NODE_EXE or not Path(NODE_EXE).exists():
        raise MessengerError("Messenger isn't set up in this build yet -- Node.js wasn't provisioned.")
    if is_connected(account):
        return f'Already connected (as {_status[account].own_user_id}, account="{account}").'
    await _app_browser_call("/open", {"label": browser_label(account), "url": FACEBOOK_URL}, timeout_s=90.0)
    _login_requested[account] = True
    return (
        f'Opened a Facebook login window for the "{account}" account -- log in there yourself (including any '
        "2FA code or checkpoint Facebook asks for, exactly like any ordinary browser login). No need to tell me "
        "anything back -- I'll notice once you're logged in and connect Messenger automatically, usually within "
        "a few seconds of you finishing."
    )


async def _try_capture_appstate(workspace_dir: str, account: str) -> bool:
    """Polled by start_messenger_channel's loop while _login_requested[account]
    is set. Returns True once a working session was captured (appState
    written, browser window closed) -- the caller's own loop then falls
    through to the normal has_prior_session path next iteration, same as
    whatsapp_channel.py's own start_login flag -> _has_prior_session
    handoff."""
    label = browser_label(account)
    try:
        cookies = await _app_browser_call("/get_cookies", {"label": label, "uri": FACEBOOK_ORIGIN}, timeout_s=15.0)
    except Exception as exc:
        log_event("plugin:messenger", "cookie_poll_failed", account=account, error=str(exc))
        return False
    if not isinstance(cookies, list) or not any(c.get("key") == "c_user" and c.get("value") for c in cookies):
        return False
    _appstate_path(workspace_dir, account).write_text(json.dumps(cookies), encoding="utf-8")
    try:
        await _app_browser_call("/close", {"label": label})
    except Exception as exc:
        log_event("plugin:messenger", "login_browser_close_failed", account=account, error=str(exc))
    _login_requested[account] = False
    log_event("plugin:messenger", "login_captured", account=account)
    return True


async def _run_sidecar(workspace_dir: str, account: str, inject_proactive: Callable[[str], None], touch: Callable[[], None] = lambda: None) -> None:
    if not NODE_EXE or not Path(NODE_EXE).exists():
        raise MessengerError("Node.js runtime not found -- reinstall Caroline to provision it.")
    if not SIDECAR_DIR or not (Path(SIDECAR_DIR) / "index.js").exists():
        raise MessengerError("messenger-sidecar not found -- reinstall Caroline.")

    closed_event = asyncio.Event()
    appstate_rejected = False
    sidecar: SidecarProcess | None = None

    def on_line(msg: dict[str, Any]) -> None:
        nonlocal appstate_rejected
        touch()
        msg_type = msg.get("type")
        if msg_type == "ready":
            _status[account].connected = True
            _status[account].linked = True
            _status[account].own_user_id = msg.get("userID")
            log_event("plugin:messenger", "connected", account=account, user_id=_status[account].own_user_id)
        elif msg_type == "message":
            if msg.get("fromSelf"):
                return
            text = (msg.get("text") or "").strip()
            if not text:
                return
            thread_id = msg.get("threadID") or "unknown"
            name = msg.get("chatName") or thread_id
            _status[account].last_event_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            log_event("plugin:messenger", "message_received", account=account, thread_id=thread_id)
            note = (
                f'[New Messenger message in {_ACCOUNT_LABEL[account]} Messenger account, from "{name}" '
                f'(threadID={thread_id}, senderID={msg.get("senderID")}, account="{account}"):\n{text}'
            )
            media = msg.get("media")
            attachment_ref = msg.get("attachmentRef")
            if media and attachment_ref:
                note += (
                    f'\nAttached: {media}. Use messenger_download_attachment(ref="{attachment_ref}", '
                    f'account="{account}") to save it locally -- only usable for a while after the message '
                    "arrives, this sidecar keeps no long-term attachment history."
                )
            if account == "self":
                note += (
                    "\nThis is Caroline's own separate Messenger account, not the user's -- replying here is "
                    "Caroline's own correspondence, not something done on the user's behalf, and needs no extra "
                    f'permission. Reply with messenger_send_message(to="{thread_id}", text=..., account="self") '
                    "if it needs a response.]"
                )
            else:
                note += (
                    "\nThis is the OWNER'S OWN correspondence, not yours -- read it and tell the owner about it "
                    "if it matters, but do not reply as them unless they have explicitly asked you to send this "
                    f'specific reply (messenger_send_message(to="{thread_id}", text=..., account="owner")).]'
                )
            inject_proactive(note)
        elif msg_type == "log":
            # Per explicit instruction (2026-10-20), after a real incident: a
            # successful-but-empty getThreadList result had nothing logging
            # WHY it was empty, so diagnosing it needed a manual standalone
            # reproduction instead of just reading caroline.log. index.js
            # now logs every operation and sub-step through this message
            # type -- plain-passed through to the real log, not filtered or
            # summarized, so a future anomaly is already on the record.
            extra = {k: v for k, v in msg.items() if k not in ("type", "message")}
            log_event("plugin:messenger", "sidecar_log", account=account, message=msg.get("message"), **extra)
        elif msg_type in ("sendResult", "chats", "history"):
            op_id = msg.get("id")
            fut = _pending[account].pop(op_id, None) if op_id else None
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif msg_type == "error":
            error_text = str(msg.get("message") or "")
            log_event("plugin:messenger", "sidecar_reported_error", account=account, error=error_text)
            if not _status[account].connected:
                # Never reached "ready" yet in this spawn -- the saved
                # appState itself was rejected outright (Facebook invalidated
                # the session since it was captured). Delete it rather than
                # retrying the same known-bad file every 60s forever; the
                # user has to redo messenger_login to get a fresh one.
                appstate_rejected = True
                _status[account].last_error = error_text
        elif msg_type == "sessionInvalid":
            # Confirmed live (2026-10-19, a deliberately bogus appState):
            # "ready" can fire BEFORE Facebook's own later validation catches
            # a broken session -- this event means the session is dead even
            # though _status[account].connected may already be True. Force
            # the sidecar to stop (same discard-and-require-relogin handling
            # as the "error"-before-ready case above) rather than leaving it
            # sitting there reporting "connected" with nothing actually
            # working -- see index.js's own doc comment for the full story.
            error_text = f'Facebook session invalidated ({msg.get("event")}): {json.dumps(msg.get("detail"))}'
            log_event("plugin:messenger", "session_invalidated", account=account, event=msg.get("event"))
            appstate_rejected = True
            _status[account].last_error = error_text
            if sidecar is not None:
                sidecar.stop()

    def on_closed() -> None:
        _status[account].connected = False
        closed_event.set()

    env = dict(os.environ)
    env["MESSENGER_APPSTATE_PATH"] = str(_appstate_path(workspace_dir, account))

    sidecar = SidecarProcess(
        argv=[NODE_EXE, str(Path(SIDECAR_DIR) / "index.js")],
        env=env,
        on_line=on_line,
        on_closed=on_closed,
        label=f"messenger_{account}",
        cwd=SIDECAR_DIR,
    )
    sidecar.start()
    _sidecar[account] = sidecar

    async def _pump_heartbeat() -> None:
        while True:
            if _status[account].connected:
                touch()
            await asyncio.sleep(HEARTBEAT_PUMP_INTERVAL_S)

    heartbeat_pump = asyncio.ensure_future(_pump_heartbeat())
    try:
        await closed_event.wait()
    finally:
        # Same orphan-process bug class whatsapp_channel.py's own _run_sidecar
        # fixed (2026-10-13) -- stop() here even when on_closed already fired,
        # idempotent, so a heartbeat-stall cancellation can't leave this
        # process running forever in the background.
        sidecar.stop()
        heartbeat_pump.cancel()
        _sidecar[account] = None
        _status[account].connected = False
        if appstate_rejected:
            _appstate_path(workspace_dir, account).unlink(missing_ok=True)
            log_event("plugin:messenger", "appstate_discarded", account=account)
        for fut in _pending[account].values():
            if not fut.done():
                fut.set_exception(MessengerError("Messenger sidecar disconnected"))
        _pending[account].clear()


def start_messenger_channel(workspace_dir: str, inject_proactive: Callable[[str], None]) -> list[asyncio.Task[None]]:
    """Startup loop, one per account: if a saved appState exists, spawn the
    sidecar; if messenger_login opened a browser window instead, poll its
    cookie jar until the user finishes logging in by hand (see
    _try_capture_appstate); otherwise re-check every 30s until one of those
    happens. Same overall shape as whatsapp_channel.py's own
    start_whatsapp_channel, with the QR-wait replaced by a cookie-wait."""
    def _make_loop(account: str) -> Callable[[Callable[[], None]], Any]:
        async def _loop(touch: Callable[[], None]) -> None:
            while True:
                if _login_requested[account] and not _has_prior_session(workspace_dir, account):
                    # Human-paced wait for the user to finish logging in by
                    # hand in the embedded browser window -- same "not a
                    # stall" reasoning as WhatsApp's own QR-scan wait.
                    touch()
                    await _try_capture_appstate(workspace_dir, account)
                    await asyncio.sleep(LOGIN_POLL_INTERVAL_S)
                    continue
                if not _has_prior_session(workspace_dir, account):
                    touch()
                    await asyncio.sleep(30.0)
                    continue
                try:
                    await _run_sidecar(workspace_dir, account, inject_proactive, touch)
                except MessengerError as exc:
                    _status[account].last_error = str(exc)
                    log_event("plugin:messenger", "sidecar_unavailable", account=account, error=str(exc))
                    touch()
                    await asyncio.sleep(60.0)
                    continue
                except Exception as exc:
                    _status[account].last_error = str(exc)
                    log_event("plugin:messenger", "sidecar_failed", account=account, error=str(exc))
                touch()
                await asyncio.sleep(5.0)
        return _loop

    return [
        supervise(f"messenger_channel_{account}", _make_loop(account), heartbeat_timeout_s=CHANNEL_HEARTBEAT_TIMEOUT_S)
        for account in ACCOUNTS
    ]
