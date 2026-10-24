"""Visual Mode on Linux: the talking-head renderer (vendor/XcfaRenderer, C#) run as a
headless child process, XcfaBridge, and spoken to over a pipe (one JSON object per
line, see vendor/XcfaRenderer/Bridge/Program.cs).

The WPF shell calls the renderer in-process (VisualModeManager.cs); this is the same
sequence for the Python shell: warm the model once (configure), render each clip
(visual_speech_prepare renders it ahead, visual_speech_audio plays it), cancel clips
nobody will play. Clips render one at a time in the order they arrive.

State of the port: the clips are RENDERED for real (the render time, the cost of
each remark in animated mode, is what narration timing depends on) and visual_speech_done
is reported when the clip's duration has passed, so the page's speech queue runs at
the pace of real playback. Showing the video in a window is not done yet, and nothing
is heard from a clip rendered here, so the bridge is off unless CAROLINE_VISUAL_BRIDGE=1
is set -- without it the shell keeps answering `played: false` and chat.js plays plain
audio, as before."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

ENABLED_ENV = "CAROLINE_VISUAL_BRIDGE"
BRIDGE_DLL_ENV = "CAROLINE_XCFA_BRIDGE"  # path of XcfaBridge.dll (run with `dotnet`)
FFMPEG_ENV = "CAROLINE_FFMPEG"


def enabled() -> bool:
    return os.environ.get(ENABLED_ENV) == "1"


class _Clip:
    def __init__(self, request_id: str, audio_path: Path, out_path: Path) -> None:
        self.request_id = request_id
        self.audio_path = audio_path
        self.out_path = out_path
        self.sent_at = time.monotonic()
        self.done = threading.Event()
        self.ok = False
        self.duration_s = 0.0
        self.render_ms = 0
        self.waiting: list[Callable[[bool], None]] = []  # visual_speech_audio callers


class VisualBridge:
    def __init__(self, log: Callable[[str], None], run_on_main: Callable[[Callable[[], None]], None]) -> None:
        """log: the shell's logger. run_on_main: schedules a callable on the GTK main loop
        (the page can only be spoken to from there)."""
        self._log = log
        self._run_on_main = run_on_main
        self._proc: subprocess.Popen[str] | None = None
        self._ready = threading.Event()
        self._clips: dict[str, _Clip] = {}
        self._lock = threading.Lock()
        self._dir = Path(tempfile.gettempdir()) / "CarolineVisualMode"
        self._dir.mkdir(parents=True, exist_ok=True)

    # -- process -----------------------------------------------------------------

    def configure(self, model_path: str | None) -> None:
        """visual_mode_config: warm the model (None/empty: Visual Mode is off)."""
        if not model_path:
            self._log("visual bridge: no model to warm (Visual Mode off or unavailable)")
            return
        dll = os.environ.get(BRIDGE_DLL_ENV)
        if not dll or not Path(dll).exists():
            self._log(f"visual bridge: {BRIDGE_DLL_ENV} not set or missing ({dll!r}); Visual Mode stays off")
            return
        if self._proc is None or self._proc.poll() is not None:
            self._ready.clear()
            self._proc = subprocess.Popen(
                ["dotnet", dll], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1, encoding="utf-8",
            )
            threading.Thread(target=self._read_events, args=(self._proc,), daemon=True, name="xcfa-bridge-reader").start()
        self._send({"cmd": "configure", "modelPath": model_path, "ffmpeg": os.environ.get(FFMPEG_ENV, "ffmpeg"), "height": 480})
        self._log(f"visual bridge: warming {model_path}")

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                self._send({"cmd": "quit"}, proc)
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()

    def _send(self, payload: dict, proc: subprocess.Popen[str] | None = None) -> None:
        proc = proc or self._proc
        if proc is None or proc.stdin is None:
            return
        with self._lock:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

    # -- page messages -----------------------------------------------------------

    def prepare(self, request_id: str, audio_b64: str) -> _Clip | None:
        """visual_speech_prepare (and the first half of visual_speech_audio): start rendering."""
        if not self._ready.is_set():
            return None
        existing = self._clips.get(request_id)
        if existing is not None:
            return existing
        audio_path = self._dir / f"{request_id}.mp3"
        audio_path.write_bytes(base64.b64decode(audio_b64))
        clip = _Clip(request_id, audio_path, self._dir / f"{request_id}.webm")
        self._clips[request_id] = clip
        self._send({"cmd": "render", "id": request_id, "audio": str(audio_path), "out": str(clip.out_path)})
        return clip

    def play(self, request_id: str, audio_b64: str, on_done: Callable[[bool], None]) -> None:
        """visual_speech_audio: on_done(played) once the clip has been rendered and its
        duration has passed; on_done(False) at once when nothing could be rendered (the page
        then plays plain audio)."""
        clip = self.prepare(request_id, audio_b64)
        if clip is None:
            on_done(False)
            return
        if clip.done.is_set():
            self._finish(clip, on_done)
        else:
            clip.waiting.append(on_done)

    def cancel(self, request_id: str) -> None:
        clip = self._clips.pop(request_id, None)
        if clip is not None:
            self._send({"cmd": "cancel", "id": request_id})

    # -- events from the bridge --------------------------------------------------

    def _read_events(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("event")
            if kind == "ready":
                self._ready.set()
                self._log(f"visual bridge: model warm in {event.get('prepMs')} ms")
            elif kind == "rendered":
                clip = self._clips.get(event.get("id"))
                if clip is None:
                    continue
                clip.ok = bool(event.get("ok"))
                clip.duration_s = float(event.get("durationS") or 0.0)
                clip.render_ms = int(event.get("renderMs") or 0)
                self._log(
                    f"visual bridge: clip {clip.request_id} ok={clip.ok} render_ms={clip.render_ms} "
                    f"waited_ms={int((time.monotonic() - clip.sent_at) * 1000)} duration_s={clip.duration_s:.1f} "
                    f"{event.get('error') or ''}"
                )
                clip.done.set()
                for waiter in clip.waiting:
                    self._run_on_main(lambda w=waiter, c=clip: self._finish(c, w))
                clip.waiting.clear()
            elif kind in ("cancelled", "error"):
                self._log(f"visual bridge: {line.strip()}")

    def _finish(self, clip: _Clip, on_done: Callable[[bool], None]) -> None:
        """The clip is rendered: report it played after its own duration (see the module docstring)."""
        if not clip.ok:
            self._discard(clip)
            on_done(False)
            return

        def done() -> None:
            self._discard(clip)
            on_done(True)

        threading.Timer(clip.duration_s, lambda: self._run_on_main(done)).start()

    def _discard(self, clip: _Clip) -> None:
        self._clips.pop(clip.request_id, None)
        for path in (clip.audio_path, clip.out_path):
            try:
                path.unlink()
            except OSError:
                pass
