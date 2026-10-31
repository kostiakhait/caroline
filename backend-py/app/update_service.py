"""The update client every platform shares (docs/UPDATE_EVERYWHERE_PLAN.md): look at the signed release manifest, download and verify this platform's
artifact in the background, and say what to do with it. Installing is the platform's own business (Windows' installer, the AppImage's replace, OS
Beroun's `shelld`): this module stops at "ready: a verified file at <path>".

States of `status`:  up_to_date | available | downloading | ready | needs_runtime | failed | idle (nothing checked yet)
The rule (docs, "The rule"): `action(busy)` is "install" when Caroline is inactive, "ask" when she is in use and the owner has not yet said yes;
the owner's yes (`confirm()`) turns "ask" into "install". A ready update therefore installs by itself at the first inactive moment, answered or not.

Her own version is `backend-py/VERSION`: every platform ships backend-py, so every platform knows its version. The manifest is read from
`<base>/release.json` and `<base>/release.json.sig`; nothing of it is trusted before the signature checks (app/release_manifest.py). Retries are flat
(5 minutes), never growing.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from app import release_manifest as rm
from app.logging_setup import log_event

DEFAULT_BASE_URL = "https://downloader.multi-portal.org/apps/caroline"
FIRST_CHECK_S = 10
CHECK_INTERVAL_S = 1800
RETRY_S = 300
DOWNLOAD_ATTEMPTS = 3
HTTP_TIMEOUT_S = 30
APP_ROOT = Path(__file__).resolve().parent.parent  # backend-py


def current_version() -> str:
    try:
        return (APP_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "dev"


def detect_platform() -> str:
    """The artifact this Caroline is updated with. `CAROLINE_UPDATE_PLATFORM` (set by the shell that knows) wins; else the edition file of OS Beroun's
    dist-intellicat, else the operating system."""
    forced = (os.environ.get("CAROLINE_UPDATE_PLATFORM") or "").strip()
    if forced in rm.PLATFORMS:
        return forced
    if sys.platform.startswith("win"):
        return "windows"
    try:
        if (APP_ROOT.parent / "EDITION").read_text(encoding="utf-8").strip() == "intellicat":
            return "beroun"
    except OSError:
        pass
    return "linux-x86_64"


def have_runtime() -> int:
    """The runtime number of this platform (section 3 of the plan): what the update does not carry. OS Beroun's shell sets CAROLINE_RUNTIME."""
    try:
        return int(os.environ.get("CAROLINE_RUNTIME") or 0)
    except ValueError:
        return 0


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Caroline-updater"}), timeout=HTTP_TIMEOUT_S) as resp:
        return resp.read()


def _fetch_to(url: str, dest: Path, progress: Callable[[int], None] | None) -> None:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Caroline-updater"}), timeout=HTTP_TIMEOUT_S) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last = -1
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if progress and total and done * 100 // total != last:
                last = done * 100 // total
                progress(last)


class UpdateService:
    def __init__(self, *, base_url: str | None = None, platform: str | None = None, version: str | None = None, runtime: int | None = None,
                 download_dir: str | Path | None = None, public_key: bytes | None = None) -> None:
        self.base_url = (base_url or os.environ.get("CAROLINE_UPDATE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.platform = platform or detect_platform()
        self.version = version or current_version()
        self.runtime = have_runtime() if runtime is None else runtime
        self.download_dir = Path(download_dir or os.environ.get("CAROLINE_UPDATE_DIR") or Path(tempfile.gettempdir()) / "Caroline-update")
        self.public_key = public_key
        self._lock = threading.RLock()
        self._confirmed = False
        self._state: dict[str, Any] = {"status": "idle", "error": None, "checked_at": None, "version": None, "path": None, "progress": None, "payload": None, "payload_path": None}
        self._artifact: rm.Artifact | None = None

    # ---- state --------------------------------------------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._state, "installed_version": self.version, "platform": self.platform, "confirmed": self._confirmed}

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)

    def action(self, busy: bool) -> str:
        """"install" | "ask" | "none": what the platform does with the update right now."""
        with self._lock:
            if self._state["status"] != "ready":
                return "none"
            return "install" if (self._confirmed or not busy) else "ask"

    def confirm(self) -> None:
        """The owner said yes while Caroline was in use."""
        with self._lock:
            self._confirmed = True

    def mark_installed(self) -> None:
        """The platform started the install (the process is about to restart): forget the verified file's state."""
        with self._lock:
            self._confirmed = False
            self._state.update(status="idle", path=None, payload_path=None, progress=None)

    # ---- check and download (blocking: run them in a thread) ----------------------------------------------------------------
    def check(self) -> dict[str, Any]:
        try:
            manifest_bytes = _fetch(f"{self.base_url}/release.json")
            signature = _fetch(f"{self.base_url}/release.json.sig").decode("ascii").strip()
            manifest = rm.verify_manifest(manifest_bytes, signature, self.public_key)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # no release published yet: nothing to do, and nothing to retry every five minutes
                self._set(status="up_to_date", error=None, checked_at=time.time())
                return self.status()
            self._set(status="failed" if self._state["status"] not in ("ready", "downloading") else self._state["status"], error=str(exc), checked_at=time.time())
            log_event("updater", "check_failed", error=str(exc))
            return self.status()
        except Exception as exc:  # noqa: BLE001 -- offline, no release published yet, a bad signature: all the same to the owner, none of them an update
            self._set(status="failed" if self._state["status"] not in ("ready", "downloading") else self._state["status"], error=str(exc), checked_at=time.time())
            log_event("updater", "check_failed", error=str(exc))
            return self.status()
        version = manifest["version"]
        if not rm.is_newer(version, self.version):
            self._artifact = None
            self._set(status="up_to_date", error=None, checked_at=time.time(), version=version)
            return self.status()
        artifact, why = rm.artifact_for(manifest, self.platform, self.runtime)
        if artifact is None:
            self._set(status="up_to_date", error=None, checked_at=time.time(), version=version)
            log_event("updater", "release_without_artifact", platform=self.platform, version=version)
        elif why == "needs_runtime":
            self._artifact = None
            self._set(status="needs_runtime", error=f"release {version} needs runtime {artifact.runtime}, this platform has {self.runtime}", checked_at=time.time(), version=version)
        else:
            if self._artifact != artifact or self._state["status"] not in ("ready", "downloading"):
                self._artifact = artifact
                self._set(status="available", error=None, checked_at=time.time(), version=version, path=None, payload=artifact.payload, payload_path=None)
            log_event("updater", "update_available", version=version, installed=self.version, platform=self.platform)
        return self.status()

    def download(self, progress: Callable[[int], None] | None = None) -> dict[str, Any]:
        """Downloads the artifact of the last check and verifies it against the signed manifest. `ready` only for a file that matches exactly.

        The payload too (Windows' Caroline.zip), per explicit instruction (2026-10-31, confirmed live with 1.0.1): the installer used to download
        it only after Caroline had closed -- 515 MB with nothing on the screen. Now `ready` means both files are here and verified
        (`payload_path`), and the installer is handed the local zip."""
        artifact = self._artifact
        if artifact is None or self._state["status"] not in ("available", "failed", "ready"):
            return self.status()
        files = [artifact]
        if artifact.payload:
            pay = artifact.payload
            files.append(rm.Artifact(artifact.platform, pay["file"], pay["sha256"], pay["size"], 0))
        self.download_dir.mkdir(parents=True, exist_ok=True)
        keep = {f.file for f in files} | {f.file + ".part" for f in files}
        for old in self.download_dir.iterdir():  # leftovers of earlier versions and half downloads
            if old.name not in keep:
                old.unlink(missing_ok=True)
        total = sum(f.size for f in files) or 1
        done = 0
        paths: list[Path] = []
        for f in files:
            def report(pct: int, base: int = done, size: int = f.size) -> None:
                overall = (base + size * pct // 100) * 100 // total
                self._set(progress=overall)
                if progress:
                    progress(overall)
            path = self._obtain(f, report)
            if path is None:
                return self.status()
            paths.append(path)
            done += f.size
        self._set(status="ready", path=str(paths[0]), payload_path=str(paths[1]) if len(paths) > 1 else None, error=None, progress=100)
        log_event("updater", "update_ready", version=self._state["version"], path=str(paths[0]), payload_path=self._state["payload_path"])
        return self.status()

    def _obtain(self, art: rm.Artifact, report: Callable[[int], None]) -> Path | None:
        """One file of the release, here and verified: the path, or None with the state `failed`."""
        final = self.download_dir / art.file
        try:
            rm.check_file(final, art)  # already here and verified: no second download
            return final
        except (rm.ReleaseError, OSError):
            final.unlink(missing_ok=True)
        part = self.download_dir / (art.file + ".part")
        self._set(status="downloading", error=None)
        last_error = ""
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                _fetch_to(f"{self.base_url}/{art.file}", part, report)
                rm.check_file(part, art)
                os.replace(part, final)
                return final
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                log_event("updater", "download_failed", file=art.file, attempt=attempt, error=last_error)
                part.unlink(missing_ok=True)
        self._set(status="failed", error=last_error, progress=None)
        return None

    def run_once(self) -> dict[str, Any]:
        state = self.check()
        if state["status"] == "available":
            state = self.download()
        return state

    async def run_forever(self) -> None:
        """The background loop: 10 seconds after the start (a freshly started Caroline is inactive, so a waiting update goes in first), then every 30 minutes; after a failure, every 5 minutes (flat)."""
        await asyncio.sleep(FIRST_CHECK_S)
        while True:
            state = await asyncio.to_thread(self.run_once)
            await asyncio.sleep(RETRY_S if state["status"] == "failed" else CHECK_INTERVAL_S)
