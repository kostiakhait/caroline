"""Standalone process supervisor for Caroline's backend (run_server.py).

Per explicit instruction (2026-09-27): decouples backend-process lifecycle
management from the WPF frontend -- previously spread across
Native/BackendProcess.cs (spawn/kill), Native/BackendHealthWatchdog.cs
(external health polling, both whole-process and per-tab), and
MainWindow.xaml.cs's RestartBackend/RecoverTab (rate-limited restart
policy) -- into a plain Python process exposing an HTTP control API. Motive
(stated directly): "поможет в будущем отделить визуальный супервизор от
бэкенда" -- a future non-WPF (e.g. headless Linux) launcher can drive this
exact same HTTP surface instead of a from-scratch reimplementation of
process supervision in C#/.NET, which is Windows-only by construction.

This file is deliberately a standalone entry point, NOT part of the `app`
package's own import graph (only app.logging_setup, a genuinely dependency-
free module) -- a broken/uninstallable heavy dependency in the real
backend (app.main's own huge import list) must not also break the
supervisor's own ability to report that and try to restart it. Spawned
once by the WPF app (Native/SupervisorClient.cs, mirroring how
BackendProcess.cs used to spawn run_server.py directly) and lives for the
app's whole session; spawns and owns run_server.py itself, autonomously
(its own health-poll loop runs regardless of whether any HTTP client is
even connected -- the API is for external observability/manual control,
never the only reason recovery happens).

Ported 1:1 where a direct equivalent existed on the C# side (rate-limit
constants, grace windows, bad-check thresholds); see each constant's own
comment for exactly which C# original it mirrors.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.logging_setup import log_event  # noqa: E402

# --- Layout -------------------------------------------------------------
# CarolineInstaller.AppPaths' layout: Root/app-<hash>/backend-py/
# supervisor.py (this file) and run_server.py (sibling); Root/art/...;
# Root/runtime/... -- see BackendProcess.cs's own comments for the
# original reasoning behind each of the sibling paths below, now ported
# here since this file, not that one, spawns run_server.py. Computed from
# this file's own location (self-sufficient) rather than requiring a
# launcher to pass it in -- CAROLINE_APP_ROOT is still honored as an
# override for dev/test runs where this file isn't at its normal installed
# depth.
_BACKEND_PY_DIR = Path(__file__).resolve().parent
_APP_DIR = _BACKEND_PY_DIR.parent  # Root/app-<hash>/
_ROOT_DIR = Path(os.environ["CAROLINE_APP_ROOT"]) if os.environ.get("CAROLINE_APP_ROOT") else _APP_DIR.parent

BACKEND_PORT = int(os.environ.get("CAROLINE_PORT", "48765"))
# New port, deliberately not 48765 (the backend's own app/chat API) or 8767
# (AppBrowserHost) -- see this project's own port registry discussion.
SUPERVISOR_PORT = int(os.environ.get("CAROLINE_SUPERVISOR_PORT", "48766"))

# Mirrors MainWindow.xaml.cs's MaxBackendRestartsPerWindow/BackendRestartWindow
# exactly (RestartBackend's own doc comment: "a handful of quick auto-restarts
# are fine (transient), but a backend that keeps crashing needs a human").
MAX_RESTARTS_PER_WINDOW = 5
RESTART_WINDOW_S = 10 * 60

# Mirrors BackendHealthWatchdog.cs's CheckInterval/BadChecksBeforeAction/
# StartupGraceWindow/StuckTurnMs exactly -- see that file's own doc comment
# for the two separate real incidents each constant's value came from.
CHECK_INTERVAL_S = 30
BAD_CHECKS_BEFORE_ACTION = 2
STARTUP_GRACE_S = 10 * 60
STUCK_TURN_MS = 5 * 60_000


def _resolve_pythonw_exe() -> str:
    """Mirrors BackendProcess.cs's ResolvePythonwExe: prefer the
    installer's own isolated embeddable Python (a sibling of the app's own
    install dir), fall back to "pythonw" on PATH for dev/debug runs where
    the installer was never involved.

    Linux port (2026-10-05): there is no windowless/console distinction on
    Linux the way "pythonw" vs "python" exists on Windows (no console ever
    gets auto-allocated for a spawned process the way Windows does for a
    console-subsystem child) -- this collapses to a single bundled
    `python3`, falling back to "python3" on PATH for the same dev/debug
    reason as the Windows branch."""
    if sys.platform != "win32":
        isolated = _ROOT_DIR / "runtime" / "python" / "bin" / "python3"
        return str(isolated) if isolated.exists() else "python3"
    isolated = _ROOT_DIR / "runtime" / "python" / "pythonw.exe"
    return str(isolated) if isolated.exists() else "pythonw"


def _child_env() -> dict[str, str]:
    """Same env vars BackendProcess.cs used to set on the child process --
    see its own comments for why each is a SIBLING of the app dir, never
    inside it (survives the app dir being fully replaced on every update).

    Linux port (2026-10-05): no ".exe" suffix on any of these on non-Windows,
    and no CAROLINE_NATIVEHOST_EXE_PATH at all there -- Phase 3 of the Linux
    port (see docs/LINUX_PORT_PLAN.md) runs the embedded-browser host
    in-process inside backend-py itself via Playwright, there is no separate
    exe to launch, so app_browser_plugin.py's own Linux branch never reads
    this var in the first place."""
    env = dict(os.environ)
    env["CAROLINE_MODELS_DIR"] = str(_ROOT_DIR / "art" / "models")
    env["CAROLINE_WHISPER_MODEL_PATH"] = str(_ROOT_DIR / "art" / "whisper-model")
    env["CAROLINE_PORT"] = str(BACKEND_PORT)
    if sys.platform == "win32":
        env["CAROLINE_FFMPEG_PATH"] = str(_ROOT_DIR / "runtime" / "ffmpeg" / "ffmpeg.exe")
        env["CAROLINE_CODEX_PATH"] = str(_ROOT_DIR / "runtime" / "codex" / "bin" / "codex-app-server.exe")
        env["CAROLINE_PYTHON_PATH"] = str(_ROOT_DIR / "runtime" / "python" / "python.exe")
        # Caroline.NativeHost.exe (the embedded-browser host, 2026-10-03 extraction out of
        # Caroline.exe -- see app_browser_plugin.py's own doc comment) publishes as a SIBLING of
        # Caroline.exe and this backend-py dir inside the same app-<hash> folder (_APP_DIR, not
        # _ROOT_DIR -- see the Makefile's own publish step), same layout Caroline.exe itself lives
        # at relative to AppContext.BaseDirectory.
        env["CAROLINE_NATIVEHOST_EXE_PATH"] = str(_APP_DIR / "Caroline.NativeHost.exe")
    else:
        env["CAROLINE_FFMPEG_PATH"] = str(_ROOT_DIR / "runtime" / "ffmpeg" / "ffmpeg")
        env["CAROLINE_CODEX_PATH"] = str(_ROOT_DIR / "runtime" / "codex" / "bin" / "codex-app-server")
        env["CAROLINE_PYTHON_PATH"] = str(_ROOT_DIR / "runtime" / "python" / "bin" / "python3")
    return env


def _kill_pid(pid: int) -> None:
    """A forceful, whole-tree kill of a single OS pid -- used for both the
    whole backend process and one tab's own stuck cliProcessPid. Windows
    path mirrors BackendProcess.cs's own taskkill fallback (and
    chat_session.py's _force_kill_underlying_cli_process, which already
    does the identical thing for the SAME reason: a graceful stop that
    doesn't reliably land).

    Linux port (2026-10-05), bug fix: the POSIX branch used to call
    os.kill(pid, SIGKILL) on just that one pid -- NOT a tree kill despite
    this function's own docstring/callers assuming one (confirmed by this
    file's own Linux-port audit). A plain SIGKILL leaves every grandchild
    (e.g. a stuck claude.exe/codex-app-server spawned BY run_server.py)
    orphaned and running. os.killpg targets the whole process GROUP instead
    -- this only works because Supervisor.start() now passes
    start_new_session=True on POSIX, which makes run_server.py the leader of
    its own fresh group that every descendant inherits, exactly mirroring
    what taskkill's own /T flag already does via the Windows job/process-tree
    walk."""
    if sys.platform == "win32":
        subprocess.Popen(
            ["taskkill.exe", "/F", "/T", "/PID", str(pid)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


class Supervisor:
    """Owns run_server.py's whole lifecycle: spawn, autonomous health
    polling (whole-process AND per-tab), and rate-limited restart -- ported
    from BackendProcess.cs + BackendHealthWatchdog.cs +
    MainWindow.RestartBackend/RecoverTab (all retired on the C# side, see
    those files' own 2026-09-27 removal notes)."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._intentional_stop = False
        self._restart_timestamps: list[float] = []
        self._last_restart_reason: str | None = None
        self._last_restart_at: float | None = None
        self._grace_until = 0.0
        self._consecutive_bad_checks = 0
        # Per-tab bookkeeping, keyed by tabId -- populated/pruned purely
        # from what /api/status's own tabs[] reports each poll, so (unlike
        # BackendHealthWatchdog's one-object-per-open-tab design) nothing
        # needs to be pushed here when a tab opens/closes on the WPF side.
        self._tab_grace_until: dict[str, float] = {}
        self._tab_last_seen: dict[str, float] = {}
        self._tab_last_recovered_at: dict[str, float] = {}
        self._http = httpx.AsyncClient(timeout=10.0)

    # --- process lifecycle -----------------------------------------------
    async def start(self) -> bool:
        entry = _BACKEND_PY_DIR / "run_server.py"
        if not entry.exists():
            log_event("supervisor", "backend_entry_missing", path=str(entry))
            return False
        pythonw = _resolve_pythonw_exe()
        log_event("supervisor", "backend_start", pythonw=pythonw, entry=str(entry))
        try:
            self._process = await asyncio.create_subprocess_exec(
                pythonw, str(entry),
                cwd=str(_BACKEND_PY_DIR),
                env=_child_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                # Linux-port bug fix (2026-10-05): makes run_server.py the leader of its
                # own new process group so _kill_pid's os.killpg can reach its whole
                # descendant tree -- see _kill_pid's own doc comment. False (the default,
                # a no-op) on Windows; start_new_session is POSIX-only.
                start_new_session=(sys.platform != "win32"),
                stdout=asyncio.subprocess.PIPE,
                # Merged into stdout, same as BackendProcess.cs piping both
                # OutputDataReceived and ErrorDataReceived into the same
                # OutputLine event -- one interleaved-by-time stream, not two.
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:
            log_event("supervisor", "backend_start_failed", error=str(exc))
            return False
        self._intentional_stop = False
        self._grace_until = time.monotonic() + STARTUP_GRACE_S
        self._consecutive_bad_checks = 0
        log_event("supervisor", "backend_started", pid=self._process.pid)
        self._pump_task = asyncio.create_task(self._pump_output(self._process))
        return True

    async def _pump_output(self, proc: asyncio.subprocess.Process) -> None:
        """Re-emits the backend's own stdout onto THIS process's stdout,
        line by line -- logging_setup.py's own docstring is explicit that
        every log_event() call is meant to land in one combined,
        correlatable timeline (originally: piped through BackendProcess.cs
        into Logger.Log/caroline.log). Now that this file spawns the
        backend instead, preserving that property just means forwarding
        here -- Native/SupervisorClient.cs pipes THIS process's stdout into
        Logger.Log exactly as BackendProcess.cs used to pipe the backend's
        own stdout directly, so the combined timeline survives unchanged
        from the WPF app's point of view."""
        assert proc.stdout is not None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                sys.stdout.buffer.write(line)
                sys.stdout.flush()
        except Exception as exc:
            log_event("supervisor", "output_pump_failed", error=str(exc))

    def stop(self) -> None:
        self._intentional_stop = True
        self._kill_backend()

    def _kill_backend(self) -> None:
        proc, self._process = self._process, None
        if proc is None:
            return
        if proc.returncode is None:
            _kill_pid(proc.pid)

    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def restart(self, reason: str) -> bool:
        now = time.monotonic()
        self._restart_timestamps = [t for t in self._restart_timestamps if now - t <= RESTART_WINDOW_S]
        self._restart_timestamps.append(now)
        if len(self._restart_timestamps) > MAX_RESTARTS_PER_WINDOW:
            log_event("supervisor", "backend_restart_gave_up", reason=reason, count=len(self._restart_timestamps))
            return False
        log_event(
            "supervisor", "backend_restarting", reason=reason,
            attempt=len(self._restart_timestamps), max=MAX_RESTARTS_PER_WINDOW,
        )
        self._kill_backend()
        started = await self.start()
        if started:
            self._last_restart_reason = reason
            self._last_restart_at = time.time()
            # Same reasoning as BackendHealthWatchdog.NotifyBackendRestarted:
            # a whole-process restart means every open tab's own query() is
            # about to cold-start too -- don't judge any of them as "stuck"
            # again until they've had a fair chance to come back up.
            self._tab_grace_until = {tid: now + STARTUP_GRACE_S for tid in self._tab_grace_until}
        return started

    # --- health polling ----------------------------------------------------
    async def check_once(self) -> None:
        try:
            resp = await self._http.get(f"http://127.0.0.1:{BACKEND_PORT}/api/status")
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            await self._on_whole_process_bad_check(f"/api/status did not respond: {exc}")
            return
        self._consecutive_bad_checks = 0
        self._check_tabs(data.get("tabs") or [])

    async def _on_whole_process_bad_check(self, reason: str) -> None:
        self._consecutive_bad_checks += 1
        in_grace = time.monotonic() < self._grace_until
        log_event(
            "supervisor", "bad_check", reason=reason,
            count=self._consecutive_bad_checks, in_grace=in_grace,
        )
        if in_grace or self._consecutive_bad_checks < BAD_CHECKS_BEFORE_ACTION:
            return
        self._consecutive_bad_checks = 0
        await self.restart(reason)

    def _check_tabs(self, tabs: list[dict[str, Any]]) -> None:
        now = time.monotonic()
        seen_ids: set[str] = set()
        for tab in tabs:
            tab_id = tab.get("tabId")
            if not tab_id:
                continue
            seen_ids.add(tab_id)
            if tab_id not in self._tab_grace_until:
                self._tab_grace_until[tab_id] = now + STARTUP_GRACE_S
            self._tab_last_seen[tab_id] = now
            if now < self._tab_grace_until[tab_id]:
                continue
            turn_pending = bool(tab.get("turnPending"))
            last_activity_ms = tab.get("lastActivityMs") or 0
            if not (turn_pending and last_activity_ms > STUCK_TURN_MS):
                continue
            pid = tab.get("cliProcessPid")
            if not pid:
                log_event("supervisor", "tab_frozen_no_pid", tab_id=tab_id, last_activity_ms=last_activity_ms)
                continue
            log_event("supervisor", "tab_recovering", tab_id=tab_id, pid=pid, last_activity_ms=last_activity_ms)
            _kill_pid(pid)
            self._tab_last_recovered_at[tab_id] = time.time()
            # Don't re-kill the same tab again on the very next tick, before
            # its fresh query() has had a chance to reconnect.
            self._tab_grace_until[tab_id] = now + STARTUP_GRACE_S
        # A tab no longer present in /api/status (closed on the WPF side)
        # has nothing left to track -- matches BackendHealthWatchdog's own
        # per-tab instances being Dispose()'d in CloseTabAsync, just
        # expressed as dict cleanup since nothing is pushed here anymore.
        for stale in set(self._tab_grace_until) - seen_ids:
            self._tab_grace_until.pop(stale, None)
            self._tab_last_seen.pop(stale, None)
            self._tab_last_recovered_at.pop(stale, None)

    async def poll_loop(self) -> None:
        while True:
            await asyncio.sleep(CHECK_INTERVAL_S)
            if self._intentional_stop:
                continue
            await self.check_once()

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        recent_restarts = [t for t in self._restart_timestamps if now - t <= RESTART_WINDOW_S]
        return {
            "backendRunning": self.is_running(),
            "backendPid": self._process.pid if self._process else None,
            "gaveUp": len(recent_restarts) > MAX_RESTARTS_PER_WINDOW,
            "restartCount": len(recent_restarts),
            "maxRestartsPerWindow": MAX_RESTARTS_PER_WINDOW,
            "restartWindowSeconds": RESTART_WINDOW_S,
            "lastRestartReason": self._last_restart_reason,
            "lastRestartAtUtc": self._last_restart_at,
            "tabs": {tid: {"lastRecoveredAtUtc": self._tab_last_recovered_at.get(tid)} for tid in self._tab_last_seen},
        }


supervisor = Supervisor()
app = FastAPI()


@app.on_event("startup")
async def _on_startup() -> None:
    log_event("supervisor", "listening", port=SUPERVISOR_PORT, backend_port=BACKEND_PORT, root=str(_ROOT_DIR))
    await supervisor.start()
    asyncio.create_task(supervisor.poll_loop())


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    supervisor.stop()


@app.get("/status")
async def get_status() -> JSONResponse:
    return JSONResponse(supervisor.status())


@app.post("/start")
async def post_start() -> JSONResponse:
    if supervisor.is_running():
        return JSONResponse({"ok": True, "alreadyRunning": True})
    started = await supervisor.start()
    return JSONResponse({"ok": started})


@app.post("/stop")
async def post_stop() -> JSONResponse:
    supervisor.stop()
    return JSONResponse({"ok": True})


@app.post("/restart")
async def post_restart() -> JSONResponse:
    ok = await supervisor.restart("manual (HTTP /restart)")
    return JSONResponse({"ok": ok, "gaveUp": supervisor.status()["gaveUp"]})


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=SUPERVISOR_PORT)
