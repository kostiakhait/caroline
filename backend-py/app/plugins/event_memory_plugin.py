"""event_memory -- the tool surface over app/event_memory.py's durable
record of things Caroline herself arranged. See that module's own doc
comment for the full design and why it's separate from working_memory.py's
"events" category.

schedule_reminder (scheduler_plugin.py) already writes here automatically
on every call -- event_memory_remember is only for recording something
Caroline arranged WITHOUT a reminder (e.g. a verbal agreement with no
scheduled alert).
"""

from __future__ import annotations

import json
from typing import Any

from app.event_memory import forget_event, remember_event, search_events
from app.plugins.loader import Plugin, PluginTool
from app.workspace_dir import WORKSPACE_DIR


def _event_dict(event: Any) -> dict[str, Any]:
    return {"id": event.id, "text": event.text, "dueAtIso": event.due_at_iso, "source": event.source}


async def event_memory_recall(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    events = search_events(WORKSPACE_DIR, args.get("query") or "")
    if not events:
        return {"text": "No matching events in event memory."}
    return {"text": json.dumps([_event_dict(e) for e in events], ensure_ascii=False)}


async def event_memory_remember(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    event = remember_event(WORKSPACE_DIR, args["text"], due_at_iso=args.get("due_at_iso"), source="manual")
    return {"text": json.dumps(_event_dict(event), ensure_ascii=False)}


async def event_memory_forget(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    removed = forget_event(WORKSPACE_DIR, args["id"])
    return {"text": "Removed." if removed else f"No event with id {args['id']} -- nothing to remove."}


_USAGE_INSTRUCTIONS = (
    "event_memory holds a durable record of things you yourself arranged -- appointments, meetings, anything "
    "you scheduled a reminder for. schedule_reminder already writes here automatically on every call, so you "
    "don't need to call event_memory_remember for anything that went through it; use event_memory_remember only "
    "for something you arranged WITHOUT a reminder (e.g. a verbal agreement with no alert needed). Call "
    "event_memory_recall (with or without a query) FIRST whenever the user asks about something you yourself "
    "scheduled or agreed to, before searching Notes or re-deriving it from old conversation -- unlike "
    "list_reminders, this still shows entries whose reminder has already fired. Entries stay until well after "
    "their own due date passes, then age out automatically -- this is not a place to store facts unrelated to "
    "a specific date/commitment (use working_memory or Notes for those)."
)


PLUGIN = Plugin(
    name="event_memory",
    usage_instructions=_USAGE_INSTRUCTIONS,
    tools=[
        PluginTool(
            "event_memory_recall",
            "Searches durable event memory (things you yourself arranged -- appointments, meetings) for a "
            "query substring, or lists everything if query is omitted. Unlike list_reminders, still shows "
            "events whose reminder has already fired.",
            {"query": str | None}, event_memory_recall,
        ),
        PluginTool(
            "event_memory_remember",
            "Records something you arranged WITHOUT a reminder (schedule_reminder already does this "
            "automatically for anything scheduled through it).",
            {"text": str, "due_at_iso": str | None}, event_memory_remember,
        ),
        PluginTool(
            "event_memory_forget",
            "Explicitly removes a durable event-memory record by id (e.g. something that turned out to be "
            "wrong or was never actually agreed to).",
            {"id": str}, event_memory_forget,
        ),
    ],
)
