"""Persistent registry of the background agents/tasks a tab has launched.

Why (2026-09-24, "она запускает агентов, но после компактизации забывает что
запустила"): an async agent's only trace in the conversation is the "Async agent
launched successfully ... agentId: X" tool result. Compaction can summarize that
away, and a backend restart (every restart kills the CLI process, and with it every
agent living inside it -- silently, no completion notification ever arrives) leaves
the history claiming an agent is running that no longer exists. The SDK reports the
lifecycle as typed events (TaskStartedMessage / TaskNotificationMessage /
TaskUpdatedMessage) that nothing consumed; this module is where they land.

State lives in workspace/agents-<tabId>.json:
  running -- launched by the CURRENT CLI process, not yet finished
  lost    -- were running when a CLI process died; kept until the model has been told
A human-readable copy (what the model is pointed at) is written to
workspace/running-agents-<tabId>.txt after every change, so it is always current no
matter what compaction did to the conversation.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.durability import _sanitize_tab_id
from app.logging_setup import log_event

_lock = threading.Lock()


def _state_path(workspace_dir: str, tab_id: str) -> Path:
    return Path(workspace_dir) / f"agents-{_sanitize_tab_id(tab_id)}.json"


def status_file_path(workspace_dir: str, tab_id: str) -> Path:
    return Path(workspace_dir) / f"running-agents-{_sanitize_tab_id(tab_id)}.txt"


def _load(workspace_dir: str, tab_id: str) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(workspace_dir, tab_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("running", {})
    data.setdefault("lost", [])
    return data


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _save(workspace_dir: str, tab_id: str, data: dict[str, Any]) -> None:
    try:
        _atomic_write(_state_path(workspace_dir, tab_id), json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        _atomic_write(status_file_path(workspace_dir, tab_id), render_text(data))
    except Exception as exc:
        log_event("engine", "agent_registry_save_failed", tab_id=tab_id, error=str(exc))


def _line(task_id: str, entry: dict[str, Any]) -> str:
    kind = f" [{entry['task_type']}]" if entry.get("task_type") else ""
    return f"- agentId {task_id}{kind}: {entry.get('description') or '(no description)'} -- launched {entry.get('launched_at', '?')}"


def render_text(data: dict[str, Any]) -> str:
    running, lost = data.get("running") or {}, data.get("lost") or []
    parts = [
        "Background agents/tasks YOU launched from this tab. This file is rewritten on every change and is "
        "always current -- trust it over your memory of the conversation (compaction can drop who you launched)."
    ]
    parts.append("\nRUNNING NOW (still working; you will be notified when each finishes -- do not re-launch them):")
    parts += [_line(i, e) for i, e in running.items()] or ["(none)"]
    if lost:
        parts.append(
            "\nLOST IN A RESTART (were running when the app restarted -- they are GONE, nothing will report back. "
            "If the work is still needed, launch it again):"
        )
        parts += [_line(e.get("task_id", "?"), e) for e in lost]
    return "\n".join(parts) + "\n"


def ensure_status_file(workspace_dir: str, tab_id: str) -> str:
    """The path the model is pointed at must exist even before the first agent is ever launched."""
    path = status_file_path(workspace_dir, tab_id)
    if not path.exists():
        with _lock:
            _save(workspace_dir, tab_id, _load(workspace_dir, tab_id))
    return str(path)


def register(workspace_dir: str, tab_id: str, task_id: str, description: str, task_type: str | None, tool_use_id: str | None) -> None:
    with _lock:
        data = _load(workspace_dir, tab_id)
        data["running"][task_id] = {
            "description": description, "task_type": task_type, "tool_use_id": tool_use_id,
            "launched_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        _save(workspace_dir, tab_id, data)
    log_event("engine", "agent_registered", tab_id=tab_id, task_id=task_id, task_type=task_type, description=description[:120])


def finish(workspace_dir: str, tab_id: str, task_id: str, status: str | None = None) -> bool:
    with _lock:
        data = _load(workspace_dir, tab_id)
        entry = data["running"].pop(task_id, None)
        if entry is None:
            return False
        _save(workspace_dir, tab_id, data)
    log_event("engine", "agent_finished", tab_id=tab_id, task_id=task_id, status=status)
    return True


def running(workspace_dir: str, tab_id: str) -> dict[str, dict[str, Any]]:
    return _load(workspace_dir, tab_id)["running"]


def mark_running_as_lost(workspace_dir: str, tab_id: str) -> list[dict[str, Any]]:
    """Called when a FRESH CLI process starts for this tab: every agent still listed as
    running belonged to a CLI process that is gone. Moves them to `lost` and returns them
    (each with its task_id filled in) so the caller can tell the model."""
    with _lock:
        data = _load(workspace_dir, tab_id)
        newly = [{"task_id": i, **e} for i, e in data["running"].items()]
        if not newly:
            return []
        data["running"] = {}
        data["lost"] = (data["lost"] + newly)[-20:]
        _save(workspace_dir, tab_id, data)
    log_event("engine", "agents_lost_in_restart", tab_id=tab_id, count=len(newly), task_ids=[e["task_id"] for e in newly])
    return newly


def clear_lost(workspace_dir: str, tab_id: str) -> None:
    with _lock:
        data = _load(workspace_dir, tab_id)
        if data["lost"]:
            data["lost"] = []
            _save(workspace_dir, tab_id, data)
