"""Structured (JSON-lines) logging for the whole backend.

Per the migration plan's explicit logging requirement: every record is one
JSON object per line (timestamp, component, event, plus whatever fields the
caller passes) -- still fine to eyeball with `tail -f`, but actually
greppable/parseable, unlike free-text string interpolation. Written to
stdout so it lands in the SAME combined stream MainWindow.xaml.cs's
BackendProcess already pipes into Logger.Log (and the WPF shell's own C#
logs / the external health-watchdog's logs) -- one correlatable timeline,
matching today's proven pattern, not split into separate files.

Every caller goes through log_event() (engine code) or the plugin
dispatcher's own automatic wrapping (see plugins/loader.py) -- individual
plugins are never expected to add their own ad hoc logging for the
call/result/duration triad, that's a systemic guarantee from this layer.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any


def log_event(component: str, event: str, **fields: Any) -> None:
    """Writes one JSON-lines record to stdout. `component` is e.g.
    "engine", "ws", "http", or "plugin:<name>"; `event` is a short
    snake_case name (e.g. "query_created", "tool_call", "state_transition").
    Extra keyword args become the record's payload -- pass whatever's
    relevant (tab_id, session_id, duration_ms, error, matched_pattern, ...).
    """
    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}",
        "component": component,
        "event": event,
        **fields,
    }
    print(json.dumps(record, ensure_ascii=False, default=str), file=sys.stdout, flush=True)
