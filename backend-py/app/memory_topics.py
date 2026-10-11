"""memory_topics -- the local short-term copy of topic memory.

Topic memory itself lives on the server (the microagent memory behind
save_info/request_info, see docs/MICROAGENTS_PLAN.md section 2.6). What is
kept here is only what the system prompt needs: the short description of
every topic discussed lately and when it was last touched. The prompt shows
them in two groups -- the last 24 hours and the last week; anything older is
left out of the prompt entirely and is reached through request_info.

The copy is filled by code, never by the model: every save_info reply carries
the updated topic (update_topic), and memory_plugin.py refreshes the whole
list from the server when the copy is stale (replace_topics). Which group a
topic falls in is computed here from its last-episode time at the moment the
prompt is built, so a topic ages from "today" into "this week" and then out
of the prompt without any refresh.

Storage: <workspace_dir>/memory_topics.json, same local-JSON-in-workspace
shape as owner_profile.py / working_memory.py.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.logging_setup import log_event

TODAY_S = 24 * 3600
WEEK_S = 7 * 24 * 3600
STALE_AFTER_S = 15 * 60          # the copy is refreshed from the server when older than this
MAX_LISTED_PER_GROUP = 30        # newest first; the rest of a group is left out of the prompt
MAX_DESCRIPTION_CHARS = 160


def _path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "memory_topics.json"


def _load(workspace_dir: str) -> dict[str, Any]:
    try:
        stored = json.loads(_path(workspace_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"fetched_at": 0.0, "topics": {}}
    if not isinstance(stored, dict) or not isinstance(stored.get("topics"), dict):
        return {"fetched_at": 0.0, "topics": {}}
    return stored


def _save(workspace_dir: str, data: dict[str, Any]) -> None:
    try:
        path = _path(workspace_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as exc:
        log_event("memory_topics", "save_failed", error=str(exc))


def _entry(topic: dict[str, Any]) -> dict[str, Any]:
    return {key: topic.get(key) for key in ("id", "parent", "description", "last_episode_at")}


def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return 0.0


def is_stale(workspace_dir: str, now: float | None = None) -> bool:
    return (now if now is not None else time.time()) - float(_load(workspace_dir).get("fetched_at") or 0.0) > STALE_AFTER_S


def update_topic(workspace_dir: str, topic: dict[str, Any]) -> None:
    """One topic as a save_info reply returned it."""
    if not isinstance(topic, dict) or not topic.get("id"):
        return
    data = _load(workspace_dir)
    data["topics"][topic["id"]] = _entry(topic)
    _save(workspace_dir, data)


def replace_topics(workspace_dir: str, tiers: dict[str, Any], now: float | None = None) -> None:
    """The server's own listing ({"today": [...], "week": [...]}) replaces the copy."""
    topics = {}
    for entries in (tiers or {}).values():
        for topic in entries or []:
            if isinstance(topic, dict) and topic.get("id"):
                topics[topic["id"]] = _entry(topic)
    _save(workspace_dir, {"fetched_at": now if now is not None else time.time(), "topics": topics})


def recent_topics(workspace_dir: str, now: float | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(discussed in the last 24 hours, discussed in the last week but not today), newest first."""
    moment = now if now is not None else time.time()
    today, week = [], []
    for topic in _load(workspace_dir)["topics"].values():
        age = moment - _epoch(topic.get("last_episode_at"))
        if age <= TODAY_S:
            today.append(topic)
        elif age <= WEEK_S:
            week.append(topic)
    newest_first = lambda topics: sorted(topics, key=lambda t: t.get("last_episode_at") or "", reverse=True)[:MAX_LISTED_PER_GROUP]
    return newest_first(today), newest_first(week)


def memory_topics_system_prompt_clause(workspace_dir: str, now: float | None = None) -> str:
    """Empty when nothing was discussed in the last week."""
    today, week = recent_topics(workspace_dir, now)
    if not today and not week:
        return ""
    line = lambda topic: f"- {(topic.get('description') or '').strip()[:MAX_DESCRIPTION_CHARS]}"
    parts = [
        "Topics you and the user have been discussing lately, from your topic memory (short descriptions only -- "
        "call request_info for where a topic stands, what was decided, what is still open, and its letters and "
        "documents; topics older than a week are not listed here but are still found by request_info):"
    ]
    if today:
        parts.append("In the last 24 hours:\n" + "\n".join(line(topic) for topic in today))
    if week:
        parts.append("Earlier this week:\n" + "\n".join(line(topic) for topic in week))
    return "\n".join(parts)
