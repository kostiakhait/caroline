"""First plugin, deliberately trivial -- Phase 1's whole job is proving a
real chat turn round-trips end-to-end (WS protocol + plugin loading +
mcp tool call) against the real, unmodified chat.js frontend before any
real tool-porting effort begins. Mirrors backend/src's own `time.ts`
MCP server (get_current_time)."""

from __future__ import annotations

import datetime
from typing import Any

from app.plugins.loader import Plugin, PluginTool


async def get_current_time(_args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    now = datetime.datetime.now().astimezone()
    return {"text": now.strftime("%A, %B %d, %Y, %I:%M %p %Z")}


PLUGIN = Plugin(
    name="time",
    tools=[
        PluginTool(
            name="get_current_time",
            description="Returns the current local date and time.",
            input_schema={},
            handler=get_current_time,
        )
    ],
)
