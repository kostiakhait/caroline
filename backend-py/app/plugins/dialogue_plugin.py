"""get_dialogue_transcript: the exact record of what was said between the user and Caroline, as the user saw it
(app/dialogue_log.py). Written 2026-10-26, explicit instruction: the model's own memory of the dialogue (the CLI
transcript) is what it SAID, not what reached the user -- filters sit between the two."""

from __future__ import annotations

from typing import Any

from app import dialogue_log
from app.plugins.loader import Plugin, PluginTool
from app.session_context import get_tab_id
from app.workspace_dir import WORKSPACE_DIR


async def get_dialogue_transcript(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    tab_id = get_tab_id() or "1"
    since = args.get("since_minutes")
    entries = dialogue_log.select(
        dialogue_log.read_entries(WORKSPACE_DIR, tab_id),
        limit=args.get("limit") or dialogue_log.DEFAULT_LIMIT,
        since_minutes=float(since) if since not in (None, "") else None,
        contains=args.get("contains"),
        include_withheld=args.get("include_withheld") is not False,
    )
    return {"text": dialogue_log.render(entries)}


PLUGIN = Plugin(
    name="dialogue",
    description="The exact transcript of the dialogue with the user, as they saw it",
    always_loaded=True,
    tools=[
        PluginTool(
            name="get_dialogue_transcript",
            description=(
                "Returns the EXACT transcript of this tab's dialogue with the user, with timestamps: what the user wrote, and "
                "what really reached their screen from you (after review and translation), message by message, complete. "
                "Messages you wrote that a filter kept back are marked WITHHELD -- the user never saw those. Use it when it "
                "matters what the user was actually told or asked: to check whether a result was reported, before saying "
                "\"I already told you\", when the user says they did not get something, or when your own memory of the "
                "conversation and theirs differ. Your own transcript is not a substitute: it holds what you said, not what "
                "was shown. All arguments are optional: limit (last N messages, default 30), since_minutes, contains "
                "(case-insensitive text to find), include_withheld (default true; false hides what was never shown)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "since_minutes": {"type": "number"},
                    "contains": {"type": "string"},
                    "include_withheld": {"type": "boolean"},
                },
                "required": [],
            },
            handler=get_dialogue_transcript,
        )
    ],
)
