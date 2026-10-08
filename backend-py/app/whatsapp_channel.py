"""app/whatsapp_channel.py -- WhatsApp personal-account connection via a
Node sidecar (Baileys; see companion-apps/whatsapp-sidecar/index.js's own
doc comment for the full wire protocol and why Node, not Python -- there
is no official API for a personal WhatsApp account, Baileys is an
unofficial reimplementation of the WhatsApp Web protocol). Fourth of the
planned messenger integrations (docs/MESSENGER_INTEGRATIONS_PLAN.md,
2026-10-06), and the first to need an external runtime -- see
app/sidecar_process.py for the stdio transport this rides on.

Linking shows a QR code: rendered to a PNG under workspace_dir and opened
with the OS default viewer (files_plugin.open_file_with_default_app) --
no new native UI needed, reusing what already exists.

Baileys' own multi-file auth-state (created on first link) lives under
workspace_dir/messengers/whatsapp/auth/ -- not individually DPAPI-
encrypted like a single token (Baileys owns the shape of those files,
not this module), but still under the same messengers/ root
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
from app.plugins.files_plugin import open_file_with_default_app
from app.secret_store import messenger_secrets_dir
from app.sidecar_process import SidecarProcess
from app.task_supervisor import supervise
from app.workspace_dir import WORKSPACE_DIR

import os

NODE_EXE = os.environ.get("CAROLINE_NODE_PATH", "")
SIDECAR_DIR = os.environ.get("CAROLINE_WHATSAPP_SIDECAR_DIR", "")
SEND_TIMEOUT_S = 30.0


class WhatsAppError(Exception):
    pass


def _auth_dir(workspace_dir: str) -> Path:
    d = messenger_secrets_dir(workspace_dir, "whatsapp") / "auth"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _qr_image_path(workspace_dir: str) -> Path:
    return messenger_secrets_dir(workspace_dir, "whatsapp") / "link-qr.png"


@dataclass
class WhatsAppChannelStatus:
    linked: bool = False
    connected: bool = False
    own_jid: str | None = None
    last_event_at_iso: str | None = None
    last_error: str | None = None


_status = WhatsAppChannelStatus()
_sidecar: SidecarProcess | None = None
_pending: dict[str, asyncio.Future[Any]] = {}
# Per explicit instruction (2026-10-06): never start the sidecar (and so
# never pop a QR image open) until the user has actually asked to connect
# WhatsApp -- a fresh install must not auto-launch Node and show a QR
# nobody requested. True for the rest of this process's life once set,
# same as "has credentials" gates Slack/Telegram/Discord's own loops --
# the difference here is WhatsApp's own "credential" (the auth-state dir)
# doesn't exist yet on a first-ever link, so an explicit flag covers that
# one case; every later reconnect (including across a Caroline restart,
# once linked once) goes through the normal auth-dir check.
_login_requested = False


def _has_prior_session(workspace_dir: str) -> bool:
    try:
        return any(_auth_dir(workspace_dir).iterdir())
    except OSError:
        return False


def get_whatsapp_channel_status() -> dict[str, Any]:
    return asdict(_status)


def is_connected() -> bool:
    return _sidecar is not None and not _sidecar.closed and _status.connected


async def send_message(to: str, text: str) -> None:
    if not is_connected():
        raise WhatsAppError("WhatsApp isn't connected yet -- use whatsapp_login first.")
    op_id = uuid.uuid4().hex
    fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
    _pending[op_id] = fut
    assert _sidecar is not None
    _sidecar.send({"cmd": "sendMessage", "id": op_id, "to": to, "text": text})
    try:
        result = await asyncio.wait_for(fut, timeout=SEND_TIMEOUT_S)
    except asyncio.TimeoutError:
        _pending.pop(op_id, None)
        raise WhatsAppError(f"No response from WhatsApp within {int(SEND_TIMEOUT_S)}s.")
    if not result.get("ok"):
        raise WhatsAppError(result.get("error") or "unknown error")


async def list_chats() -> list[dict[str, Any]]:
    if not is_connected():
        raise WhatsAppError("WhatsApp isn't connected yet -- use whatsapp_login first.")
    op_id = uuid.uuid4().hex
    fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
    _pending[op_id] = fut
    assert _sidecar is not None
    _sidecar.send({"cmd": "listChats", "id": op_id})
    try:
        result = await asyncio.wait_for(fut, timeout=SEND_TIMEOUT_S)
    except asyncio.TimeoutError:
        _pending.pop(op_id, None)
        raise WhatsAppError(f"No response from WhatsApp within {int(SEND_TIMEOUT_S)}s.")
    return result.get("chats") or []


async def start_login(workspace_dir: str) -> str:
    """Sets the flag that lets start_whatsapp_channel's loop actually spawn
    the sidecar -- the QR itself appears (as an opened image) once it
    connects and emits one, not synchronously from this call. Doesn't wait
    inside the tool call for something that can take a while and needs the
    user to act in between (scan the code)."""
    global _login_requested
    if not NODE_EXE or not Path(NODE_EXE).exists():
        raise WhatsAppError("WhatsApp isn't set up in this build yet -- Node.js wasn't provisioned.")
    if is_connected():
        return f"Already connected (as {_status.own_jid})."
    _login_requested = True
    return (
        "Starting WhatsApp -- a QR code image will open automatically in a moment. Scan it with WhatsApp on "
        "your phone (Linked Devices > Link a Device). Real risk, stated plainly: WhatsApp has no official API "
        "for a personal account, so this uses an unofficial protocol library -- WhatsApp could in principle "
        "flag or restrict the account for it, at their own discretion."
    )


async def _run_sidecar(workspace_dir: str, inject_proactive: Callable[[str], None]) -> None:
    global _sidecar
    if not NODE_EXE or not Path(NODE_EXE).exists():
        raise WhatsAppError("Node.js runtime not found -- reinstall Caroline to provision it.")
    if not SIDECAR_DIR or not (Path(SIDECAR_DIR) / "index.js").exists():
        raise WhatsAppError("whatsapp-sidecar not found -- reinstall Caroline.")

    closed_event = asyncio.Event()

    def on_line(msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "qr":
            _handle_qr(workspace_dir, str(msg.get("data") or ""))
        elif msg_type == "ready":
            _status.connected = True
            _status.linked = True
            _status.own_jid = msg.get("jid")
            log_event("plugin:whatsapp", "connected", jid=_status.own_jid)
        elif msg_type == "loggedOut":
            _status.connected = False
            _status.linked = False
            log_event("plugin:whatsapp", "logged_out")
        elif msg_type == "message":
            if msg.get("fromMe"):
                return
            text = (msg.get("text") or "").strip()
            if not text:
                return
            frm = msg.get("from") or "unknown"
            name = msg.get("chatName") or frm
            _status.last_event_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            log_event("plugin:whatsapp", "message_received", frm=frm)
            inject_proactive(
                f'[New WhatsApp message from "{name}" (jid={frm}) -- reply with '
                f"whatsapp_send_message(to=\"{frm}\", text=...) if it needs a response:\n{text}]"
            )
        elif msg_type in ("sendResult", "chats"):
            op_id = msg.get("id")
            fut = _pending.pop(op_id, None) if op_id else None
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif msg_type == "error":
            log_event("plugin:whatsapp", "sidecar_reported_error", error=msg.get("message"))

    def on_closed() -> None:
        _status.connected = False
        closed_event.set()

    sidecar = SidecarProcess(
        argv=[NODE_EXE, str(Path(SIDECAR_DIR) / "index.js")],
        env={**_env_for_sidecar(), "WHATSAPP_AUTH_DIR": str(_auth_dir(workspace_dir))},
        on_line=on_line,
        on_closed=on_closed,
        label="whatsapp",
        cwd=SIDECAR_DIR,
    )
    sidecar.start()
    _sidecar = sidecar
    try:
        await closed_event.wait()
    finally:
        _sidecar = None
        _status.connected = False
        for fut in _pending.values():
            if not fut.done():
                fut.set_exception(WhatsAppError("WhatsApp sidecar disconnected"))
        _pending.clear()


def _env_for_sidecar() -> dict[str, str]:
    env = dict(os.environ)
    return env


def _handle_qr(workspace_dir: str, data: str) -> None:
    if not data:
        return
    try:
        import qrcode

        img = qrcode.make(data)
        path = _qr_image_path(workspace_dir)
        img.save(path)
        log_event("plugin:whatsapp", "qr_ready", path=str(path))
        open_file_with_default_app(str(path))
    except Exception as exc:
        log_event("plugin:whatsapp", "qr_render_failed", error=str(exc))


def start_whatsapp_channel(workspace_dir: str, inject_proactive: Callable[[str], None]) -> asyncio.Task[None]:
    async def _loop() -> None:
        while True:
            if not (_login_requested or _has_prior_session(workspace_dir)):
                await asyncio.sleep(30.0)
                continue
            try:
                await _run_sidecar(workspace_dir, inject_proactive)
            except WhatsAppError as exc:
                _status.last_error = str(exc)
                log_event("plugin:whatsapp", "sidecar_unavailable", error=str(exc))
                await asyncio.sleep(60.0)
                continue
            except Exception as exc:
                _status.last_error = str(exc)
                log_event("plugin:whatsapp", "sidecar_failed", error=str(exc))
            await asyncio.sleep(5.0)

    return supervise("whatsapp_channel", _loop)
