"""window_registry -- per explicit instruction (2026-09-14): Caroline had no
memory of which of her own windows (embedded browser tabs, viewer/editor
windows) were currently open or why she'd opened each one. Confirmed live as
a real bug: asked to close "the rest" of her windows, she could only check
app_browser windows via list_app_browsers (found none), and had no way to
even know a second, untracked viewer window was still open -- she had
nothing to check it against.

This is the single, cross-plugin, in-memory bookkeeping of "my own open
windows" that open_in_viewer/close_viewer (viewer_plugin.py) and
open_app_browser/close_app_browser (app_browser_plugin.py) both read and
write, so list_my_windows (windows_plugin.py) can answer accurately instead
of the model having to guess or re-derive it from tool-call history it may
not even still have in context.

Best-effort, not authoritative for viewer windows specifically -- there is
no live "is this WPF window still open" query the way AppBrowserHost's own
/list is for app_browser windows (windows_plugin.py reconciles against that
real source for browser entries specifically). A viewer window the user
closes by clicking its own X is only known to us once/if the corresponding
editor_result control op arrives (main.py); a window opened by a backend
version before this registry existed is invisible until the next open/close
of it touches the registry. In-memory only, not persisted across an app
restart -- exactly like the live windows themselves, which don't survive
one either.
"""

from __future__ import annotations

import time
from typing import Any

from app.logging_setup import log_event

_open_windows: dict[str, dict[str, Any]] = {}


def register_window(key: str, *, kind: str, label: str, purpose: str, tab_id: str | None, topic_id: str | None = None) -> None:
    _open_windows[key] = {
        "kind": kind,
        "label": label,
        "purpose": purpose,
        "tab_id": tab_id,
        "topic_id": topic_id,
        "opened_at": time.time(),
    }


def unregister_window(key: str) -> None:
    _open_windows.pop(key, None)


def list_windows(tab_id: str | None = None) -> list[dict[str, Any]]:
    entries = list(_open_windows.values())
    if tab_id is not None:
        entries = [e for e in entries if e.get("tab_id") == tab_id]
    return sorted(entries, key=lambda e: e["opened_at"])


async def close_windows_for_topic(topic_id: str) -> list[str]:
    """Per explicit instruction (2026-10-16): closing a track should close
    the windows that belong to it, not just forget about them -- "belongs
    to" means the tab's CURRENT track (memory_topics.get_current_topic) at
    the moment the window was opened (register_window's own topic_id,
    threaded through from viewer_plugin.py/app_browser_plugin.py). Returns
    the labels actually closed, for the caller (close_track) to report back
    to the model. Deferred imports: viewer_plugin.py and
    app_browser_plugin.py both import register_window/unregister_window
    from this module, so importing their close functions back at module
    level here would be circular."""
    from app.plugins.app_browser_plugin import close_app_browser
    from app.plugins.viewer_plugin import close_viewer

    matches = [(key, entry) for key, entry in _open_windows.items() if entry.get("topic_id") == topic_id]
    closed: list[str] = []
    for key, entry in matches:
        try:
            if entry.get("kind") == "browser":
                await close_app_browser({"label": entry["label"]}, None)
            else:
                await close_viewer({"path": entry["label"]}, None)
            closed.append(entry["label"])
        except Exception as exc:  # noqa: BLE001 -- one window's close failing must not stop the rest
            log_event("window_registry", "close_windows_for_topic_failed", key=key, error=str(exc))
            unregister_window(key)  # best-effort: forget it even if the real close failed
    return closed
