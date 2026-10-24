"""Visual Mode on Linux: the talking-head renderer (vendor/XcfaRenderer, C#) run as a
headless child process, XcfaBridge, and spoken to over a pipe (one JSON object per
line, see vendor/XcfaRenderer/Bridge/Program.cs).

The WPF shell calls the renderer in-process (VisualModeManager.cs); this is the same
sequence for the Python shell: warm the model once (configure), render each clip
(visual_speech_prepare renders it ahead, visual_speech_audio plays it), cancel clips
nobody will play. Clips render one at a time in the order they arrive.

Playback (2026-10-24): the rendered clip is shown and heard in visual_window.VisualWindow --
borderless, transparent, above the other windows; for a dialogue it stays up between the
remarks, for a clip played from a message's speaker button it closes a second after the clip,
and the user's Stop closes it. visual_speech_done is reported when the clip really ended (or
was stopped); when a clip cannot be rendered or played, played=False and chat.js plays plain
audio. Off unless CAROLINE_VISUAL_BRIDGE=1 is set -- without it the shell keeps answering
`played: false`, as before."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
from typing import Any
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
        self.waiting: list[Callable[[bool, bool], None]] = []  # visual_speech_audio callers
        self.dialogue = False


class VisualBridge:
    def __init__(self, log: Callable[[str], None], run_on_main: Callable[[Callable[[], None]], None],
                 chat_rect: Callable[[], tuple[int, int, int, int] | None] | None = None) -> None:
        """log: the shell's logger. run_on_main: schedules a callable on the GTK main loop
        (the page can only be spoken to from there)."""
        self._log = log
        self._run_on_main = run_on_main
        self._chat_rect = chat_rect  # where the chat window is, so the animation window goes beside it
        self._proc: subprocess.Popen[str] | None = None
        self._ready = threading.Event()
        self._clips: dict[str, _Clip] = {}
        self._lock = threading.Lock()
        self._dir = Path(tempfile.gettempdir()) / "CarolineVisualMode"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._window: Any = None
        self._rest: str | None = None  # the model's rest pose (PNG), from the renderer's "ready" event
        self._hide_generation = 0
        self._persistent = False
        self._playing: Callable[[bool, bool], None] | None = None

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

    def play(self, request_id: str, audio_b64: str, dialogue: bool, persistent: bool, on_done: Callable[[bool, bool], None]) -> None:
        """visual_speech_audio: on_done(played, stopped) when the clip has been rendered and shown
        to its end (or stopped by the user); on_done(False, False) at once when nothing could be
        rendered or shown (the page then plays plain audio). `dialogue`: the window stays up
        afterwards (a voice reply or a narration remark), else it closes a second later.
        `persistent`: continuous narration -- the window is never closed by this code, whatever
        happens (2026-10-24, explicit instruction: opened once, never closed); Stop only stops
        the clip. Ordinary playback is as before."""
        if persistent:
            self._persistent = True
        clip = self.prepare(request_id, audio_b64)
        if clip is None:
            on_done(False, False)
            return
        clip.dialogue = dialogue
        if clip.done.is_set():
            self._finish(clip, on_done)
        else:
            clip.waiting.append(on_done)

    def stop(self) -> None:
        """visual_speech_stop: the user pressed Stop in the chat window."""
        if self._window is not None and self._window.is_visible():
            if self._persistent:
                self._window.stop_playback()  # the window stays
            else:
                self._window.hide()
            done, self._playing = self._playing, None
            if done is not None:
                done(True, True)

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
                self._rest = event.get("rest")
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

    def _finish(self, clip: _Clip, on_done: Callable[[bool, bool], None]) -> None:
        """The clip is rendered: show it (on the GTK main thread) and report when it has played."""
        if not clip.ok:
            self._discard(clip)
            on_done(False, False)
            return
        try:
            if self._window is None:
                import visual_window

                self._window = visual_window.VisualWindow(self._log)
                if self._rest:
                    self._window.set_rest(self._rest)
        except Exception as exc:  # noqa: BLE001 -- no display, no WebKit: plain audio instead
            self._log(f"visual bridge: window could not be made: {exc}")
            self._discard(clip)
            on_done(False, False)
            return
        self._hide_generation += 1

        def finished(played: bool, stopped: bool) -> None:
            self._playing = None
            self._discard(clip)
            if not self._persistent and (stopped or not clip.dialogue):
                generation = self._hide_generation

                def maybe_hide() -> bool:
                    if generation == self._hide_generation and self._window is not None:
                        self._window.hide()
                    return False

                from gi.repository import GLib

                GLib.timeout_add(0 if stopped else 1000, maybe_hide)
            on_done(played, stopped)

        def on_event(kind: str, payload: dict) -> None:
            callback, self._playing = self._playing, None
            if callback is None:
                return
            callback(kind != "error", kind == "stopped")

        self._playing = finished
        try:
            rect = self._chat_rect() if self._chat_rect else None
        except Exception:  # noqa: BLE001
            rect = None
        self._window.play(str(clip.out_path), on_event, rect)

    def _discard(self, clip: _Clip) -> None:
        self._clips.pop(clip.request_id, None)
        for path in (clip.audio_path, clip.out_path):
            try:
                path.unlink()
            except OSError:
                pass
