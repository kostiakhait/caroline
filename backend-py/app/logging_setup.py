"""Structured (JSON-lines) logging for the whole backend.

Per the migration plan's explicit logging requirement: every record is one
JSON object per line (timestamp, component, event, plus whatever fields the
caller passes) -- still fine to eyeball with `tail -f`, but actually
greppable/parseable, unlike free-text string interpolation. Written to
stdout so it lands in the SAME combined stream MainWindow.xaml.cs's
BackendProcess already pipes into Logger.Log (and the WPF shell's own C#
logs / the external health-watchdog's logs) -- one correlatable timeline,
matching today's proven pattern, not split into separate files.

Every caller goes through log_event() (engine code) or the plugin
dispatcher's own automatic wrapping (see plugins/loader.py) -- individual
plugins are never expected to add their own ad hoc logging for the
call/result/duration triad, that's a systemic guarantee from this layer.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

# Bug fix (2026-09-11), confirmed live: pythonw.exe with RedirectStandardOutput
# (not a real console) doesn't default sys.stdout/stderr to UTF-8 on Windows --
# without PYTHONIOENCODING set, Python falls back to the process's ANSI
# codepage. log_event below deliberately writes real Cyrillic (ensure_ascii=
# False, for readability) any time a caller logs actual dialogue/comment
# text (e.g. _check_progress_narration's dialogue_preview/comment fields) --
# on that codepage, print() then throws UnicodeEncodeError ("'charmap' codec
# can't encode characters..."). Confirmed live: this silently aborted the
# progress narrator's watchdog tick every single time it tried to log
# anything with Cyrillic in it (~once a minute, every attempt, for the
# whole length of an active Russian conversation) -- caught by the outer
# per-tick guard so it didn't crash the process, but no narration comment
# ever got sent, making an actively-working tab look completely frozen to
# the user (nothing else stood in for it). One-time fix, right here, at
# import time -- log_event is the single choke point every log call in the
# whole backend goes through, so this covers all of them, not just
# narration's own calls.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass  # best-effort -- a stream that can't reconfigure just keeps its old encoding


# Redesign (2026-11-10), confirmed live: print(..., flush=True) on a stdout PIPE blocks the calling thread while the
# reader (the WPF shell) is behind -- and log_event is called from the asyncio loop thread, so a slow reader froze the
# whole event loop for 3-18 s (event_loop_blocked), after which the supervisor found /api/status silent and killed the
# backend. The record is now only queued; one daemon thread does the (possibly blocking) write. Order is kept (one
# queue, one writer). The queue is bounded so a reader that is stuck for good cannot eat the memory: past the cap new
# records are counted and dropped, and one "log_records_dropped" line says how many once the writer catches up.
import atexit
import queue
import threading

_LOG_QUEUE_MAX = 50_000
_log_queue: "queue.Queue[str | None]" = queue.Queue(maxsize=_LOG_QUEUE_MAX)
_log_dropped = 0
_log_writer: threading.Thread | None = None
_log_writer_lock = threading.Lock()


def _write_line(line: str) -> None:
    try:
        print(line, file=sys.stdout, flush=True)
    except Exception:
        pass  # a log line must never take anything down (closed pipe, encoding)


def _log_writer_loop() -> None:
    global _log_dropped
    while True:
        line = _log_queue.get()
        if line is None:
            return
        _write_line(line)
        if _log_dropped and _log_queue.qsize() < _LOG_QUEUE_MAX // 2:
            n, _log_dropped = _log_dropped, 0
            _write_line(_render("engine", "log_records_dropped", {"count": n}))


def _drain_log_at_exit() -> None:
    writer = _log_writer
    if writer is None or not writer.is_alive():
        return
    try:
        _log_queue.put(None, timeout=1.0)
    except queue.Full:
        return
    writer.join(timeout=3.0)


def _ensure_log_writer() -> bool:
    global _log_writer
    if _log_writer is not None:
        return True
    with _log_writer_lock:
        if _log_writer is None:
            try:
                t = threading.Thread(target=_log_writer_loop, name="log-writer", daemon=True)
                t.start()
                _log_writer = t
                atexit.register(_drain_log_at_exit)
            except Exception:
                return False
    return True


def _render(component: str, event: str, fields: dict[str, Any]) -> str:
    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}",
        "component": component,
        "event": event,
        **fields,
    }
    return json.dumps(record, ensure_ascii=False, default=str)


def log_event(component: str, event: str, **fields: Any) -> None:
    """Writes one JSON-lines record to stdout (through the writer thread, see above). `component` is e.g.
    "engine", "ws", "http", or "plugin:<name>"; `event` is a short
    snake_case name (e.g. "query_created", "tool_call", "state_transition").
    Extra keyword args become the record's payload -- pass whatever's
    relevant (tab_id, session_id, duration_ms, error, matched_pattern, ...).
    """
    global _log_dropped
    line = _render(component, event, fields)
    if not _ensure_log_writer():
        _write_line(line)
        return
    try:
        _log_queue.put_nowait(line)
    except queue.Full:
        _log_dropped += 1
