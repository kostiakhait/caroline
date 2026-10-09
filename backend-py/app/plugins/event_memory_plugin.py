"""DEPRECATED and read-only for the model since the microagent memory
(save_info / request_info) -- see app/deprecated_memory.py. What follows
describes the store as it was built; its reading tool still works,
and schedule_reminder keeps writing it from code.

event_memory -- the tool surface over app/event_memory.py's durable
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

from app.deprecated_memory import READ_PREFIX, USAGE_NOTE, WRITE_PREFIX, read_only_refusal
from app.event_memory import search_events
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
    return read_only_refusal("event_memory_remember")


async def event_memory_forget(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return read_only_refusal("event_memory_forget")


PLUGIN = Plugin(
    name="event_memory",
    usage_instructions=USAGE_NOTE,
    tools=[
        PluginTool(
            "event_memory_recall",
            READ_PREFIX + "Searches durable event memory (things you yourself arranged -- appointments, meetings) for a "
            "query substring, or lists everything if query is omitted. Unlike list_reminders, still shows "
            "events whose reminder has already fired.",
            {"query": str | None}, event_memory_recall,
        ),
        PluginTool(
            "event_memory_remember",
            WRITE_PREFIX + "Records something you arranged WITHOUT a reminder (schedule_reminder already does this "
            "automatically for anything scheduled through it).",
            {"text": str, "due_at_iso": str | None}, event_memory_remember,
        ),
        PluginTool(
            "event_memory_forget",
            WRITE_PREFIX + "Explicitly removes a durable event-memory record by id (e.g. something that turned out to be "
            "wrong or was never actually agreed to).",
            {"id": str}, event_memory_forget,
        ),
    ],
)
