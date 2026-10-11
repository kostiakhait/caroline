"""Hard kill of a whole process tree -- what the Stop button is for.

Stop is the absolute kill switch (per explicit instruction, restated
2026-10-09: "Стоп должен грубо вырубать все процессы всех запущенных
агентов... чтобы если Кэролайн пытается сделать что-то опасное, её можно
было остановить"). Nothing here is polite: no interrupt request, no grace
period, no waiting for a process to wind down on its own.

`taskkill /T` alone is not enough: it walks the tree from the root at the
moment it runs, so a child whose parent has already exited (a shell that
launched a command and died, an agent that detached its worker) is no
longer attached to anything and survives. So the tree is SNAPSHOTTED first,
while every parent link is still there, and then every process in the
snapshot is killed by its own pid -- parent first, so that nothing is left
to spawn replacements while its children are being killed.
"""

from __future__ import annotations

import os
import subprocess
import sys

from app.logging_setup import log_event

try:
    import psutil
except Exception:  # noqa: BLE001 -- optional dependency; taskkill/killpg below still work without it
    psutil = None  # type: ignore[assignment]


def process_tree(pid: int) -> list[int]:
    """The pid and every descendant alive right now, root first."""
    if psutil is None:
        return [pid]
    try:
        root = psutil.Process(pid)
        return [pid] + [child.pid for child in root.children(recursive=True)]
    except Exception:  # noqa: BLE001 -- already gone, or not ours to inspect
        return [pid]


def kill_process_tree(pid: int | None, why: str = "") -> int:
    """Kills the process and everything under it, at once. Never raises.
    Returns how many processes were found to kill."""
    if not pid or pid == os.getpid():
        return 0
    pids = process_tree(pid)
    killed = 0
    for target in pids:
        try:
            if psutil is not None:
                psutil.Process(target).kill()
            else:
                os.kill(target, 9)
            killed += 1
        except Exception:  # noqa: BLE001 -- already gone is exactly what we wanted
            pass
    # Belt and braces: the OS's own tree kill, for anything spawned between
    # the snapshot and the kills above.
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                ["taskkill.exe", "/F", "/T", "/PID", str(pid)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass
    log_event("engine", "process_tree_killed", pid=pid, processes=len(pids), killed=killed, why=why)
    return len(pids)
