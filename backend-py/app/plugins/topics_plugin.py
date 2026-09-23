"""topics -- Caroline's own thematic short-term memory. Per explicit
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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def _save_topics(tab_id: str, topics: list[dict[str, Any]]) -> None:
    try:
        path = _topics_path(tab_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(topics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as exc:
        log_event("plugin:topics", "save_failed", tab_id=tab_id, error=str(exc))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def topic_upsert(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    tab_id = get_tab_id() or "default"
    name = args["name"].strip()
    status = args["status"].strip()
    topics = _load_topics(tab_id)
    existing = next((t for t in topics if t["name"] == name), None)
    now = _now_iso()
    evicted: str | None = None
    if existing:
        existing["status"] = status
        existing["updated_at_iso"] = now
    else:
        if len(topics) >= MAX_TOPICS_PER_TAB:
            # Least recently UPDATED, not oldest by creation -- see this
            # module's own docstring for why that distinction matters.
            oldest = min(topics, key=lambda t: t["updated_at_iso"])
            topics.remove(oldest)
            evicted = oldest["name"]
        topics.append({"name": name, "status": status, "created_at_iso": now, "updated_at_iso": now})
    _save_topics(tab_id, topics)
    log_event("plugin:topics", "upsert", tab_id=tab_id, name=name, was_new=existing is None, evicted=evicted)
    note = f' (evicted "{evicted}" to make room -- it was the least recently updated of the {MAX_TOPICS_PER_TAB})' if evicted else ""
    return {"text": f'Topic "{name}" {"updated" if existing else "added"}{note}.'}


async def topics_list(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    tab_id = get_tab_id() or "default"
    topics = _load_topics(tab_id)
    if not topics:
        return {"text": "No current topics tracked for this tab."}
    ordered = sorted(topics, key=lambda t: t["updated_at_iso"], reverse=True)
    lines = [f'- "{t["name"]}": {t["status"]} (updated {t["updated_at_iso"]})' for t in ordered]
    return {"text": "\n".join(lines)}


async def topic_close(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    tab_id = get_tab_id() or "default"
    name = args["name"].strip()
    topics = _load_topics(tab_id)
    after = [t for t in topics if t["name"] != name]
    removed = len(after) < len(topics)
    if removed:
        _save_topics(tab_id, after)
    log_event("plugin:topics", "close", tab_id=tab_id, name=name, removed=removed)
    return {"text": f'Closed "{name}".' if removed else f'No current topic named "{name}" (see topics_list).'}


def _usage_instructions() -> str:
    return (
        "This is your OWN thematic short-term memory for THIS tab, up to "
        f"{MAX_TOPICS_PER_TAB} entries -- distinct from any file-based dialogue history: a short name plus a "
        "free-text status YOU write and keep current. Per explicit instruction, after a real incident: you have "
        "repeatedly lost track of what's actually going on mid-conversation, the way someone with no short-term "
        "memory would -- this exists specifically to stop that, but ONLY if you actually use it, since nothing "
        "injects it into your context automatically. Treat maintaining it as a HARD, non-optional habit, not "
        "something to remember only when convenient:\n"
        "- The moment a real, multi-step piece of work becomes 'a thing' (the user asked for something that will "
        "take more than an immediate answer, or you're tracking an ongoing situation for them), call topic_upsert "
        "right away -- don't wait until you're deep into it or about to lose track.\n"
        "- Every time that topic's real state changes in a way that matters (progress made, blocked on something, "
        "waiting for a reply, new information), call topic_upsert again with the updated status -- keep it "
        "current, not a stale snapshot from when you started.\n"
        "- Call topics_list at the start of any turn where you're not sure what's still open, before asking the "
        "user to remind you, and especially right after anything that could have disrupted your own sense of "
        "continuity (a restart, a reconnect, a long gap since the last message).\n"
        "- Call topic_close the moment something is genuinely finished or abandoned -- don't let it sit and "
        "eventually get silently evicted; an explicit close is a real signal, a silent eviction is just you "
        "running out of room.\n"
        "Five slots is not a lot -- if something doesn't deserve one of them anymore, close it."
    )


PLUGIN = Plugin(
    name="topics",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "topic_upsert",
            f"Add a new current topic for this tab, or update an existing one's status (matched by exact "
            f"name). At most {MAX_TOPICS_PER_TAB} topics per tab -- adding a new one beyond that evicts the "
            "least recently updated existing one (never the one you're actively working on, since updating it "
            "refreshes its own recency).",
            {"name": str, "status": str}, topic_upsert,
        ),
        PluginTool(
            "topics_list",
            "Lists this tab's current topics and their statuses, most recently updated first.",
            {}, topics_list,
        ),
        PluginTool(
            "topic_close",
            "Explicitly retires a current topic (finished or abandoned) by exact name, before it would "
            "otherwise sit around and eventually get silently evicted.",
            {"name": str}, topic_close,
        ),
    ],
)
