"""app/signal_channel.py -- Signal personal-account connection via
signal-cli, driven as a JSON-RPC daemon over a local TCP socket (NOT
stdio, unlike the WhatsApp sidecar -- see app/sidecar_process.py's own
doc comment for why that distinction matters here). Fifth and last of
the planned messenger integrations (docs/MESSENGER_INTEGRATIONS_PLAN.md,
2026-10-06).

Signal has no bot/app API at all -- signal-cli (a widely-used, community-
maintained tool, not Caroline-specific) is the standard way any third-
party integration talks to a personal Signal account. Two phases:

1. LINKING (one-time): `signal-cli link -n Caroline` prints a
   "tsdevice:/..." URI on stdout; rendered to a QR code (same PNG-and-
   open approach as whatsapp_channel.py) for the user to scan in
   Signal's own app (Settings > Linked Devices > Link New Device). The
   process exits once scanned, having registered a local account under
   --config.
2. DAEMON (every run after linking): `signal-cli -a <number> daemon
   --tcp 127.0.0.1:<port>` serves newline-delimited JSON-RPC 2.0 over a
   plain TCP socket -- connected to directly via asyncio.open_connection,
   no subprocess stdio piping needed for the IPC itself (the daemon
   process is simply kept running in the background).

Exact signal-cli CLI flags and JSON-RPC method/notification shapes here
follow its documented conventions as of this writing but have NOT been
exercised against a live install in the environment this was written in
-- verify against the actually-bundled signal-cli version before relying
on this in production, same caveat as whatsapp-sidecar/index.js's own
doc comment.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.logging_setup import log_event
from app.plugins.files_plugin import open_file_with_default_app
from app.secret_store import messenger_secrets_dir
from app.task_supervisor import supervise

JAVA_HOME = os.environ.get("CAROLINE_JAVA_HOME", "")
SIGNAL_CLI_BAT = os.environ.get("CAROLINE_SIGNAL_CLI_BAT", "")
DAEMON_PORT = 7592
DAEMON_READY_TIMEOUT_S = 20.0
RPC_TIMEOUT_S = 30.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SignalError(Exception):
    pass


def _config_dir(workspace_dir: str) -> Path:
    d = messenger_secrets_dir(workspace_dir, "signal") / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _number_file(workspace_dir: str) -> Path:
    return messenger_secrets_dir(workspace_dir, "signal") / "linked_number.txt"


def _qr_image_path(workspace_dir: str) -> Path:
    return messenger_secrets_dir(workspace_dir, "signal") / "link-qr.png"


def _linked_number(workspace_dir: str) -> str | None:
    try:
        return _number_file(workspace_dir).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def _env_for_subprocess() -> dict[str, str]:
    env = dict(os.environ)
    if JAVA_HOME:
        env["JAVA_HOME"] = JAVA_HOME
    return env


def _require_tooling() -> None:
    if not SIGNAL_CLI_BAT or not Path(SIGNAL_CLI_BAT).exists() or not JAVA_HOME or not Path(JAVA_HOME).exists():
        raise SignalError("Signal isn't set up in this build yet -- signal-cli/Java weren't provisioned. Reinstall Caroline.")


async def start_login(workspace_dir: str) -> str:
    """Runs `signal-cli link` as a one-shot subprocess, reads the pairing
    URI from its stdout, and renders+opens it as a QR image -- same
    reasoning as whatsapp_channel.py's own start_login for not blocking
    synchronously on the user actually scanning it: this kicks off the
    link attempt and returns immediately; the background loop notices
    the resulting linked number and starts the daemon on its own next
    pass."""
    _require_tooling()
    config_dir = _config_dir(workspace_dir)
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
                _render_and_open_qr(workspace_dir, line)
        code = await proc.wait()
        if code == 0:
            await _record_linked_number(workspace_dir, config_dir)
            log_event("plugin:signal", "link_succeeded")
        elif not uri_found:
            stderr = (await proc.stderr.read()).decode("utf-8", errors="replace") if proc.stderr else ""
            log_event("plugin:signal", "link_failed_no_qr", stderr=stderr[:500])
        else:
            log_event("plugin:signal", "link_process_exited_nonzero", code=code)

    asyncio.create_task(_watch())
    return (
        "Starting Signal linking -- a QR code image will open automatically in a moment. Scan it in the "
        "Signal app on your phone (Settings > Linked Devices > Link New Device)."
    )


def _render_and_open_qr(workspace_dir: str, data: str) -> None:
    try:
        import qrcode

        img = qrcode.make(data)
        path = _qr_image_path(workspace_dir)
        img.save(path)
        log_event("plugin:signal", "qr_ready", path=str(path))
        open_file_with_default_app(str(path))
    except Exception as exc:
        log_event("plugin:signal", "qr_render_failed", error=str(exc))


async def _record_linked_number(workspace_dir: str, config_dir: Path) -> None:
    """`signal-cli listAccounts` reports every locally-registered account
    under --config; we only ever link one, so the first (only) result is
    the number the daemon should bind to."""
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
                _number_file(workspace_dir).write_text(token, encoding="utf-8")
                return
    log_event("plugin:signal", "could_not_parse_linked_number", output=text[:500])


@dataclass
class SignalChannelStatus:
    linked: bool = False
    connected: bool = False
    own_number: str | None = None
    last_event_at_iso: str | None = None
    last_error: str | None = None


_status = SignalChannelStatus()
_writer: asyncio.StreamWriter | None = None
_pending: dict[int, asyncio.Future[Any]] = {}
_next_id = 0


def get_signal_channel_status() -> dict[str, Any]:
    return asdict(_status)


def is_connected() -> bool:
    return _writer is not None and _status.connected


async def _rpc_call(method: str, params: dict[str, Any]) -> Any:
    global _next_id
    if not is_connected():
        raise SignalError("Signal isn't connected yet -- use signal_login first.")
    _next_id += 1
    req_id = _next_id
    fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
    _pending[req_id] = fut
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}) + "\n"
    assert _writer is not None
    _writer.write(body.encode("utf-8"))
    await _writer.drain()
    try:
        result = await asyncio.wait_for(fut, timeout=RPC_TIMEOUT_S)
    except asyncio.TimeoutError:
        _pending.pop(req_id, None)
        raise SignalError(f"No response from signal-cli within {int(RPC_TIMEOUT_S)}s.")
    return result


async def send_message(recipient: str, text: str) -> None:
    await _rpc_call("send", {"recipient": [recipient], "message": text})


async def list_contacts() -> list[dict[str, Any]]:
    result = await _rpc_call("listContacts", {})
    return result if isinstance(result, list) else []


async def _spawn_daemon(workspace_dir: str) -> subprocess.Popen[bytes]:
    number = _linked_number(workspace_dir)
    assert number
    config_dir = _config_dir(workspace_dir)
    return subprocess.Popen(
        [SIGNAL_CLI_BAT, "--config", str(config_dir), "-a", number, "daemon", "--tcp", f"127.0.0.1:{DAEMON_PORT}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=_env_for_subprocess(), creationflags=_NO_WINDOW,
    )


async def _run_daemon_connection(workspace_dir: str, inject_proactive: Callable[[str], None]) -> None:
    global _writer
    _require_tooling()
    number = _linked_number(workspace_dir)
    if not number:
        return

    daemon_proc = await _spawn_daemon(workspace_dir)
    try:
        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        deadline = asyncio.get_event_loop().time() + DAEMON_READY_TIMEOUT_S
        while asyncio.get_event_loop().time() < deadline:
            if daemon_proc.poll() is not None:
                raise SignalError(f"signal-cli daemon exited early (code {daemon_proc.returncode}) before accepting connections.")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", DAEMON_PORT)
                break
            except OSError:
                await asyncio.sleep(0.5)
        if reader is None or writer is None:
            raise SignalError(f"signal-cli daemon didn't start listening on 127.0.0.1:{DAEMON_PORT} within {int(DAEMON_READY_TIMEOUT_S)}s.")

        _writer = writer
        _status.connected = True
        _status.linked = True
        _status.own_number = number
        log_event("plugin:signal", "daemon_connected", number=number)

        while True:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            _dispatch_rpc_line(msg, inject_proactive)
    finally:
        _writer = None
        _status.connected = False
        for fut in _pending.values():
            if not fut.done():
                fut.set_exception(SignalError("Signal daemon disconnected"))
        _pending.clear()
        if daemon_proc.poll() is None:
            daemon_proc.terminate()


def _dispatch_rpc_line(msg: dict[str, Any], inject_proactive: Callable[[str], None]) -> None:
    if "id" in msg and msg.get("id") in _pending:
        fut = _pending.pop(msg["id"])
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
        if not text:
            return
        source = envelope.get("sourceNumber") or envelope.get("source") or "unknown"
        name = envelope.get("sourceName") or source
        _status.last_event_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        log_event("plugin:signal", "message_received", source=source)
        inject_proactive(
            f'[New Signal message from "{name}" ({source}) -- reply with '
            f"signal_send_message(to=\"{source}\", text=...) if it needs a response:\n{text}]"
        )


def start_signal_channel(workspace_dir: str, inject_proactive: Callable[[str], None]) -> asyncio.Task[None]:
    async def _loop() -> None:
        while True:
            if not _linked_number(workspace_dir):
                await asyncio.sleep(30.0)
                continue
            try:
                await _run_daemon_connection(workspace_dir, inject_proactive)
            except SignalError as exc:
                _status.last_error = str(exc)
                log_event("plugin:signal", "daemon_unavailable", error=str(exc))
                await asyncio.sleep(60.0)
                continue
            except Exception as exc:
                _status.last_error = str(exc)
                log_event("plugin:signal", "daemon_failed", error=str(exc))
            await asyncio.sleep(5.0)

    return supervise("signal_channel", _loop)
