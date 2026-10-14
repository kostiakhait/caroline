"""Port of Windows/Caroline/Services/UpdateChecker.cs, adapted to
AppImage's single-file distribution model: no separate installer dance
(CarolineInstaller.exe/InstallerFetcher) -- the AppImage itself IS the
whole install unit, so self-update means downloading a fresh AppImage,
verifying its sha256, replacing the running file in place (Linux allows
replacing a file that is currently executing -- the old inode stays
alive for this still-running process via its open fd), and relaunching
it. Ironically closer to ShortNerdCat's original Go client (the model
UpdateChecker.cs's own header comment credits) than Windows' own
directory-tree install is: a single binary replacing itself, no
installer process needed at all.

Only runs at all when launched from within a real AppImage ($APPIMAGE,
set by the AppImage runtime to the mounted .AppImage's own path) -- a
dev-tree or plain dist-linux run has no single file to self-replace, so
there is nothing for this to do; `enabled` is False and start() is a
no-op in that case, same shape as every other "nothing to do on this
platform/config" gate in this codebase.

No release artifacts are published at the URLs below yet -- that needs
an actual Linux release pipeline (build_appimage.sh produces the
AppImage; uploading it to downloader.multi-portal.org is a separate,
not-yet-done publishing step, see docs/LINUX_PORT_PLAN.md). Until then
every check just finds nothing published and stays silent (_fetch_text
returns None), the same fail-open shape every network check in this
codebase already uses -- this is the client side, ready for whenever a
release exists.

Flat retry interval, never growing -- same standing rule UpdateChecker.cs
itself documents ("no exponential backoff anywhere")."""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import Callable

from gi.repository import GLib, Gtk

BASE_URL = "https://downloader.multi-portal.org/apps/caroline"
APPIMAGE_SHA256_URL = f"{BASE_URL}/Caroline-x86_64.AppImage.sha256"
APPIMAGE_VERSION_URL = f"{BASE_URL}/Caroline-x86_64.AppImage.version"
APPIMAGE_DOWNLOAD_URL = f"{BASE_URL}/Caroline-x86_64.AppImage"

INITIAL_DELAY_MS = 60_000
CHECK_INTERVAL_MS = 30 * 60_000
FAILED_RETRY_INTERVAL_MS = 5 * 60_000
DOWNLOAD_MAX_ATTEMPTS = 3
DOWNLOAD_TIMEOUT_S = 120.0
FETCH_TIMEOUT_S = 30.0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_text(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
            return body or None
    except Exception:
        return None


class UpdateChecker:
    """`on_update_available(version)`: fired as soon as a newer build is
    found, independent of the once-per-session Yes/No dialog -- lets the
    tray keep an "Update to {version}" item around after the dialog is
    dismissed. `on_status_changed(text)`: fired once the download starts
    and periodically with progress, for a status broadcast into every
    open tab (chat.js's existing "update_status" message type, same
    contract Windows' BroadcastUpdateStatus already uses -- shared code,
    no chat.js changes needed). `on_restart()`: called right after a
    verified update has been written to disk and the new AppImage
    process has been spawned -- the caller's job is a full, clean
    shutdown of this process (stop the supervisor, Gtk.main_quit()),
    mirroring Application.Current.Shutdown() after Process.Start on
    Windows."""

    def __init__(
        self, log: Callable[[str], None],
        on_update_available: Callable[[str], None],
        on_status_changed: Callable[[str], None],
        on_restart: Callable[[], None],
    ) -> None:
        self._log = log
        self._on_update_available = on_update_available
        self._on_status_changed = on_status_changed
        self._on_restart = on_restart
        appimage_env = os.environ.get("APPIMAGE")
        self._appimage_path = Path(appimage_env) if appimage_env else None
        self._prompted_this_session = False
        self._last_failed_fingerprint: str | None = None
        self._retry_timer_active = False
        self._update_in_progress = False

    @property
    def enabled(self) -> bool:
        return self._appimage_path is not None

    def start(self) -> None:
        if not self.enabled:
            self._log("[UpdateChecker] not running from an AppImage ($APPIMAGE unset) -- self-update disabled")
            return
        self._log(f"[UpdateChecker] starting -- first check in {INITIAL_DELAY_MS / 1000:.0f}s, then every {CHECK_INTERVAL_MS / 60_000:.0f}min")
        GLib.timeout_add(INITIAL_DELAY_MS, self._first_check_tick)

    def _first_check_tick(self) -> bool:
        threading.Thread(target=self._check, daemon=True).start()
        GLib.timeout_add(CHECK_INTERVAL_MS, self._check_tick)
        return False

    def _check_tick(self) -> bool:
        threading.Thread(target=self._check, daemon=True).start()
        return True

    def _retry_tick(self) -> bool:
        if self._update_in_progress:
            return True
        threading.Thread(target=self._do_retry, daemon=True).start()
        return True

    def _do_retry(self) -> None:
        """Runs off the main thread, same reason _check does -- see that
        method's own note."""
        fingerprint = _fetch_text(APPIMAGE_SHA256_URL)
        if fingerprint and self._last_failed_fingerprint and fingerprint == self._last_failed_fingerprint:
            self._log("[UpdateChecker] retry: the server looks the same as at the last failure -- nothing new to try")
            return
        self._log("[UpdateChecker] retry: server state changed (or first retry) -- trying the download again")
        self._start_download(user_initiated=False, on_mismatch_fingerprint=fingerprint)

    def _check(self) -> None:
        """Runs off the main thread (called via threading.Thread, never
        directly from a GLib timeout callback) -- a remote HTTPS fetch
        (30s timeout) plus hashing a 100+MB local AppImage file is not the
        sub-millisecond loopback cost _poll_supervisor_status/
        backend_healthy elsewhere in this codebase accept blocking the
        GTK main loop for; this would visibly freeze the whole UI for as
        long as the slowest of those takes, every 30 minutes, without
        this."""
        self._log("[UpdateChecker] check starting")
        remote_sha = _fetch_text(APPIMAGE_SHA256_URL)
        if not remote_sha:
            self._log("[UpdateChecker] check: no sha256 published (or fetch failed) -- skipping this tick")
            return
        remote_sha = remote_sha[:64].lower()
        local_sha = self._local_sha256()
        if local_sha and local_sha == remote_sha:
            self._log(f"[UpdateChecker] check: up to date (sha={remote_sha})")
            return
        remote_version = _fetch_text(APPIMAGE_VERSION_URL) or remote_sha[:8]
        self._log(f"[UpdateChecker] new version available: local={local_sha or '(unknown)'} remote={remote_sha} ({remote_version})")
        GLib.idle_add(self._on_update_available, remote_version)
        if self._prompted_this_session:
            self._log("[UpdateChecker] already prompted this session -- not re-showing the dialog, tray item stays updated")
            return
        # Set here (still on the background thread, before scheduling the
        # dialog), not inside _offer_update itself -- that only runs once
        # GLib.idle_add's callback actually reaches the main thread, which
        # is not instant; setting it this early is what actually prevents
        # a second concurrent _check() from queuing a second dialog.
        self._prompted_this_session = True
        GLib.idle_add(self._offer_update)

    def _local_sha256(self) -> str | None:
        try:
            return _sha256_file(self._appimage_path).lower()
        except OSError as exc:
            self._log(f"[UpdateChecker] could not hash the running AppImage: {exc}")
            return None

    def _offer_update(self) -> None:
        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO, text="A new version of Caroline is available.")
        dialog.format_secondary_text("Restart now to update?")
        dialog.set_title("Caroline Update")
        response = dialog.run()
        dialog.destroy()
        self._log(f"[UpdateChecker] update-available dialog result: {response}")
        if response == Gtk.ResponseType.YES:
            self.update_now()

    def update_now(self) -> None:
        """Entry point for both the Yes/No dialog's Yes and the tray's
        "Update to ..." item -- the click/Yes itself is the user's
        explicit go-ahead, no second confirmation."""
        if self._update_in_progress:
            self._log("[UpdateChecker] update_now: already in progress, ignoring")
            return
        self._start_download(user_initiated=True, on_mismatch_fingerprint=None)

    def _start_download(self, user_initiated: bool, on_mismatch_fingerprint: str | None) -> None:
        self._update_in_progress = True
        threading.Thread(target=self._download_and_apply, args=(user_initiated,), daemon=True).start()

    def _download_and_apply(self, user_initiated: bool) -> None:
        GLib.idle_add(self._on_status_changed, "Caroline: Update ongoing...")
        tmp_path = self._appimage_path.with_suffix(self._appimage_path.suffix + ".update-tmp")
        last_error: Exception | None = None
        for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
            try:
                self._download_to(tmp_path)
                break
            except Exception as exc:
                last_error = exc
                self._log(f"[UpdateChecker] download attempt {attempt}/{DOWNLOAD_MAX_ATTEMPTS} failed: {exc}")
        else:
            self._update_in_progress = False
            GLib.idle_add(self._on_download_network_failure, str(last_error), user_initiated)
            return

        remote_sha = _fetch_text(APPIMAGE_SHA256_URL)
        actual_sha = _sha256_file(tmp_path).lower()
        if not remote_sha or actual_sha != remote_sha[:64].lower():
            self._log(f"[UpdateChecker] sha256 mismatch: expected {remote_sha}, got {actual_sha}")
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._update_in_progress = False
            GLib.idle_add(self._on_mismatch, remote_sha, user_initiated)
            return

        GLib.idle_add(self._apply_update, tmp_path)

    def _download_to(self, dest: Path) -> None:
        req = urllib.request.Request(APPIMAGE_DOWNLOAD_URL)
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as resp:
            total = resp.length or 0
            written = 0
            last_percent = -1
            with dest.open("wb") as out:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
                    if total:
                        percent = int(written * 100 / total)
                        if percent != last_percent:
                            last_percent = percent
                            GLib.idle_add(self._on_status_changed, f"Caroline: Update ongoing... {percent}%")

    def _on_download_network_failure(self, message: str, user_initiated: bool) -> bool:
        self._log(f"[UpdateChecker] failed to download update: {message}")
        self._arm_retry(fingerprint=None)
        if user_initiated:
            dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK, text=f"The update couldn't be downloaded right now ({message}).")
            dialog.format_secondary_text(
                "Nothing on your computer was changed. Caroline will keep trying automatically "
                "every 5 minutes and offer the update again as soon as it works.",
            )
            dialog.set_title("Caroline Update")
            dialog.run()
            dialog.destroy()
        return False

    def _on_mismatch(self, remote_sha: str | None, user_initiated: bool) -> bool:
        self._arm_retry(fingerprint=remote_sha)
        self._on_status_changed("Caroline: Update waiting for the server -- retrying every 5 minutes")
        if user_initiated:
            dialog = Gtk.MessageDialog(
                message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK,
                text="The download server is offering a file that doesn't match its own published checksum.",
            )
            dialog.format_secondary_text(
                "That's a problem on the server, not with your connection or this computer. Nothing on your "
                "computer was changed. Caroline will keep checking every 5 minutes and offer the update again "
                "as soon as a valid one is available.",
            )
            dialog.set_title("Caroline Update")
            dialog.run()
            dialog.destroy()
        return False

    def _arm_retry(self, fingerprint: str | None) -> None:
        self._last_failed_fingerprint = fingerprint
        if not self._retry_timer_active:
            self._retry_timer_active = True
            self._log(f"[UpdateChecker] a failed update will be re-examined every {FAILED_RETRY_INTERVAL_MS / 60_000:.0f} min until it works")
            GLib.timeout_add(FAILED_RETRY_INTERVAL_MS, self._retry_tick)

    def _apply_update(self, tmp_path: Path) -> bool:
        self._log(f"[UpdateChecker] verified update ready, replacing {self._appimage_path}")
        try:
            os.replace(tmp_path, self._appimage_path)
            os.chmod(self._appimage_path, 0o755)
            subprocess.Popen([str(self._appimage_path)], start_new_session=True)
        except OSError as exc:
            self._log(f"[UpdateChecker] failed to apply update: {exc}")
            self._update_in_progress = False
            dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK, text=f"Could not install the downloaded update: {exc}")
            dialog.set_title("Caroline Update")
            dialog.run()
            dialog.destroy()
            return False
        self._log("[UpdateChecker] relaunched the updated AppImage, shutting this process down")
        self._on_restart()
        return False
