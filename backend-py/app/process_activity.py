"""Per-process OS-level activity monitor -- CPU%/RSS/IO-based, replacing
the former last-SDK-message-based hang-detection signal.

Per explicit instruction (2026-09-15): "Это должно быть 90-секунд
отсчитываемых, когда ничего не происходит: процессы не потребляют процессор
и не меняется загрузка памяти. Это не таймаут выполнения, а таймаут от
зависания." -- a genuine hang is the underlying claude.exe OS process doing
NOTHING (no CPU, no memory movement) for the whole window, which is NOT the
same thing as "no SDK message arrived": a slow API round-trip against a
large context, or a live native /compact, can both go 90+ seconds with zero
SDK messages while the process itself is genuinely busy the whole time --
confirmed live via a direct isolated test: a real /compact against a
realistically-sized session took 109.5 seconds end to end, with no
intermediate SDK message of any kind, yet the process was doing real work.
The old last-SDK-message clock could not tell that case apart from an
actual dead/frozen process; this can.

Uses psutil (added to PythonInstaller.cs's PythonPackages) -- the practical
way to read another process's CPU%/RSS/IO from Python on Windows. CPU/RSS
alone were found (while testing this module) to be insufficient on their
own -- see IO_ACTIVITY_THRESHOLD_BYTES's own comment for why a third,
IO-based signal is also tracked.
"""

from __future__ import annotations

import time

import psutil

# Below these, a sample counts as "no measurable activity" this tick --
# small enough not to be fooled by a genuinely idle process's own tiny
# allocator/scheduler jitter, large enough that a truly working process
# (JSON encode/decode, TLS, buffering a growing response, even background
# GC) reliably crosses it on at least one of the two axes. Own chosen
# values, not measured constants -- revisit if real logs show either false
# positives (flagged as hung while genuinely working) or false negatives
# (never detects an actually-frozen process).
CPU_ACTIVITY_THRESHOLD_PERCENT = 1.0
MEMORY_ACTIVITY_THRESHOLD_BYTES = 64 * 1024
# Bug fix (2026-09-15), found while testing this same module: CPU% and RSS
# alone cannot tell "the process is genuinely stuck" apart from "the
# process is blocked reading a slow-but-alive network response" -- both
# show ~0% CPU and unchanged memory while blocked in a socket read. This is
# exactly the scenario that actually matters here (a real API call against
# a large context, or a /compact round-trip, both spend most of their time
# waiting on the network, not computing locally). psutil's io_counters()
# tracks cumulative bytes moved by the process -- on Windows, socket I/O
# typically shows up under `other_bytes` (Win32 IO_COUNTERS' OtherTransfer,
# not ReadTransfer/WriteTransfer, which are mostly file-handle I/O), so all
# three are summed. Any of these bytes still moving means the connection is
# genuinely alive and receiving/sending data, even if no complete SDK
# message has been assembled yet. A small threshold on purpose -- even a
# handful of TLS/HTTP2 frames trickling in should count.
IO_ACTIVITY_THRESHOLD_BYTES = 4 * 1024


class ProcessActivityMonitor:
    """Tracks whether a given OS process has shown measurable CPU or memory
    activity, sampled on demand -- call sample() once per watchdog tick
    (see ChatSession._check_hang). Not thread-safe; one instance per live
    CLI subprocess, recreated whenever the pid changes (a fresh
    connection) -- see ChatSession's own _cli_process_pid/cli-pid-sink
    wiring in win_subprocess_patch.py."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._process: psutil.Process | None = None
        self._last_rss: int | None = None
        self._last_io_bytes: int | None = None
        self._io_supported = True
        self._last_activity_at = time.monotonic()
        self._available = True
        try:
            self._process = psutil.Process(pid)
            # Prime cpu_percent() -- psutil's own documented contract: the
            # FIRST call after construction always returns a meaningless
            # 0.0 (no prior sample to diff against). This call establishes
            # that baseline so the next real sample() call is accurate.
            self._process.cpu_percent(interval=None)
            self._last_rss = self._process.memory_info().rss
            self._last_io_bytes = self._read_io_bytes()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            self._available = False

    def _read_io_bytes(self) -> int | None:
        if not self._io_supported or self._process is None:
            return None
        try:
            io = self._process.io_counters()
            return io.read_bytes + io.write_bytes + getattr(io, "other_bytes", 0)
        except (NotImplementedError, AttributeError):
            # Some platforms/processes genuinely don't expose this --
            # degrade gracefully to CPU/RSS only rather than treating a
            # missing io_counters() as the whole process being unreadable.
            self._io_supported = False
            return None

    def sample(self) -> bool:
        """Call once per tick. Returns True if this process has shown
        measurable activity (CPU, memory, or network/file I/O) since the
        LAST sample() call, and refreshes the internal "last activity
        seen" clock in that case, so seconds_since_last_activity() stays
        accurate.

        Returns True (never counts toward a hang) if the process can no
        longer be read at all -- an unreadable process is a MONITORING
        failure, not evidence of a hang; the caller should fall back to
        another signal (see `available`) rather than treat "can't tell"
        as "definitely hung"."""
        if not self._available or self._process is None:
            return True
        try:
            cpu = self._process.cpu_percent(interval=None)
            mem = self._process.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            self._available = False
            return True
        io_bytes = self._read_io_bytes()
        active = (
            cpu >= CPU_ACTIVITY_THRESHOLD_PERCENT
            or (self._last_rss is not None and abs(mem - self._last_rss) >= MEMORY_ACTIVITY_THRESHOLD_BYTES)
            or (
                io_bytes is not None and self._last_io_bytes is not None
                and io_bytes - self._last_io_bytes >= IO_ACTIVITY_THRESHOLD_BYTES
            )
        )
        self._last_rss = mem
        if io_bytes is not None:
            self._last_io_bytes = io_bytes
        if active:
            self._last_activity_at = time.monotonic()
        return active

    def seconds_since_last_activity(self) -> float:
        return time.monotonic() - self._last_activity_at

    @property
    def available(self) -> bool:
        return self._available
