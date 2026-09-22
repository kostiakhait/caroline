"""Minimal JSON-RPC client for `codex app-server` over stdio (JSONL).

The wire is JSON-RPC 2.0 with the "jsonrpc" member omitted. Three kinds of
inbound lines: responses (`id` + result/error) resolve the matching pending
request; notifications (`method`, no `id`) go to on_notification; server ->
client requests (`method` + `id`) go to on_server_request, whose return value
is sent back as the response. Blocking pipe I/O runs on plain threads, so this
works on any asyncio loop (a Windows selector loop cannot spawn subprocesses).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from typing import Any, Awaitable, Callable

from app.logging_setup import log_event

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class CodexRpcError(Exception):
    def __init__(self, code: int | None, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class CodexRpcClient:
    def __init__(
        self,
        argv: list[str],
        env: dict[str, str],
        on_notification: Callable[[str, dict[str, Any]], None],
        on_server_request: Callable[[str, dict[str, Any]], Awaitable[Any]],
        on_closed: Callable[[], None],
        label: str = "codex",
    ) -> None:
        self._argv = argv
        self._env = env
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._on_closed = on_closed
        self._label = label
        self._proc: subprocess.Popen[bytes] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
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
            env=self._env, creationflags=_NO_WINDOW,
        )
        threading.Thread(target=self._read_stdout, name=f"{self._label}-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"{self._label}-stderr", daemon=True).start()

    # ------------------------------------------------------------ inbound --

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout and self._loop
        try:
            for raw in self._proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self._loop.call_soon_threadsafe(self._dispatch_line, line)
        except Exception as exc:
            log_event("engine", "codex_stdout_reader_failed", label=self._label, error=repr(exc))
        finally:
            self._loop.call_soon_threadsafe(self._handle_closed)

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        try:
            for raw in self._proc.stderr:
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    self.stderr_tail = (self.stderr_tail + [text])[-40:]
        except Exception:
            pass

    def _handle_closed(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CodexRpcError(None, "codex process ended: " + " | ".join(self.stderr_tail[-3:])))
        self._pending.clear()
        self._on_closed()

    def _dispatch_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except Exception:
            log_event("engine", "codex_unparseable_line", label=self._label, line=line[:200])
            return
        if not isinstance(msg, dict):
            return
        method = msg.get("method")
        if method is None:
            fut = self._pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                if "error" in msg:
                    err = msg["error"] or {}
                    fut.set_exception(CodexRpcError(err.get("code"), err.get("message") or "codex error", err.get("data")))
                else:
                    fut.set_result(msg.get("result"))
            return
        params = msg.get("params") or {}
        if "id" in msg:
            asyncio.ensure_future(self._answer_server_request(msg["id"], method, params))
        else:
            try:
                self._on_notification(method, params)
            except Exception as exc:
                log_event("engine", "codex_notification_handler_failed", label=self._label, method=method, error=repr(exc))

    async def _answer_server_request(self, req_id: Any, method: str, params: dict[str, Any]) -> None:
        try:
            result = await self._on_server_request(method, params)
            await self._write({"id": req_id, "result": result})
        except Exception as exc:
            log_event("engine", "codex_server_request_failed", label=self._label, method=method, error=repr(exc))
            await self._write({"id": req_id, "error": {"code": -32603, "message": str(exc)}})

    # ----------------------------------------------------------- outbound --

    async def _write(self, obj: dict[str, Any]) -> None:
        if self._closed or not self._proc or not self._proc.stdin:
            return
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

        def _do() -> None:
            with self._write_lock:
                try:
                    self._proc.stdin.write(data)  # type: ignore[union-attr]
                    self._proc.stdin.flush()  # type: ignore[union-attr]
                except (BrokenPipeError, OSError, ValueError):
                    pass

        await asyncio.to_thread(_do)

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = 60.0) -> Any:
        assert self._loop
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future[Any] = self._loop.create_future()
        self._pending[req_id] = fut
        await self._write({"method": method, "id": req_id, "params": params or {}})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write({"method": method, "params": params or {}})

    # ------------------------------------------------------------ closing --

    async def kill_and_wait(self, timeout: float = 10.0) -> None:
        """kill(), then wait for the process to actually be gone -- a restart
        that starts the next process on the same CODEX_HOME while the old one
        still holds its database locks fails to start."""
        self.kill()
        proc = self._proc
        if proc is None:
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass

    def kill(self) -> None:
        """Kills the process and everything it spawned (shell commands the
        agent started), not just the exe itself."""
        proc = self._proc
        if not proc or proc.poll() is not None:
            return
        try:
            subprocess.Popen(
                ["taskkill.exe", "/F", "/T", "/PID", str(proc.pid)],
                creationflags=_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# Config every Caroline-launched Codex runs with. Codex is embedded here as an
# agent runtime, not used as a standalone product: without these it clones a
# third-party plugin repository from GitHub into CODEX_HOME at startup (and runs
# git for it), checks for its own updates (the installer pins the version), and
# sends product analytics.
BASE_CONFIG_OVERRIDES = [
    "features.plugins=false",
    "check_for_update_on_startup=false",
    "analytics.enabled=false",
]


def codex_argv(exe: str, overrides: list[str] | None = None) -> list[str]:
    argv = [exe]
    for override in [*BASE_CONFIG_OVERRIDES, *(overrides or [])]:
        argv += ["-c", override]
    return argv


def build_env(codex_home: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_HOME"] = codex_home
    if extra:
        env.update(extra)
    return env
