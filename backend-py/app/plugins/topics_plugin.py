"""DEPRECATED and read-only for the model since the microagent memory
(save_info / request_info) -- see app/deprecated_memory.py. What follows
describes the store as it was built; its reading tool still works.

topics -- Caroline's own thematic short-term memory. Per explicit
instruction (2026-09-22), after a real incident ("рыбка Дори" -- losing
track of what's actually going on): up to MAX_TOPICS_PER_TAB "current
topics" per tab, each a short name plus a free-text status she writes and
updates herself. When a genuinely NEW topic would exceed the cap, the
LEAST RECENTLY UPDATED one is evicted -- "least recently updated", not
"oldest by creation": a topic she's actively iterating on is exactly the
one that must NOT fall out just because it happened to be opened first.

Deliberately a TOOL, not automatic injection (unlike chat_session.py's
RECENT_HOUR_INLINE_WINDOW_HOURS, added the same day for a related but
distinct problem) -- see this module's own usage_instructions for why the
instruction wording has to compensate for that with extra insistence
instead.

Storage: workspace/topics-<tabId>.json, one file per tab -- each tab is
its own independent thread of work (a grant application in one, a coding
task in another), and conflating them into one shared list would mean an
unrelated tab's busy afternoon evicts topics this tab still cares about.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.deprecated_memory import READ_PREFIX, USAGE_NOTE, WRITE_PREFIX, read_only_refusal
from app.durability import _sanitize_tab_id
from app.logging_setup import log_event
from app.plugins.loader import Plugin, PluginTool
from app.session_context import get_tab_id
from app.workspace_dir import WORKSPACE_DIR

MAX_TOPICS_PER_TAB = 5


def _topics_path(tab_id: str) -> Path:
    return Path(WORKSPACE_DIR) / f"topics-{_sanitize_tab_id(tab_id)}.json"


def _load_topics(tab_id: str) -> list[dict[str, Any]]:
    path = _topics_path(tab_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        log_event("plugin:topics", "load_failed", tab_id=tab_id, error=str(exc))
        return []


async def topic_upsert(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return read_only_refusal("topic_upsert")


async def topics_list(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    tab_id = get_tab_id() or "default"
    topics = _load_topics(tab_id)
    if not topics:
        return {"text": "No current topics tracked for this tab."}
    ordered = sorted(topics, key=lambda t: t["updated_at_iso"], reverse=True)
    lines = [f'- "{t["name"]}": {t["status"]} (updated {t["updated_at_iso"]})' for t in ordered]
    return {"text": "\n".join(lines)}


async def topic_close(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return read_only_refusal("topic_close")


PLUGIN = Plugin(
    name="topics",
    usage_instructions=USAGE_NOTE,
    tools=[
        PluginTool(
            "topic_upsert",
            WRITE_PREFIX + f"Add a new current topic for this tab, or update an existing one's status (matched by exact "
            f"name). At most {MAX_TOPICS_PER_TAB} topics per tab -- adding a new one beyond that evicts the "
            "least recently updated existing one (never the one you're actively working on, since updating it "
            "refreshes its own recency).",
            {"name": str, "status": str}, topic_upsert,
        ),
        PluginTool(
            "topics_list",
            READ_PREFIX + "Lists this tab's current topics and their statuses, most recently updated first.",
            {}, topics_list,
        ),
        PluginTool(
            "topic_close",
            WRITE_PREFIX + "Explicitly retires a current topic (finished or abandoned) by exact name, before it would "
            "otherwise sit around and eventually get silently evicted.",
            {"name": str}, topic_close,
        ),
    ],
)
