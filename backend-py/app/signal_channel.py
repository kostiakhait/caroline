"""app/signal_channel.py -- Signal personal-account connections via
signal-cli, driven as a JSON-RPC daemon over a local TCP socket (NOT
stdio, unlike the WhatsApp sidecar -- see app/sidecar_process.py's own
doc comment for why that distinction matters here). Fifth and last of
the planned messenger integrations (docs/MESSENGER_INTEGRATIONS_PLAN.md,
2026-10-06).

TWO separate accounts, not one (decision 2026-10-11, same shape as
telegram_channel.py's own "self"/"owner" split, added once the
single-account design shipped here turned out to just be incomplete, not
a real protocol limitation): "self" is Caroline's OWN Signal account --
her own number, linked once like any other Signal linked device, she may
read and reply there as herself, no extra permission needed; "owner" is
the human owner's own personal Signal account, linked so Caroline can read
it and keep them posted, but may send AS the owner only when they have
explicitly asked her to, for that specific message. Each account gets its
own signal-cli `--config` directory and its own daemon TCP port -- two
fully independent linked devices, same as running signal-cli for two
different people would be. The one real external prerequisite, same class
as Telegram's own app registration at my.telegram.org: "self" needs an
actual second phone number with an active Signal installation to scan the
link QR, whenever one is available -- the code is ready regardless.

Signal has no bot/app API at all -- signal-cli (a widely-used, community-
maintained tool, not Caroline-specific) is the standard way any third-
party integration talks to a personal Signal account. Two phases, per
account:

1. LINKING (one-time): `signal-cli link -n Caroline` prints a
   "tsdevice:/..." URI on stdout; rendered to a QR code (same
   open-in-Caroline's-own-viewer approach as whatsapp_channel.py, fixed
   2026-10-11 -- this used to pop the OS default image viewer) for the
   user to scan in Signal's own app (Settings > Linked Devices > Link New
   Device). The process exits once scanned, having registered a local
   account under that account's own --config.
2. DAEMON (every run after linking): `signal-cli -a <number> daemon
   --tcp 127.0.0.1:<port>` serves newline-delimited JSON-RPC 2.0 over a
   plain TCP socket -- connected to directly via asyncio.open_connection,
   no subprocess stdio piping needed for the IPC itself (the daemon
   process is simply kept running in the background). Each account's
   daemon binds its own port (DAEMON_PORT_BY_ACCOUNT) so both can run at
   once without colliding.

Exact signal-cli CLI flags and JSON-RPC method/notification shapes here
follow its documented conventions as of this writing but have NOT been
exercised against a live install in the environment this was written in
-- verify against the actually-bundled signal-cli version before relying
on this in production, same caveat as whatsapp-sidecar/index.js's own
doc comment.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.logging_setup import log_event
from app.file_guard import download_target_refusal, outbound_file_refusal
from app.untrusted_text import third_party_label, third_party_text
from app.secret_store import messenger_secrets_dir
from app.session_context import get_send_for_tab, get_tab_id
from app.task_supervisor import supervise
from app.workspace_dir import WORKSPACE_DIR

ACCOUNTS = ("self", "owner")
DEFAULT_ACCOUNT = "self"
_ACCOUNT_LABEL = {"self": "your own", "owner": "the owner's"}

JAVA_HOME = os.environ.get("CAROLINE_JAVA_HOME", "")
SIGNAL_CLI_BAT = os.environ.get("CAROLINE_SIGNAL_CLI_BAT", "")
DAEMON_PORT_BY_ACCOUNT = {"self": 7592, "owner": 7593}
DAEMON_READY_TIMEOUT_S = 20.0
RPC_TIMEOUT_S = 30.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Opt-in heartbeat watchdog (task_supervisor.py's supervise(), 2026-10-11,
# after a real Telegram incident of the same shape -- see telegram_
# channel.py's own docstring). Linking (signal-cli link, the human-paced
# QR scan) is a one-shot subprocess started by start_login(), entirely
# separate from this loop -- the daemon loop this watches only ever runs
# once a number is already linked, same "no legitimate long human wait"
# shape as Telegram's own reconnect path, hence the same tight timeout.
CHANNEL_HEARTBEAT_TIMEOUT_S = 60.0
HEARTBEAT_PUMP_INTERVAL_S = 10.0


class SignalError(Exception):
    pass


def is_valid_account(account: str) -> bool:
    return account in ACCOUNTS


def _check_account(account: str) -> str:
    if account not in ACCOUNTS:
        raise SignalError(f'"account" must be one of {", ".join(ACCOUNTS)}, not {account!r}.')
    return account


def _config_dir(workspace_dir: str, account: str) -> Path:
    d = messenger_secrets_dir(workspace_dir, "signal") / f"config_{account}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _number_file(workspace_dir: str, account: str) -> Path:
    return messenger_secrets_dir(workspace_dir, "signal") / f"linked_number_{account}.txt"


def _qr_image_path(workspace_dir: str, account: str) -> Path:
    return messenger_secrets_dir(workspace_dir, "signal") / f"link-qr-{account}.png"


def _linked_number(workspace_dir: str, account: str) -> str | None:
    try:
        return _number_file(workspace_dir, account).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def _env_for_subprocess() -> dict[str, str]:
    env = dict(os.environ)
    if JAVA_HOME:
        env["JAVA_HOME"] = JAVA_HOME
    return env


def _require_tooling() -> None:
    # Linux parity pass (2026-10-21): the Linux build provisions signal-cli's
    # own "-Linux-native" (GraalVM native-image) release -- self-contained,
    # no separate JRE, so no CAROLINE_JAVA_HOME to check there at all (see
    # supervisor.py's _child_env and linux-shell/provision_runtime.sh).
    if not SIGNAL_CLI_BAT or not Path(SIGNAL_CLI_BAT).exists():
        raise SignalError("Signal isn't set up in this build yet -- signal-cli wasn't provisioned. Reinstall Caroline.")
    if sys.platform == "win32" and (not JAVA_HOME or not Path(JAVA_HOME).exists()):
        raise SignalError("Signal isn't set up in this build yet -- Java wasn't provisioned. Reinstall Caroline.")


async def start_login(workspace_dir: str, account: str) -> str:
    """Runs `signal-cli link` as a one-shot subprocess, reads the pairing
    URI from its stdout, and renders+opens it as a QR image in Caroline's
    own viewer window -- same reasoning as whatsapp_channel.py's own
    start_login for not blocking synchronously on the user actually
    scanning it: this kicks off the link attempt and returns immediately;
    the background loop notices the resulting linked number and starts
    the daemon on its own next pass.

    Captures the calling tab's id (get_tab_id() is only valid here, inside
    a real tool call) so the QR lands in the window of whichever tab
    asked to connect, not an arbitrary one."""
    _check_account(account)
    _require_tooling()
    _login_requested_tab_id[account] = get_tab_id()
    config_dir = _config_dir(workspace_dir, account)
    proc = await asyncio.create_subprocess_exec(
        SIGNAL_CLI_BAT, "--config", str(config_dir), "link", "-n", "Caroline",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_env_for_subprocess(), creationflags=_NO_WINDOW,
    )

    async def _watch() -> None:
        assert proc.stdout is not None
        uri_found = False
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("tsdevice:") or line.startswith("sgnl://"):
                uri_found = True
                _render_and_open_qr(workspace_dir, account, line)
        code = await proc.wait()
        if code == 0:
            await _record_linked_number(workspace_dir, account, config_dir)
            log_event("plugin:signal", "link_succeeded", account=account)
            _close_qr_viewer(workspace_dir, account)
        elif not uri_found:
            stderr = (await proc.stderr.read()).decode("utf-8", errors="replace") if proc.stderr else ""
            log_event("plugin:signal", "link_failed_no_qr", account=account, stderr=stderr[:500])
        else:
            log_event("plugin:signal", "link_process_exited_nonzero", account=account, code=code)

    asyncio.create_task(_watch())
    return (
        f'Starting Signal linking ("{account}") -- a QR code image will open automatically in a moment. Scan it '
        "in the Signal app on the phone for that account (Settings > Linked Devices > Link New Device)."
    )


def _render_and_open_qr(workspace_dir: str, account: str, data: str) -> None:
    """Opens in Caroline's own floating viewer window (fixed 2026-10-11,
    same reuse-in-place fix as whatsapp_channel.py's own QR handling --
    see MainWindow.xaml.cs's open_editor handler), not the OS default
    image viewer this used to pop."""
    try:
        import qrcode

        img = qrcode.make(data)
        path = _qr_image_path(workspace_dir, account)
        img.save(path)
        log_event("plugin:signal", "qr_ready", account=account, path=str(path))
    except Exception as exc:
        log_event("plugin:signal", "qr_render_failed", account=account, error=str(exc))
        return
    send = get_send_for_tab(_login_requested_tab_id.get(account))
    if send is None:
        log_event("plugin:signal", "qr_no_tab_to_notify", account=account)
        return
    asyncio.ensure_future(send({"type": "open_editor", "requestId": uuid.uuid4().hex, "path": str(path), "kind": "image"}))


def _close_qr_viewer(workspace_dir: str, account: str) -> None:
    send = get_send_for_tab(_login_requested_tab_id.get(account))
    if send is None:
        return
    asyncio.ensure_future(send({"type": "close_editor", "path": str(_qr_image_path(workspace_dir, account))}))


async def _record_linked_number(workspace_dir: str, account: str, config_dir: Path) -> None:
    """`signal-cli listAccounts` reports every locally-registered account
    under --config; each account's --config dir here only ever links one
    number, so the first (only) result is the number that account's
    daemon should bind to."""
    proc = await asyncio.create_subprocess_exec(
        SIGNAL_CLI_BAT, "--config", str(config_dir), "listAccounts",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=_env_for_subprocess(), creationflags=_NO_WINDOW,
    )
    out, _ = await proc.communicate()
    text = out.decode("utf-8", errors="replace")
    # Each line looks roughly like "Number: +1234567890 ..." -- take the
    # first token that parses as a phone number (leading '+', digits).
    for line in text.splitlines():
        for token in line.split():
            if token.startswith("+") and token[1:].isdigit():
                _number_file(workspace_dir, account).write_text(token, encoding="utf-8")
                return
    log_event("plugin:signal", "could_not_parse_linked_number", account=account, output=text[:500])


@dataclass
class SignalChannelStatus:
    account: str = ""
    linked: bool = False
    connected: bool = False
    own_number: str | None = None
    last_event_at_iso: str | None = None
    last_error: str | None = None


_status: dict[str, SignalChannelStatus] = {account: SignalChannelStatus(account=account) for account in ACCOUNTS}
_writer: dict[str, asyncio.StreamWriter | None] = {account: None for account in ACCOUNTS}
_pending: dict[str, dict[int, asyncio.Future[Any]]] = {account: {} for account in ACCOUNTS}
_next_id: dict[str, int] = {account: 0 for account in ACCOUNTS}
# Which tab's start_login call should receive that account's QR window --
# same reasoning as whatsapp_channel.py's own _login_requested_tab_id.
_login_requested_tab_id: dict[str, str | None] = {account: None for account in ACCOUNTS}


def get_signal_channel_status() -> dict[str, dict[str, Any]]:
    return {account: asdict(status) for account, status in _status.items()}


def is_connected(account: str) -> bool:
    return _writer.get(account) is not None and _status[account].connected


async def _rpc_call(account: str, method: str, params: dict[str, Any]) -> Any:
    if not is_connected(account):
        raise SignalError(f'Signal ("{account}") isn\'t connected yet -- use signal_login(account="{account}") first.')
    _next_id[account] += 1
    req_id = _next_id[account]
    fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
    _pending[account][req_id] = fut
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}) + "\n"
    writer = _writer[account]
    assert writer is not None
    writer.write(body.encode("utf-8"))
    await writer.drain()
    try:
        result = await asyncio.wait_for(fut, timeout=RPC_TIMEOUT_S)
    except asyncio.TimeoutError:
        _pending[account].pop(req_id, None)
        raise SignalError(f"No response from signal-cli within {int(RPC_TIMEOUT_S)}s.")
    return result


# A Signal username: 3-32 chars, letters/digits/underscore, a literal "."
# then a numeric discriminator (e.g. "alice.01") -- added by Signal
# specifically so people can be reached with no phone number shared at all.
# The leading "u:" people sometimes already type is optional and stripped
# before matching, matching signal-cli's own recipient-argument convention
# (any RECIPIENT place accepts phone / ACI / "PNI:..." / "u:username").
_USERNAME = re.compile(r"^(?:u:)?[A-Za-z_][A-Za-z0-9_]{2,31}\.\d{1,9}$")


def _is_group_id(target: str) -> bool:
    """signal-cli recipients are always E.164 phone numbers (a leading
    '+') or a "u:username" (see _USERNAME above); group ids are opaque
    base64 strings with neither shape -- the same shape-based distinction
    telegram_channel.py and whatsapp_channel.py each already use for their
    own id-vs-phone split, so every to/target param here can take any of
    the three without a separate flag."""
    return not target.startswith("+") and not _USERNAME.match(target)


def _recipient_value(target: str) -> str:
    """signal-cli's own recipient syntax requires the "u:" prefix on a bare
    username (confirmed against its own RECIPIENT-argument docs: phone /
    ACI / "PNI:..." / "u:username" are the only forms it accepts there) --
    add it if the caller didn't already."""
    if _USERNAME.match(target) and not target.startswith("u:"):
        return f"u:{target}"
    return target


async def send_message(account: str, recipient: str, text: str) -> None:
    key = "groupId" if _is_group_id(recipient) else "recipient"
    value = recipient if key == "groupId" else _recipient_value(recipient)
    await _rpc_call(account, "send", {key: [value], "message": text})


async def list_contacts(account: str) -> list[dict[str, Any]]:
    result = await _rpc_call(account, "listContacts", {})
    return result if isinstance(result, list) else []


# Attachments (2026-10-11). signal-cli's own send/getAttachment commands
# (confirmed against the real v0.14.9 source: SendCommand.java,
# GetAttachmentCommand.java, JsonAttachment.java) move a local file as a
# plain path on the way out, and base64 inside the JSON-RPC response on
# the way back in -- there is no local disk path this process can just
# read directly for a received attachment (getAttachment is the only
# supported way to retrieve one), unlike Telegram/WhatsApp.
SIGNAL_DOWNLOADS_DIR = Path(WORKSPACE_DIR) / "signal_downloads"


async def send_file(account: str, to: str, path: str, caption: str | None = None) -> None:
    if refusal := outbound_file_refusal(path):
        raise SignalError(refusal)
    if not Path(path).is_file():
        raise SignalError(f"No such local file: {path}")
    key = "groupId" if _is_group_id(to) else "recipient"
    value = to if key == "groupId" else _recipient_value(to)
    params: dict[str, Any] = {key: [value], "attachment": [path]}
    if caption:
        params["message"] = caption
    await _rpc_call(account, "send", params)


async def download_attachment(account: str, ref: str, save_path: str | None = None) -> str:
    """`ref` is "<attachment id>|<source>" (the source recipient number or
    group id the attachment arrived from -- getAttachment requires one,
    mutually exclusive with the other, to look it up), as handed out in
    the proactive notification built by _dispatch_rpc_line below."""
    if save_path and (refusal := download_target_refusal(save_path)):
        raise SignalError(refusal)
    try:
        attachment_id, source = ref.split("|", 1)
    except ValueError:
        raise SignalError(f'Malformed attachment ref {ref!r} -- expected "<id>|<source>" from a proactive notification.')
    key = "groupId" if _is_group_id(source) else "recipient"
    result = await _rpc_call(account, "getAttachment", {"id": attachment_id, key: source})
    data = (result or {}).get("data") if isinstance(result, dict) else None
    if not data:
        raise SignalError(f"signal-cli returned no data for attachment {attachment_id}.")
    if not save_path:
        SIGNAL_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        save_path = str(SIGNAL_DOWNLOADS_DIR / attachment_id)
    Path(save_path).write_bytes(base64.b64decode(data))
    return save_path


# Contacts (2026-10-11). Like WhatsApp, Signal has no "add contact" call
# distinct from messaging someone -- any phone number can be messaged
# directly. updateContact sets a local display name on a recipient
# (creating a local contact entry for them if one didn't exist yet, the
# closest Signal equivalent to Telegram's telegram_add_contact); block/
# unblock work on both individual recipients and groups.
async def update_contact_name(account: str, recipient: str, name: str | None = None, given_name: str | None = None, family_name: str | None = None) -> None:
    params: dict[str, Any] = {"recipient": _recipient_value(recipient)}
    if name:
        params["name"] = name
    if given_name:
        params["givenName"] = given_name
    if family_name:
        params["familyName"] = family_name
    await _rpc_call(account, "updateContact", params)


async def set_block(account: str, target: str, block: bool) -> None:
    key = "groupId" if _is_group_id(target) else "recipient"
    value = target if key == "groupId" else _recipient_value(target)
    await _rpc_call(account, "block" if block else "unblock", {key: [value]})


# Groups (2026-10-11). Signal groups are fully decentralized -- every
# member keeps their own independent copy, so there is no server call that
# deletes a group "for everyone", not even for its creator (unlike
# Telegram, and unlike even WhatsApp's own best-effort). delete_group
# below approximates the same way whatsapp_channel.py's own delete_group
# does: remove every other member this account can, then quit for good.
async def create_group(account: str, name: str, members: list[str]) -> str:
    result = await _rpc_call(account, "updateGroup", {"name": name, "member": members})
    group_id = (result or {}).get("groupId")
    if not group_id:
        raise SignalError("signal-cli didn't return a groupId for the new group.")
    return group_id


async def update_group_members(
    account: str, group_id: str, *, add: list[str] | None = None, remove: list[str] | None = None,
    promote: list[str] | None = None, demote: list[str] | None = None,
) -> None:
    params: dict[str, Any] = {"groupId": group_id}
    if add:
        params["member"] = add
    if remove:
        params["removeMember"] = remove
    if promote:
        params["admin"] = promote
    if demote:
        params["removeAdmin"] = demote
    await _rpc_call(account, "updateGroup", params)


async def list_groups(account: str) -> list[dict[str, Any]]:
    result = await _rpc_call(account, "listGroups", {"detailed": True})
    return result if isinstance(result, list) else []


async def quit_group(account: str, group_id: str, *, delete: bool = False, new_admins: list[str] | None = None) -> None:
    params: dict[str, Any] = {"groupId": group_id, "delete": delete}
    if new_admins:
        params["admin"] = new_admins
    await _rpc_call(account, "quitGroup", params)


async def delete_group(account: str, group_id: str) -> None:
    groups = await list_groups(account)
    members: list[str] = []
    for g in groups:
        if g.get("id") == group_id:
            members = [m.get("number") for m in (g.get("members") or []) if isinstance(m, dict) and m.get("number")]
            break
    if members:
        await update_group_members(account, group_id, remove=members)
    await quit_group(account, group_id, delete=True)


async def _spawn_daemon(workspace_dir: str, account: str) -> subprocess.Popen[bytes]:
    number = _linked_number(workspace_dir, account)
    assert number
    config_dir = _config_dir(workspace_dir, account)
    port = DAEMON_PORT_BY_ACCOUNT[account]
    return subprocess.Popen(
        [SIGNAL_CLI_BAT, "--config", str(config_dir), "-a", number, "daemon", "--tcp", f"127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=_env_for_subprocess(), creationflags=_NO_WINDOW,
    )


async def _run_daemon_connection(workspace_dir: str, account: str, inject_proactive: Callable[[str], None], touch: Callable[[], None] = lambda: None) -> None:
    """`touch()` is supervise()'s heartbeat callback (2026-10-11, after a
    real Telegram incident of the same shape -- see telegram_channel.py's
    own docstring for the full story). Called once the daemon connection
    is up, then on every received line for as long as it stays up."""
    _require_tooling()
    number = _linked_number(workspace_dir, account)
    if not number:
        return

    daemon_proc = await _spawn_daemon(workspace_dir, account)
    port = DAEMON_PORT_BY_ACCOUNT[account]
    try:
        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        deadline = asyncio.get_event_loop().time() + DAEMON_READY_TIMEOUT_S
        while asyncio.get_event_loop().time() < deadline:
            if daemon_proc.poll() is not None:
                raise SignalError(f"signal-cli daemon exited early (code {daemon_proc.returncode}) before accepting connections.")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.5)
        if reader is None or writer is None:
            raise SignalError(f"signal-cli daemon didn't start listening on 127.0.0.1:{port} within {int(DAEMON_READY_TIMEOUT_S)}s.")

        _writer[account] = writer
        _status[account].connected = True
        _status[account].linked = True
        _status[account].own_number = number
        log_event("plugin:signal", "daemon_connected", account=account, number=number)
        touch()

        async def _pump_heartbeat() -> None:
            # readline() touches on every actual line, but a quiet Signal
            # account can legitimately go long stretches with nothing
            # arriving at all -- this pump is what keeps THAT healthy,
            # idle wait from false-positiving as a stall.
            while True:
                touch()
                await asyncio.sleep(HEARTBEAT_PUMP_INTERVAL_S)

        heartbeat_pump = asyncio.ensure_future(_pump_heartbeat())
        try:
            while True:
                raw = await reader.readline()
                touch()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                _dispatch_rpc_line(account, msg, inject_proactive)
        finally:
            heartbeat_pump.cancel()
    finally:
        _writer[account] = None
        _status[account].connected = False
        for fut in _pending[account].values():
            if not fut.done():
                fut.set_exception(SignalError("Signal daemon disconnected"))
        _pending[account].clear()
        if daemon_proc.poll() is None:
            daemon_proc.terminate()


def _dispatch_rpc_line(account: str, msg: dict[str, Any], inject_proactive: Callable[[str], None]) -> None:
    if "id" in msg and msg.get("id") in _pending[account]:
        fut = _pending[account].pop(msg["id"])
        if fut.done():
            return
        if "error" in msg:
            fut.set_exception(SignalError(str(msg["error"])))
        else:
            fut.set_result(msg.get("result"))
        return
    if msg.get("method") == "receive":
        envelope = (msg.get("params") or {}).get("envelope") or {}
        data_message = envelope.get("dataMessage") or {}
        text = (data_message.get("message") or "").strip()
        attachments = data_message.get("attachments") or []
        # 2026-10-11: a media-only message (no text body) used to be
        # dropped here entirely -- notified now the same way Telegram's
        # and WhatsApp's own channels were changed to, so a bare photo/
        # file is never silently invisible on any messenger.
        if not text and not attachments:
            return
        group_info = data_message.get("groupInfo")
        source = envelope.get("sourceNumber") or envelope.get("source") or "unknown"
        name = third_party_label(envelope.get("sourceName") or source)
        target = (group_info or {}).get("groupId") or source
        kind = f'group "{third_party_label(group_info.get("groupName") or target)}"' if group_info else "DM"
        _status[account].last_event_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        log_event("plugin:signal", "message_received", account=account, source=source, kind=kind, has_media=bool(attachments))
        note = f'[New Signal {kind} message in {_ACCOUNT_LABEL[account]} Signal account, from "{name}" ({source}, account="{account}"):\n{third_party_text(text, name) if text else "(no text)"}'
        for att in attachments:
            att_id = att.get("id")
            if not att_id:
                continue
            ref = f"{att_id}|{target}"
            note += f'\nAttached: {_describe_attachment(att)}. Use signal_download_attachment(ref="{ref}", account="{account}") to save it locally first.'
        if account == "self":
            note += (
                "\nThis is Caroline's own separate Signal account, not the user's -- replying here is "
                "Caroline's own correspondence, not something done on the user's behalf, and needs no extra "
                f'permission. Reply with signal_send_message(to="{target}", text=..., account="self") if it '
                "needs a response.]"
            )
        else:
            note += (
                "\nThis is the OWNER'S OWN correspondence, not yours -- read it and tell the owner about it if "
                "it matters, but do not reply as them unless they have explicitly asked you to send this "
                f'specific reply (signal_send_message(to="{target}", text=..., account="owner")).]'
            )
        inject_proactive(note)


def _describe_attachment(att: dict[str, Any]) -> str:
    if att.get("isVoiceNote"):
        return "voice message"
    content_type = (att.get("contentType") or "").lower()
    if content_type.startswith("image/"):
        return "photo"
    if content_type.startswith("video/"):
        return "video"
    if content_type.startswith("audio/"):
        return "audio"
    name = att.get("filename")
    return f'file "{name}"' if name else "file"


def start_signal_channel(workspace_dir: str, inject_proactive: Callable[[str], None]) -> list[asyncio.Task[None]]:
    """Startup loop, one per account: if a number is already linked,
    connect; otherwise re-check every 30s (cheap file read) until
    start_login has recorded one. The two accounts are fully independent
    -- one can be linked and running while the other has never logged in,
    same as telegram_channel.py's own two loops."""
    def _make_loop(account: str) -> Callable[[Callable[[], None]], Any]:
        async def _loop(touch: Callable[[], None]) -> None:
            while True:
                if not _linked_number(workspace_dir, account):
                    # Idly waiting for this account to ever be linked is
                    # normal, indefinite, healthy -- not a stall.
                    touch()
                    await asyncio.sleep(30.0)
                    continue
                try:
                    await _run_daemon_connection(workspace_dir, account, inject_proactive, touch)
                except SignalError as exc:
                    _status[account].last_error = str(exc)
                    log_event("plugin:signal", "daemon_unavailable", account=account, error=str(exc))
                    touch()  # a deliberate backoff wait, not a stall
                    await asyncio.sleep(60.0)
                    continue
                except Exception as exc:
                    _status[account].last_error = str(exc)
                    log_event("plugin:signal", "daemon_failed", account=account, error=str(exc))
                touch()
                await asyncio.sleep(5.0)
        return _loop

    return [
        supervise(f"signal_channel_{account}", _make_loop(account), heartbeat_timeout_s=CHANNEL_HEARTBEAT_TIMEOUT_S)
        for account in ACCOUNTS
    ]
