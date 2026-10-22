"""app/whatsapp_channel.py -- WhatsApp personal-account connections via a
Node sidecar (Baileys; see companion-apps/whatsapp-sidecar/index.js's own
doc comment for the full wire protocol and why Node, not Python -- there
is no official API for a personal WhatsApp account, Baileys is an
unofficial reimplementation of the WhatsApp Web protocol). Fourth of the
planned messenger integrations (docs/MESSENGER_INTEGRATIONS_PLAN.md,
2026-10-06), and the first to need an external runtime -- see
app/sidecar_process.py for the stdio transport this rides on.

TWO separate accounts, not one (decision 2026-10-11, same shape as
telegram_channel.py's own "self"/"owner" split, added after it became
clear the single-account design shipped here was simply incomplete, not
a real protocol limitation): "self" is Caroline's OWN WhatsApp account --
her own number, linked once like any other WhatsApp Web device, she may
read and reply there as herself, no extra permission needed; "owner" is
the human owner's own personal WhatsApp account, linked so Caroline can
read it and keep them posted, but may send AS the owner only when they
have explicitly asked her to, for that specific message. Both run their
own, fully independent Node sidecar PROCESS (one each -- Baileys/WhatsApp
Web is one device-link per socket, there is no multi-account mode within
a single connection), with their own auth-state directory, so one account
can be linked and running while the other has never logged in, same as
Telegram's two accounts. The ONE real external prerequisite, unlike
Telegram (where the Caroline application itself was a one-time
registration at my.telegram.org): WhatsApp has no "create a fresh account
for free" option at all -- "self" needs an actual second phone number with
an active WhatsApp installation to scan the link QR, whenever one is
available. The code is ready regardless, the same way Telegram's "self"
account was ready in code before anyone had actually linked it.

Linking shows a QR code: rendered to a PNG under workspace_dir and opened
in Caroline's own floating viewer window (the same "open_editor" WS
message viewer_plugin.py's open_in_viewer sends, just pushed directly via
session_context's register_send_for_tab/get_send_for_tab instead of
get_send() -- this fires from the background sidecar loop, not from
inside a turn). A refreshed QR (Baileys reissues one every 20-60s while
unscanned) updates that SAME window instead of opening another one -- see
MainWindow.xaml.cs's open_editor handler for the reuse-in-place fix this
relies on (2026-10-11; a real incident, piling up one OS-viewer window per
refresh before this).

Baileys' own multi-file auth-state (created on first link) lives under
workspace_dir/messengers/whatsapp/auth_<account>/ -- not individually
DPAPI-encrypted like a single token (Baileys owns the shape of those
files, not this module), but still under the same messengers/ root
secret_store.py uses for everything else.

Real risk, not hidden: Baileys is unofficial. A WhatsApp account used
this way can be banned by WhatsApp at their own discretion -- this is
inherent to there being no personal-account API at all, not a bug here.
whatsapp_login's own result text says so.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.logging_setup import log_event
from app.untrusted_text import third_party_label, third_party_text
from app.secret_store import messenger_secrets_dir
from app.session_context import get_send_for_tab, get_tab_id
from app.sidecar_process import SidecarProcess
from app.task_supervisor import supervise
from app.workspace_dir import WORKSPACE_DIR

import os

ACCOUNTS = ("self", "owner")
DEFAULT_ACCOUNT = "self"
_ACCOUNT_LABEL = {"self": "your own", "owner": "the owner's"}

NODE_EXE = os.environ.get("CAROLINE_NODE_PATH", "")
SIDECAR_DIR = os.environ.get("CAROLINE_WHATSAPP_SIDECAR_DIR", "")
SEND_TIMEOUT_S = 30.0

# Opt-in heartbeat watchdog (task_supervisor.py's supervise(), 2026-10-11,
# after a real Telegram incident of the same shape -- see telegram_
# channel.py's own docstring). Deliberately more generous than Telegram's
# own 60s: linking here involves a human scanning a QR code at their own
# pace, which can legitimately run several minutes with little sidecar
# traffic in between -- Telegram's heartbeat-covered path never has an
# analogous human-paced wait (its own first-link flow is a separate code
# path from the one this watches).
CHANNEL_HEARTBEAT_TIMEOUT_S = 300.0
HEARTBEAT_PUMP_INTERVAL_S = 10.0


class WhatsAppError(Exception):
    pass


def is_valid_account(account: str) -> bool:
    return account in ACCOUNTS


def _check_account(account: str) -> str:
    if account not in ACCOUNTS:
        raise WhatsAppError(f'"account" must be one of {", ".join(ACCOUNTS)}, not {account!r}.')
    return account


def _auth_dir(workspace_dir: str, account: str) -> Path:
    d = messenger_secrets_dir(workspace_dir, "whatsapp") / f"auth_{account}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _qr_image_path(workspace_dir: str, account: str) -> Path:
    return messenger_secrets_dir(workspace_dir, "whatsapp") / f"link-qr-{account}.png"


@dataclass
class WhatsAppChannelStatus:
    account: str = ""
    linked: bool = False
    connected: bool = False
    own_jid: str | None = None
    last_event_at_iso: str | None = None
    last_error: str | None = None


_status: dict[str, WhatsAppChannelStatus] = {account: WhatsAppChannelStatus(account=account) for account in ACCOUNTS}
_sidecar: dict[str, SidecarProcess | None] = {account: None for account in ACCOUNTS}
_pending: dict[str, dict[str, asyncio.Future[Any]]] = {account: {} for account in ACCOUNTS}
# Per explicit instruction (2026-10-06): never start an account's sidecar
# (and so never pop a QR image open) until the user has actually asked to
# connect it -- a fresh install must not auto-launch Node and show a QR
# nobody requested. True for the rest of this process's life once set for
# that account, same as "has credentials" gates Slack/Telegram/Discord's
# own loops -- the difference here is WhatsApp's own "credential" (the
# auth-state dir) doesn't exist yet on a first-ever link, so an explicit
# flag covers that one case; every later reconnect (including across a
# Caroline restart, once linked once) goes through the normal auth-dir
# check. Independent per account, same as everything else here.
_login_requested: dict[str, bool] = {account: False for account in ACCOUNTS}
# Which tab's whatsapp_login call set _login_requested, per account -- the
# tab whose own window should show that account's QR code (see
# register_send_for_tab/get_send_for_tab, needed because _handle_qr fires
# from the background sidecar loop, not from inside that tab's own turn).
# Sticky like _login_requested itself; a later relink from a different tab
# simply replaces it.
_login_requested_tab_id: dict[str, str | None] = {account: None for account in ACCOUNTS}


def _has_prior_session(workspace_dir: str, account: str) -> bool:
    try:
        return any(_auth_dir(workspace_dir, account).iterdir())
    except OSError:
        return False


def get_whatsapp_channel_status() -> dict[str, dict[str, Any]]:
    return {account: asdict(status) for account, status in _status.items()}


def is_connected(account: str) -> bool:
    sidecar = _sidecar.get(account)
    return sidecar is not None and not sidecar.closed and _status[account].connected


async def _call(account: str, cmd: str, *, timeout: float = SEND_TIMEOUT_S, **fields: Any) -> dict[str, Any]:
    """The request/response dance every sidecar command shares: send one
    line with a fresh correlation id, wait for on_line (below) to resolve
    the matching future from a "sendResult"/"chats" reply. Factored out
    2026-10-11 once group/attachment/contact commands made send_message's
    and list_chats' original copy of this dance a five-times-repeated
    block."""
    if not is_connected(account):
        raise WhatsAppError(f'WhatsApp ("{account}") isn\'t connected yet -- use whatsapp_login(account="{account}") first.')
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
        raise WhatsAppError(f"No response from WhatsApp within {int(timeout)}s.")


def _require_ok(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        raise WhatsAppError(result.get("error") or "unknown error")
    return result


async def send_message(account: str, to: str, text: str) -> None:
    _require_ok(await _call(account, "sendMessage", to=to, text=text))


async def list_chats(account: str) -> list[dict[str, Any]]:
    result = await _call(account, "listChats")
    return result.get("chats") or []


async def check_number(account: str, phone: str) -> tuple[bool, str | None]:
    """Whether a phone number is registered on WhatsApp at all -- unlike
    Telegram, WhatsApp needs no "add contact first" step to message a
    number cold (there is no server-side contact list to add to), so this
    is purely an optional preflight, never required before send_message."""
    result = _require_ok(await _call(account, "checkNumber", phone=phone))
    return bool(result.get("exists")), result.get("jid")


async def set_block(account: str, jid: str, block: bool) -> None:
    _require_ok(await _call(account, "setBlock", jid=jid, block=block))


# Attachments (2026-10-11). Unlike Telegram, WhatsApp/Baileys gives no way
# to re-fetch an arbitrary past message by id -- the sidecar keeps a short-
# lived cache of recent media messages' raw envelopes (see index.js's
# pendingMedia) keyed by a "ref" string it hands back in the proactive
# notification; download_media just asks the sidecar for that ref.
WHATSAPP_DOWNLOADS_DIR = Path(WORKSPACE_DIR) / "whatsapp_downloads"
DOWNLOAD_TIMEOUT_S = 60.0


async def download_media(account: str, ref: str, save_path: str | None = None) -> str:
    if not save_path:
        WHATSAPP_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        # No file extension guessed here (unlike Telegram's default-path
        # case) -- unlike Telethon's Message.file, nothing on the Python
        # side knows this attachment's real type until the sidecar's own
        # downloadMediaMessage call resolves it.
        save_path = str(WHATSAPP_DOWNLOADS_DIR / ref.replace(":", "_").replace("@", "_"))
    result = _require_ok(await _call(account, "downloadMedia", timeout=DOWNLOAD_TIMEOUT_S, ref=ref, savePath=save_path))
    return result.get("path") or save_path


async def send_file(account: str, to: str, path: str, caption: str | None = None) -> None:
    if not Path(path).is_file():
        raise WhatsAppError(f"No such local file: {path}")
    _require_ok(await _call(account, "sendFile", timeout=DOWNLOAD_TIMEOUT_S, to=to, path=path, caption=caption))


# Groups (2026-10-11). WhatsApp has only one kind of group entity (unlike
# Telegram's basic-group/channel split), and no true "delete" operation --
# delete_group is a deliberate approximation (remove every other
# participant, then leave), documented as such rather than overclaiming.
async def create_group(account: str, subject: str, participants: list[str]) -> str:
    result = _require_ok(await _call(account, "createGroup", subject=subject, participants=participants))
    return result.get("jid") or ""


async def update_group_participants(account: str, jid: str, participants: list[str], action: str) -> None:
    _require_ok(await _call(account, "groupParticipants", jid=jid, participants=participants, action=action))


async def leave_group(account: str, jid: str) -> None:
    _require_ok(await _call(account, "leaveGroup", jid=jid))


async def delete_group(account: str, jid: str) -> None:
    """WhatsApp has no real "delete this group" call -- the sidecar's own
    deleteGroup command removes every other participant it can, then
    leaves, which empties the group and makes it inaccessible to everyone
    including this account. Not a true delete; whatsapp_plugin.py's tool
    description says so rather than letting the model overclaim it."""
    _require_ok(await _call(account, "deleteGroup", jid=jid))


async def start_login(workspace_dir: str, account: str) -> str:
    """Sets the flag that lets start_whatsapp_channel's loop actually spawn
    that account's sidecar -- the QR itself appears (opened in Caroline's
    own viewer window) once it connects and emits one, not synchronously
    from this call. Doesn't wait inside the tool call for something that
    can take a while and needs the user to act in between (scan the code).

    Captures the calling tab's id (get_tab_id() is only valid here, inside
    a real tool call -- not later, inside the background sidecar loop that
    actually needs it) so the QR lands in the window of whichever tab
    asked to connect, not an arbitrary one."""
    _check_account(account)
    if not NODE_EXE or not Path(NODE_EXE).exists():
        raise WhatsAppError("WhatsApp isn't set up in this build yet -- Node.js wasn't provisioned.")
    if is_connected(account):
        return f'Already connected (as {_status[account].own_jid}, account="{account}").'
    _login_requested[account] = True
    _login_requested_tab_id[account] = get_tab_id()
    return (
        f'Starting WhatsApp ("{account}") -- a QR code image will open automatically in a moment. Scan it with '
        "WhatsApp on the phone for that account (Linked Devices > Link a Device). Real risk, stated plainly: "
        "WhatsApp has no official API for a personal account, so this uses an unofficial protocol library -- "
        "WhatsApp could in principle flag or restrict the account for it, at their own discretion."
    )


async def _run_sidecar(workspace_dir: str, account: str, inject_proactive: Callable[[str], None], touch: Callable[[], None] = lambda: None) -> None:
    """`touch()` is supervise()'s heartbeat callback (2026-10-11, after a
    real Telegram incident of the same shape -- see telegram_channel.py's
    own docstring for the full story). Called on "ready" and pumped
    periodically for as long as the sidecar is connected."""
    if not NODE_EXE or not Path(NODE_EXE).exists():
        raise WhatsAppError("Node.js runtime not found -- reinstall Caroline to provision it.")
    if not SIDECAR_DIR or not (Path(SIDECAR_DIR) / "index.js").exists():
        raise WhatsAppError("whatsapp-sidecar not found -- reinstall Caroline.")

    closed_event = asyncio.Event()

    def on_line(msg: dict[str, Any]) -> None:
        # Any line at all -- including routine traffic -- proves the
        # sidecar's own event loop is alive, which is the actual thing the
        # heartbeat watchdog needs to know; QR-scan waits are human-paced
        # and can legitimately run long with little traffic, so this (not
        # only "ready") is what keeps that wait from false-positiving as
        # a stall. See CHANNEL_HEARTBEAT_TIMEOUT_S's own comment.
        touch()
        msg_type = msg.get("type")
        if msg_type == "qr":
            _handle_qr(workspace_dir, account, str(msg.get("data") or ""))
        elif msg_type == "ready":
            _status[account].connected = True
            _status[account].linked = True
            _status[account].own_jid = msg.get("jid")
            log_event("plugin:whatsapp", "connected", account=account, jid=_status[account].own_jid)
            _close_qr_viewer(workspace_dir, account)
        elif msg_type == "loggedOut":
            _status[account].connected = False
            _status[account].linked = False
            log_event("plugin:whatsapp", "logged_out", account=account)
        elif msg_type == "message":
            if msg.get("fromMe"):
                return
            text = (msg.get("text") or "").strip()
            if not text:
                return
            frm = msg.get("from") or "unknown"
            name = third_party_label(msg.get("chatName") or frm)
            _status[account].last_event_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            log_event("plugin:whatsapp", "message_received", account=account, frm=frm)
            note = (
                f'[New WhatsApp message in {_ACCOUNT_LABEL[account]} WhatsApp account, from "{name}" '
                f'(jid={frm}, account="{account}"):\n'
                f'{third_party_text(text, name) if text else "(no caption)"}'
            )
            if account == "self":
                note += (
                    "\nThis is Caroline's own separate WhatsApp account, not the user's -- replying here is "
                    "Caroline's own correspondence, not something done on the user's behalf, and needs no extra "
                    f'permission. Reply with whatsapp_send_message(to="{frm}", text=..., account="self") if it '
                    "needs a response.]"
                )
            else:
                note += (
                    "\nThis is the OWNER'S OWN correspondence, not yours -- read it and tell the owner about it "
                    "if it matters, but do not reply as them unless they have explicitly asked you to send this "
                    f'specific reply (whatsapp_send_message(to="{frm}", text=..., account="owner")).]'
                )
            inject_proactive(note)
        elif msg_type in ("sendResult", "chats"):
            op_id = msg.get("id")
            fut = _pending[account].pop(op_id, None) if op_id else None
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif msg_type == "error":
            log_event("plugin:whatsapp", "sidecar_reported_error", account=account, error=msg.get("message"))

    def on_closed() -> None:
        _status[account].connected = False
        closed_event.set()

    sidecar = SidecarProcess(
        argv=[NODE_EXE, str(Path(SIDECAR_DIR) / "index.js")],
        env={**_env_for_sidecar(), "WHATSAPP_AUTH_DIR": str(_auth_dir(workspace_dir, account))},
        on_line=on_line,
        on_closed=on_closed,
        label=f"whatsapp_{account}",
        cwd=SIDECAR_DIR,
    )
    sidecar.start()
    _sidecar[account] = sidecar

    async def _pump_heartbeat() -> None:
        # Only touches once "ready" has actually fired (_status[account].
        # connected) -- a sidecar that hangs before ever reaching "ready"
        # must still be caught as stalled, the same failure shape the real
        # Telegram incident had, not masked by a pump that started too
        # early.
        while True:
            if _status[account].connected:
                touch()
            await asyncio.sleep(HEARTBEAT_PUMP_INTERVAL_S)

    heartbeat_pump = asyncio.ensure_future(_pump_heartbeat())
    try:
        await closed_event.wait()
    finally:
        # Bug fix (2026-10-13), confirmed live: this used to only clear
        # Python's own bookkeeping (_sidecar[account] = None) and never
        # actually terminated the Node.js child process. closed_event is
        # only ever set by on_closed (the sidecar's OWN stdout pipe
        # closing) -- if THIS coroutine instead exits because it was
        # CANCELLED from outside (e.g. task_supervisor.py's supervise()
        # deciding the loop stalled and restarting it), closed_event never
        # fires, so the Node process this function spawned was never asked
        # to stop -- it kept running, orphaned, holding the exact same
        # WhatsApp session the next _run_sidecar call immediately spawns a
        # NEW process for. Confirmed live via `Get-CimInstance Win32_Process`:
        # 4 whatsapp-sidecar/index.js processes running at once (2 per
        # account instead of 1), all children of the same backend, each
        # pair endlessly kicking the other off WhatsApp's servers
        # (statusCode=440 "Stream Errored (conflict)", every ~5-10s,
        # forever) -- not two separate incidents, the same orphan fighting
        # its own replacement. sidecar.stop() is idempotent (checks
        # poll() first), so calling it here even when on_closed already
        # fired (the process is already gone) is a safe no-op.
        sidecar.stop()
        heartbeat_pump.cancel()
        _sidecar[account] = None
        _status[account].connected = False
        for fut in _pending[account].values():
            if not fut.done():
                fut.set_exception(WhatsAppError("WhatsApp sidecar disconnected"))
        _pending[account].clear()


def _env_for_sidecar() -> dict[str, str]:
    env = dict(os.environ)
    return env


def _handle_qr(workspace_dir: str, account: str, data: str) -> None:
    if not data:
        return
    try:
        import qrcode

        img = qrcode.make(data)
        path = _qr_image_path(workspace_dir, account)
        img.save(path)
        log_event("plugin:whatsapp", "qr_ready", account=account, path=str(path))
    except Exception as exc:
        log_event("plugin:whatsapp", "qr_render_failed", account=account, error=str(exc))
        return
    send = get_send_for_tab(_login_requested_tab_id[account])
    if send is None:
        log_event("plugin:whatsapp", "qr_no_tab_to_notify", account=account)
        return
    # Fixed path, same requestId reused isn't needed -- MainWindow.xaml.cs's
    # open_editor handler keys its already-open-viewer check on PATH, not
    # requestId, so a repeat send here (a refreshed, still-unscanned QR)
    # updates the same window in place instead of opening another one.
    asyncio.ensure_future(send({"type": "open_editor", "requestId": uuid.uuid4().hex, "path": str(path), "kind": "image"}))


def _close_qr_viewer(workspace_dir: str, account: str) -> None:
    send = get_send_for_tab(_login_requested_tab_id[account])
    if send is None:
        return
    asyncio.ensure_future(send({"type": "close_editor", "path": str(_qr_image_path(workspace_dir, account))}))


def start_whatsapp_channel(workspace_dir: str, inject_proactive: Callable[[str], None]) -> list[asyncio.Task[None]]:
    """Startup loop, one per account: if a session already exists (or
    whatsapp_login was called), connect; otherwise re-check every 30s
    (cheap file read) until one of those happens. The two accounts are
    fully independent -- one can be linked and running while the other
    has never logged in, same as telegram_channel.py's own two loops."""
    def _make_loop(account: str) -> Callable[[Callable[[], None]], Any]:
        async def _loop(touch: Callable[[], None]) -> None:
            while True:
                if not (_login_requested[account] or _has_prior_session(workspace_dir, account)):
                    # Idly waiting for whatsapp_login to ever be called for
                    # this account is normal, indefinite, healthy -- not a stall.
                    touch()
                    await asyncio.sleep(30.0)
                    continue
                try:
                    await _run_sidecar(workspace_dir, account, inject_proactive, touch)
                except WhatsAppError as exc:
                    _status[account].last_error = str(exc)
                    log_event("plugin:whatsapp", "sidecar_unavailable", account=account, error=str(exc))
                    touch()  # a deliberate backoff wait, not a stall
                    await asyncio.sleep(60.0)
                    continue
                except Exception as exc:
                    _status[account].last_error = str(exc)
                    log_event("plugin:whatsapp", "sidecar_failed", account=account, error=str(exc))
                touch()
                await asyncio.sleep(5.0)
        return _loop

    return [
        supervise(f"whatsapp_channel_{account}", _make_loop(account), heartbeat_timeout_s=CHANNEL_HEARTBEAT_TIMEOUT_S)
        for account in ACCOUNTS
    ]
