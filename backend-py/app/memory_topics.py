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

Per explicit instruction (2026-10-13), a real incident: track memory's own
round trip (memory:request) is slow enough server-side that nearly half of
all request_info calls (confirmed live: 31 of ~65) missed their own 30s soft
timeout, with the rest trickling in late and disconnected from the turn that
asked -- track memory wasn't actually answering "where does this track
stand" reliably. A SECOND top-level key, "details", now also caches each
topic's FULL object (summary, results, open questions, its materials list --
whatever the server actually returns, kept opaque/as-is here) the moment it's
seen, not just the slim prompt subset -- see cached_topic_detail/
topic_detail_age_s and memory_plugin.py's request_info(topic=<id>) fast path,
which answers from this cache instantly instead of re-querying the server.

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
STALE_AFTER_S = 15 * 60          # the slim prompt copy is refreshed from the server when older than this
# Per explicit instruction (2026-10-13): distinct from STALE_AFTER_S above --
# that one governs the slim prompt copy (replace_topics' own wholesale
# refresh); this one governs the FULL per-topic "details" cache the fast path
# and the background prefetch loop use. A topic's full detail is already
# refreshed for free by cached_at on every ordinary save/query that touches
# it (see memory_plugin.py's _cache_topics_from_parts/update_topic call
# sites) -- a short threshold here would mostly just re-spend money
# re-fetching topics nothing happened to. 6 hours is long enough to avoid
# pointless re-fetches within the same "today" (24h) window a topic is even
# relevant in, short enough that a topic updated from a DIFFERENT tab (topic
# memory is shared process-wide, not per-tab) still gets refreshed well
# within that window. A cost/freshness knob, not derived from any server
# contract -- shorten it if live use shows topics going stale faster than
# this in practice (e.g. several tabs actively working the same track).
DETAIL_STALE_AFTER_S = 6 * 3600
MAX_LISTED_PER_GROUP = 30        # newest first; the rest of a group is left out of the prompt
MAX_DESCRIPTION_CHARS = 160


def _path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "memory_topics.json"


def _load(workspace_dir: str) -> dict[str, Any]:
    try:
        stored = json.loads(_path(workspace_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"fetched_at": 0.0, "topics": {}, "details": {}, "current_by_tab": {}}
    if not isinstance(stored, dict) or not isinstance(stored.get("topics"), dict):
        return {"fetched_at": 0.0, "topics": {}, "details": {}, "current_by_tab": {}}
    if not isinstance(stored.get("details"), dict):
        stored["details"] = {}
    if not isinstance(stored.get("current_by_tab"), dict):
        stored["current_by_tab"] = {}
    return stored


def _save(workspace_dir: str, data: dict[str, Any]) -> None:
    try:
        path = _path(workspace_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as exc:
        log_event("memory_topics", "save_failed", error=str(exc))


def _entry(topic: dict[str, Any]) -> dict[str, Any]:
    return {key: topic.get(key) for key in ("id", "parent", "description", "status", "last_episode_at")}


def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return 0.0


def is_stale(workspace_dir: str, now: float | None = None) -> bool:
    return (now if now is not None else time.time()) - float(_load(workspace_dir).get("fetched_at") or 0.0) > STALE_AFTER_S


def update_topic(workspace_dir: str, topic: dict[str, Any], now: float | None = None) -> None:
    """One topic as a save_info reply returned it. Per explicit instruction
    (2026-10-13): this already receives the FULL topic object (the slim
    _entry() below just used to throw most of it away) -- also cache it as-is
    under "details", keyed by id, with a cached_at timestamp. Every caller of
    this function (today: only _save_in_background's own topics-result
    handling) now warms the full-detail cache for free, no new call site
    needed."""
    if not isinstance(topic, dict) or not topic.get("id"):
        return
    data = _load(workspace_dir)
    data["topics"][topic["id"]] = _entry(topic)
    data["details"][topic["id"]] = {"topic": topic, "cached_at": now if now is not None else time.time()}
    _save(workspace_dir, data)


def replace_topics(workspace_dir: str, tiers: dict[str, Any], now: float | None = None) -> None:
    """The server's own listing ({"today": [...], "week": [...]}) replaces the
    slim copy. Bug fix (2026-10-13), confirmed live: this used to write a
    bare {"fetched_at", "topics"} dict, which -- once "details" became a real
    key written elsewhere (update_topic above) -- silently wiped the ENTIRE
    full-detail cache on every slim-list refresh (at least every
    STALE_AFTER_S). Loads first and carries "details" forward unchanged --
    this path never has anything to add to it (memory:topics' own tiered
    listing is confirmed slim-only, per docs/MICROAGENTS_PLAN.md section
    2.6: no summary/results here)."""
    topics = {}
    for entries in (tiers or {}).values():
        for topic in entries or []:
            if isinstance(topic, dict) and topic.get("id"):
                topics[topic["id"]] = _entry(topic)
    details = _load(workspace_dir).get("details") or {}
    _save(workspace_dir, {"fetched_at": now if now is not None else time.time(), "topics": topics, "details": details})


def set_current_topic(workspace_dir: str, tab_id: str, topic_id: str) -> None:
    """Per explicit instruction (2026-10-15), after a real incident (the
    "Tunnel Cat" tab lost context mid-conversation and, instead of checking
    its own track memory, told the user it had no record at all): the track
    a save_info call just wrote to becomes this TAB's "current" track --
    tracks themselves are a per-USER concept server-side (shared across
    every tab), but which one a given tab is actively working on right now
    is purely a client-side notion, so it is kept here, not on the server.
    Only save_info (an active write) sets this -- a read (request_info)
    might just be looking something up, not "now working on this" -- see
    memory_topics_system_prompt_clause's own use of it below."""
    if not tab_id or not topic_id:
        return
    data = _load(workspace_dir)
    data["current_by_tab"][tab_id] = topic_id
    _save(workspace_dir, data)


def get_current_topic(workspace_dir: str, tab_id: str | None) -> str | None:
    if not tab_id:
        return None
    return (_load(workspace_dir).get("current_by_tab") or {}).get(tab_id)


def cached_topic_detail(workspace_dir: str, topic_id: str) -> dict[str, Any] | None:
    """{"topic": <the server's full object, opaque>, "cached_at": epoch_s},
    or None if this topic has never been cached. See update_topic/
    memory_plugin.py's request_info(topic=<id>) fast path."""
    entry = (_load(workspace_dir).get("details") or {}).get(topic_id)
    return entry if isinstance(entry, dict) else None


def topic_detail_age_s(workspace_dir: str, topic_id: str, now: float | None = None) -> float | None:
    """Seconds since this topic's full detail was last cached, or None if
    it was never cached at all."""
    entry = cached_topic_detail(workspace_dir, topic_id)
    if entry is None:
        return None
    return (now if now is not None else time.time()) - float(entry.get("cached_at") or 0.0)


def topic_description(workspace_dir: str, topic_id: str) -> str | None:
    """This topic's short slim description, if known -- used as the query
    text for the cold-fallback fetch (memory_plugin.py's
    _fetch_and_cache_topic) when nothing is cached yet for this id."""
    entry = (_load(workspace_dir).get("topics") or {}).get(topic_id)
    return (entry or {}).get("description") if isinstance(entry, dict) else None


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


def memory_topics_system_prompt_clause(workspace_dir: str, tab_id: str | None = None, now: float | None = None) -> str:
    """Empty when nothing was discussed in the last week and this tab has no
    current track. Per explicit instruction (2026-10-15): marks each closed
    topic and names this tab's current one (see set_current_topic), and
    tells the model explicitly to check both -- plus dialogue history --
    before ever claiming it has no record of something, instead of just
    listing topics and leaving that judgment call to chance.

    Bug fix (2026-10-19), confirmed live, per explicit instruction ("Для
    завершенных треков я уже просил заводить БЕТОННЫЙ флаг 'завершено' без
    возможности переоткрытия без явного запроса пользователя"): [closed]
    used to be purely informational (TopicMemory.py's own "Status is
    informational, not a filter" design, carried into close_track's own
    tool description verbatim: "Closing is informational only"). Confirmed
    live this let a fully-finished, explicitly-closed track get silently
    redone from scratch: the user said a generic "look at my previous
    message" twice (each after a Stop) and both times Caroline re-ran an
    already-completed multi-step research task end to end -- duplicate web
    searches, duplicate notes, duplicate save_info calls -- because nothing
    treated [closed] as anything stronger than a label. [closed] is now a
    hard gate on top of being informational: the instruction below tells
    the model not to act on one without the user's own explicit say-so."""
    today, week = recent_topics(workspace_dir, now)
    current_id = get_current_topic(workspace_dir, tab_id)
    if not today and not week and not current_id:
        return ""
    def line(topic: dict[str, Any]) -> str:
        text = f"- {(topic.get('description') or '').strip()[:MAX_DESCRIPTION_CHARS]}"
        if topic.get("id") == current_id:
            text += "  [CURRENT TRACK of this tab]"
        if topic.get("status") == "closed":
            text += "  [closed]"
        return text
    parts = [
        "Topics you and the user have been discussing lately, from your topic memory (short descriptions only -- "
        "call request_info for where a topic stands, what was decided, what is still open, and its letters and "
        "documents; topics older than a week are not listed here but are still found by request_info). Before ever "
        "telling the user you have no record of something, or that context was lost, check: this tab's current "
        "track if one is marked below, any track here not marked [closed], and your recent dialogue history -- in "
        "that order -- not just your own immediate memory of the conversation.\n"
        "[closed] IS A HARD STOP, not just a label: that track is finished, read-only. A generic cue to revisit "
        "old ground -- 'look at my previous message', a stale item surfacing from your recent dialogue history, "
        "your own initiative, anything that is not the user specifically, directly asking to return to THIS "
        "topic by name or clear description -- must NEVER cause you to redo, continue, or re-research work on a "
        "[closed] track. If something you are about to do matches a [closed] track, stop, tell the user in one "
        "short sentence that it is already done (call request_info(topic=<id>) first if you need to confirm "
        "what was actually finished), and ask whether they want anything NEW on top of it -- never silently "
        "restart the work. Only the user's own explicit request to reopen that specific topic lifts this; once "
        "they do, acting on it (save_info) reopens the track automatically, same as always.\n"
        "NEVER BLEND UNRELATED TRACKS INTO ONE REPLY. Confirmed live, a real incident: a Facebook Messenger "
        "setup was still waiting on the user's own answer, an unrelated incoming Telegram message about a "
        "completely different matter (an Apple/App Store DUNS number) arrived in the same stretch, and the "
        "reply that went out wove the DUNS detail into a status update ABOUT Messenger, as if it were part of "
        "that same task -- it was not, the two were only adjacent in time, never actually related. When you "
        "are about to mention more than one pending matter in the same reply (an open question on one track, "
        "a proactive note that landed on a different one, anything not the SAME topic), report each as its "
        "own separate item, in its own sentence -- never attribute a fact, detail, or requirement that "
        "belongs to one track to a different one just because they happen to be mentioned together. If you "
        "are not sure two things are actually the same topic, treat them as different ones."
    ]
    if today:
        parts.append("In the last 24 hours:\n" + "\n".join(line(topic) for topic in today))
    if week:
        parts.append("Earlier this week:\n" + "\n".join(line(topic) for topic in week))
    if current_id and not any(topic.get("id") == current_id for topic in today + week):
        detail = cached_topic_detail(workspace_dir, current_id)
        description = ((detail or {}).get("topic") or {}).get("description") or topic_description(workspace_dir, current_id) or current_id
        parts.append(f"This tab's current track (older than a week, so not listed above):\n- {description}  [CURRENT TRACK of this tab]")
    return "\n".join(parts)
