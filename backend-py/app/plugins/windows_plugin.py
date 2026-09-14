"""windows -- cross-plugin registry of Caroline's own currently-open
windows (embedded browser tabs + viewer/editor windows), each with the
reason she opened it. See app/window_registry.py's own docstring for why
this exists and its limits (viewer-window tracking is best-effort;
browser-window tracking is reconciled below against AppBrowserHost's real
live list, the one case where an independent ground truth exists)."""

from __future__ import annotations

import json
from typing import Any

from app.plugins.app_browser_plugin import list_app_browsers
from app.plugins.loader import Plugin, PluginTool
from app.session_context import get_tab_id
from app.window_registry import list_windows


async def list_my_windows(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    tracked = list_windows(get_tab_id())
    live_labels: set[str] | None
    try:
        raw = json.loads((await list_app_browsers({}, None))["text"])
        live_labels = {row["label"] for row in raw if row.get("label")}
    except Exception:
        # AppBrowserHost unreachable -- can't verify either way, so don't
        # drop entries just because we couldn't confirm them this time.
        live_labels = None

    windows = []
    for entry in tracked:
        if entry["kind"] == "browser" and live_labels is not None and entry["label"] not in live_labels:
            continue  # our own bookkeeping is stale -- the host says this one's actually gone.
        windows.append(entry)

    if not windows:
        return {"text": "No windows of your own are currently open."}
    lines = [f"- [{w['kind']}] {w['label']} -- {w['purpose']}" for w in windows]
    return {"text": "Your currently open windows:\n" + "\n".join(lines)}


PLUGIN = Plugin(
    name="windows",
    tools=[
        PluginTool(
            "list_my_windows",
            "List every window you (Caroline) currently have open -- embedded browser tabs (open_app_browser) "
            "and viewer/editor windows (open_in_viewer) -- each with the reason you opened it. Call this before "
            "telling the user you have no windows open, or before trying to close 'the rest' of your windows, "
            "instead of guessing from what you remember doing earlier in the conversation.",
            {}, list_my_windows,
        ),
    ],
)
