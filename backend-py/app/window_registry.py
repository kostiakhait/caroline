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

_open_windows: dict[str, dict[str, Any]] = {}


def register_window(key: str, *, kind: str, label: str, purpose: str, tab_id: str | None) -> None:
    _open_windows[key] = {
        "kind": kind,
        "label": label,
        "purpose": purpose,
        "tab_id": tab_id,
        "opened_at": time.time(),
    }


def unregister_window(key: str) -> None:
    _open_windows.pop(key, None)


def list_windows(tab_id: str | None = None) -> list[dict[str, Any]]:
    entries = list(_open_windows.values())
    if tab_id is not None:
        entries = [e for e in entries if e.get("tab_id") == tab_id]
    return sorted(entries, key=lambda e: e["opened_at"])
