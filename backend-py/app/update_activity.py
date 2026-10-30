"""Is Caroline in use? The one answer every platform's updater asks before it installs an update (docs/UPDATE_EVERYWHERE_PLAN.md, "The rule").

    GET /api/update/activity  ->  {"busy": bool, "reasons": [...], "idle_s": int | null}

Inactive: the update installs by itself. In use: the owner is asked, and it installs by itself at the first inactive moment. The rule lives here, once,
so that Windows, Linux, the Xopek desktops and OS Beroun cannot drift apart.

In use means any of:
  * a turn of a conversation is running in any session (a person's chat, the voice, an Agent Bus chain, a background agent's);
  * a background task or agent of a session is still running;
  * the continuous voice dialogue is on and the owner has been active in the last 30 minutes (the switch alone can stay on for weeks);
  * the owner sent something in the last 10 minutes (headless sessions have no owner and do not count).
"""

from __future__ import annotations

import time
from typing import Any, Iterable

IDLE_AFTER_S = 600
DIALOGUE_IDLE_AFTER_S = 1800


def snapshot(sessions: Iterable[Any], workspace_dir: str, dialogue_on: bool, now: float | None = None,
             running_tasks=None) -> dict[str, Any]:
    """`sessions`: ChatSession-like objects (tab_id, ended, headless, turn_pending, last_user_activity as time.monotonic()).
    `running_tasks(workspace_dir, tab_id)` -> dict of running tasks (agent_registry.running)."""
    if running_tasks is None:
        from app.agent_registry import running as running_tasks
    clock = time.monotonic() if now is None else now
    reasons: list[str] = []
    idle: float | None = None
    for session in sessions:
        if getattr(session, "ended", False):
            continue
        if getattr(session, "turn_pending", False):
            reasons.append(f"turn:{session.tab_id}")
        try:
            tasks = running_tasks(workspace_dir, session.tab_id)
        except Exception:  # noqa: BLE001 -- an unreadable task file must not hold an update back forever
            tasks = {}
        if tasks:
            reasons.append(f"task:{session.tab_id}:{len(tasks)}")
        if not getattr(session, "headless", False):
            since = max(0.0, clock - session.last_user_activity)
            idle = since if idle is None else min(idle, since)
    if dialogue_on and idle is not None and idle < DIALOGUE_IDLE_AFTER_S:
        reasons.append("dialogue")
    if idle is not None and idle < IDLE_AFTER_S:
        reasons.append("user")
    return {"busy": bool(reasons), "reasons": reasons, "idle_s": None if idle is None else int(idle)}
