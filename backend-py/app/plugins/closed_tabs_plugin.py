"""list_closed_tabs / read_closed_tab: the conversations of tabs the user has closed (app/closed_tabs.py).
Written 2026-11-24, explicit instruction: a closed tab's content is kept and available on request."""

from __future__ import annotations

from typing import Any

from app import closed_tabs
from app.plugins.loader import Plugin, PluginTool
from app.workspace_dir import WORKSPACE_DIR

MAX_OUTPUT_CHARS = 24_000


def _line(meta: dict[str, Any]) -> str:
    period = f"{meta.get('firstMessageAt') or '?'} .. {meta.get('lastMessageAt') or '?'}"
    first = meta.get("firstUserMessage") or ""
    return (
        f"- id {meta.get('id')}: tab {meta.get('tabId')} \"{meta.get('name')}\", closed {meta.get('closedAt')}, "
        f"{meta.get('messages', 0)} messages ({period})" + (f"; began with: {first}" if first else "")
    )


async def list_closed_tabs(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    archives = closed_tabs.list_archives(WORKSPACE_DIR)
    contains = (args.get("contains") or "").strip().lower()
    if contains:
        archives = [
            m for m in archives
            if contains in (m.get("name") or "").lower() or contains in (m.get("firstUserMessage") or "").lower()
            or _transcript_contains(m.get("id") or "", contains)
        ]
    if not archives:
        return {"text": "No closed tabs" + (f" mention \"{args.get('contains')}\"." if contains else " are archived.")}
    limit = int(args.get("limit") or 30)
    shown = archives[:limit]
    more = f"\n({len(archives) - len(shown)} older not shown; raise limit.)" if len(archives) > len(shown) else ""
    return {"text": "Closed tabs, newest first:\n" + "\n".join(_line(m) for m in shown) + more}


def _transcript_contains(archive_id: str, needle: str) -> bool:
    folder = closed_tabs.archive_folder(WORKSPACE_DIR, archive_id)
    if folder is None:
        return False
    try:
        return needle in (folder / closed_tabs.TRANSCRIPT_FILE).read_text(encoding="utf-8").lower()
    except OSError:
        return False


async def read_closed_tab(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    archive_id = (args.get("id") or "").strip()
    if not archive_id and args.get("tab") not in (None, ""):
        tab = str(args.get("tab"))
        archive_id = next((m.get("id") for m in closed_tabs.list_archives(WORKSPACE_DIR) if str(m.get("tabId")) == tab), "") or ""
    if not archive_id:
        return {"text": "Give id (from list_closed_tabs) or tab (the number of a closed tab: its latest archive is read).", "is_error": True}
    folder = closed_tabs.archive_folder(WORKSPACE_DIR, archive_id)
    if folder is None:
        return {"text": f"No closed tab with id {archive_id}; list_closed_tabs gives the ids.", "is_error": True}
    text = (folder / closed_tabs.TRANSCRIPT_FILE).read_text(encoding="utf-8")
    contains = (args.get("contains") or "").strip()
    if contains:
        blocks = text.split("\n## ")
        head, hits = blocks[0], [b for b in blocks[1:] if contains.lower() in b.lower()]
        text = head + ("".join("\n## " + b for b in hits) if hits else f"\nNo message mentions \"{contains}\".\n")
    offset = max(0, int(args.get("offset") or 0))
    part = text[offset:offset + MAX_OUTPUT_CHARS]
    tail = ""
    if offset + MAX_OUTPUT_CHARS < len(text):
        tail = f"\n\n[{len(text) - offset - MAX_OUTPUT_CHARS} more characters: call again with offset {offset + MAX_OUTPUT_CHARS}]"
    return {"text": part + tail + f"\n\n(Raw files of this tab: {folder / 'raw'})"}


PLUGIN = Plugin(
    name="closed_tabs",
    description="The conversations of chat tabs the user has closed",
    tools=[
        PluginTool(
            name="list_closed_tabs",
            description=(
                "Lists the chat tabs the user has closed, newest first: id, tab number, name, when it was closed, how "
                "many messages, the period, and the first request. A closed tab's conversation is kept here; a new tab, "
                "even with the same number, starts empty. Use it when the user asks about a conversation from a tab "
                "they closed, or about something that is not in this tab. Optional: contains (text to find in the "
                "name or anywhere in the conversation), limit (default 30)."
            ),
            input_schema={
                "type": "object",
                "properties": {"contains": {"type": "string"}, "limit": {"type": "integer"}},
                "required": [],
            },
            handler=list_closed_tabs,
        ),
        PluginTool(
            name="read_closed_tab",
            description=(
                "Reads the conversation of a closed tab, oldest message first, with times. Give id (from "
                "list_closed_tabs) or tab (a tab number: its most recently closed conversation). Optional: contains "
                "(only the messages with this text), offset (to read on when the text is long)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "tab": {"type": "string"},
                    "contains": {"type": "string"},
                    "offset": {"type": "integer"},
                },
                "required": [],
            },
            handler=read_closed_tab,
        ),
    ],
)
