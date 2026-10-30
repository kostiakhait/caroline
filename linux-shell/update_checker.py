"""The Linux end of the update (docs/UPDATE_EVERYWHERE_PLAN.md, "The rule"), the counterpart of Windows/Caroline/Services/UpdateChecker.cs.

The looking for a release, the download and the checking of the signature and of the sha256 are the backend's (backend-py/app/update_service.py, one
code for every platform). This module only asks it what to do (GET /api/update/status) and does the one thing only the shell can: put the verified
file in place of the running AppImage and start it.

  action "install"  Caroline is inactive (or the owner said yes): the AppImage is replaced and restarted at once, no dialog.
  action "ask"      Caroline is in use: no dialog, nothing that breaks the work. The tray item "Update to {version}" and a line in the status bar of every
                    tab; the owner's click (update_now) is the yes. When she becomes inactive the update installs by itself, answered or not.

This replaces the earlier Yes/No dialog "Restart now to update?" (it interrupted the work, which the rule forbids) and the earlier download code.
Only runs from a real AppImage ($APPIMAGE, set by the AppImage runtime): a dev tree or a plain dist-linux has no single file to replace, `enabled` is
False and start() does nothing. The class UpdateClient below has no GTK in it and is what the tests drive; UpdateChecker is its GTK adapter."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import Any, Callable

try:  # GTK is only needed by the adapter at the end; the client above it is tested without it
    from gi.repository import GLib
except Exception:  # noqa: BLE001
    GLib = None  # type: ignore[assignment]

POLL_INTERVAL_MS = 30_000
HTTP_TIMEOUT_S = 15.0


class UpdateClient:
    def __init__(
        self, base: str, appimage_path: Path, log: Callable[[str], None],
        on_update_available: Callable[[str], None], on_status_changed: Callable[[str], None], on_restart: Callable[[], None],
        spawn: Callable[..., Any] = subprocess.Popen, call_soon: Callable[..., Any] = lambda fn, *a: fn(*a),
    ) -> None:
        self._base = base.rstrip("/")
        self._appimage = appimage_path
        self._log = log
        self._on_update_available = on_update_available
        self._on_status_changed = on_status_changed
        self._on_restart = on_restart
        self._spawn = spawn
        self._call_soon = call_soon
        self._applying = False
        self._last_line: str | None = None
        self._offered: str | None = None

    def _request(self, path: str, post: bool = False) -> dict[str, Any] | None:
        req = urllib.request.Request(f"{self._base}{path}", data=b"{}" if post else None, method="POST" if post else "GET", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 -- the backend is not up yet or is restarting: the next poll asks again
            return None

    def _say(self, line: str) -> None:
        if line != self._last_line:
            self._last_line = line
            self._call_soon(self._on_status_changed, line)

    def poll(self) -> None:
        """Blocking: run it off the main thread."""
        if self._applying:
            return
        s = self._request("/api/update/status")
        if not s:
            return
        if s.get("status") == "downloading":
            self._say(f"Caroline: Update ongoing... {s.get('progress') or 0}%")
            return
        if s.get("status") != "ready":
            return
        version = s.get("version") or ""
        path = s.get("path")
        if s.get("action") == "install" and path and Path(path).is_file():
            self._apply(Path(path), version, visible=bool(s.get("confirmed")))
        elif s.get("action") == "ask":
            self._call_soon(self._on_update_available, version)
            if self._offered != version:
                self._offered = version
                self._log(f"[UpdateChecker] update {version} is ready, Caroline is in use -- waiting for the owner or for her to become inactive")
            self._say(f'Caroline: Update {version} ready -- installs when you are done, or choose "Update to {version}" in the tray')

    def confirm(self) -> None:
        """The owner chose "Update to ..." in the tray: that is the yes. Blocking."""
        self._log("[UpdateChecker] the owner confirmed the update")
        self._request("/api/update/confirm", post=True)
        self.poll()

    def _apply(self, verified: Path, version: str, visible: bool) -> None:
        self._applying = True
        self._log(f"[UpdateChecker] installing {version} over {self._appimage} ({'the owner confirmed' if visible else 'Caroline was inactive'})")
        staged = self._appimage.with_suffix(self._appimage.suffix + ".update-tmp")
        try:
            shutil.copyfile(verified, staged)  # the download folder may be on another file system, so copy beside the target, then rename
            if staged.stat().st_size != verified.stat().st_size:
                raise OSError("the copy is not the size of the verified file")
            os.chmod(staged, 0o755)
            os.replace(staged, self._appimage)  # Linux lets a running file be replaced: the old inode lives on for this process
            self._request("/api/update/installing", post=True)
            self._spawn([str(self._appimage)], start_new_session=True)
        except OSError as exc:
            self._log(f"[UpdateChecker] could not install the update: {exc}")
            staged.unlink(missing_ok=True)
            self._applying = False
            self._say(f"Caroline: Update {version} could not be installed ({exc}); trying again later")
            return
        self._log("[UpdateChecker] started the updated AppImage, shutting this process down")
        self._call_soon(self._on_restart)


class UpdateChecker:
    """The GTK adapter: `on_update_available(version)` keeps the tray's "Update to {version}" item; `on_status_changed(text)` is the line for every open
    tab (chat.js's "update_status", the same contract Windows' BroadcastUpdateStatus uses); `on_restart()` is the clean shutdown of this process."""

    def __init__(self, log: Callable[[str], None], on_update_available: Callable[[str], None], on_status_changed: Callable[[str], None], on_restart: Callable[[], None]) -> None:
        self._log = log
        appimage_env = os.environ.get("APPIMAGE")
        self._appimage_path = Path(appimage_env) if appimage_env else None
        port = int(os.environ.get("CAROLINE_PORT", "48765"))
        self._client = UpdateClient(
            f"http://127.0.0.1:{port}", self._appimage_path or Path("/nonexistent"), log, on_update_available, on_status_changed, on_restart,
            call_soon=lambda fn, *a: GLib.idle_add(fn, *a),
        ) if self._appimage_path else None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def start(self) -> None:
        if not self.enabled:
            self._log("[UpdateChecker] not running from an AppImage ($APPIMAGE unset) -- self-update disabled")
            return
        self._log(f"[UpdateChecker] starting -- asking the backend every {POLL_INTERVAL_MS // 1000}s (the backend looks for releases itself)")
        GLib.timeout_add(POLL_INTERVAL_MS, self._tick)

    def _tick(self) -> bool:
        threading.Thread(target=self._client.poll, daemon=True).start()
        return True

    def update_now(self) -> None:
        """The tray's "Update to ..." item: the click itself is the owner's yes."""
        if self.enabled:
            threading.Thread(target=self._client.confirm, daemon=True).start()
