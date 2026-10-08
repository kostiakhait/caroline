"""Generic newline-delimited-JSON subprocess runner, for a non-Python helper
process a messenger integration needs to stay alive and talk to over stdio
-- per docs/MESSENGER_INTEGRATIONS_PLAN.md's own "sidecar-runner" piece.
Modeled directly on app/engines/codex_rpc.py's own proven pattern (blocking
pipe I/O on plain threads bridged into asyncio via call_soon_threadsafe --
works regardless of event loop policy, unlike asyncio.create_subprocess_exec
under a Windows selector loop).

Deliberately simpler than codex_rpc.py: no JSON-RPC id/response matching,
just "one JSON object in, one JSON object out" per line -- a sidecar that
needs a reply correlates it itself (e.g. an explicit "id" field it chose),
this layer only ships bytes.

First user: app/whatsapp_channel.py's Baileys (Node) sidecar. signal-cli
(app/signal_channel.py) does NOT use this -- its JSON-RPC runs over a local
TCP socket, not stdio, so plain asyncio works there without the Windows
subprocess-under-selector-loop caveat this module exists to route around.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from typing import Any, Callable

from app.logging_setup import log_event

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SidecarProcess:
    def __init__(
        self,
        argv: list[str],
        env: dict[str, str],
        on_line: Callable[[dict[str, Any]], None],
        on_closed: Callable[[], None],
        label: str = "sidecar",
        cwd: str | None = None,
    ) -> None:
        self._argv = argv
        self._env = env
        self._cwd = cwd
        self._on_line = on_line
        self._on_closed = on_closed
        self._label = label
        self._proc: subprocess.Popen[bytes] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._write_lock = threading.Lock()
        self._closed = False
        self.stderr_tail: list[str] = []

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._proc = subprocess.Popen(
            self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._env, cwd=self._cwd, creationflags=_NO_WINDOW,
        )
        threading.Thread(target=self._read_stdout, name=f"{self._label}-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"{self._label}-stderr", daemon=True).start()
        log_event("engine", "sidecar_started", label=self._label, pid=self._proc.pid)

    def send(self, obj: dict[str, Any]) -> None:
        """Writes one JSON line to the sidecar's stdin. Safe to call from
        any thread -- write_lock serializes concurrent callers."""
        if self._proc is None or self._proc.stdin is None or self._closed:
            raise RuntimeError(f"sidecar '{self._label}' is not running")
        data = (json.dumps(obj) + "\n").encode("utf-8")
        with self._write_lock:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout and self._loop
        try:
            for raw in self._proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self._loop.call_soon_threadsafe(self._dispatch_line, line)
        except Exception as exc:
            log_event("engine", "sidecar_stdout_reader_failed", label=self._label, error=repr(exc))
        finally:
            self._loop.call_soon_threadsafe(self._handle_closed)

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        try:
            for raw in self._proc.stderr:
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    self.stderr_tail = (self.stderr_tail + [text])[-40:]
                    log_event("engine", "sidecar_stderr", label=self._label, line=text[:500])
        except Exception:
            pass

    def _handle_closed(self) -> None:
        if self._closed:
            return
        self._closed = True
        log_event("engine", "sidecar_closed", label=self._label, stderr_tail=self.stderr_tail[-5:])
        self._on_closed()

    def _dispatch_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except Exception:
            log_event("engine", "sidecar_unparseable_line", label=self._label, line=line[:300])
            return
        if isinstance(msg, dict):
            self._on_line(msg)
