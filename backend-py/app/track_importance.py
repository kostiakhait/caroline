"""Importance of tracks (topics), 0..1 (2026-11-20, owner's spec).

Set when a track is created (by the router's estimate); raised by every mention of the track in the owner's messages
and, strongly, by his explicit instructions ("это важно", "не забудь", an order about the track); lowered slowly with
time when he does not mention it. More important tracks get fuller coverage in replies and reports, and their reminders
go first. Kept locally (the server's topic memory knows nothing of it): <workspace>/track-importance.json.

Plain code: the router (app/microagents/track_route.py) only judges which track a message belongs to and whether the
message is a plain mention or an explicit instruction; the arithmetic is here."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

DEFAULT_INITIAL = 0.5
MENTION_GAIN = 0.15          # v += (1 - v) * gain, per mention
EXPLICIT_FLOOR = 0.9         # an explicit instruction lifts the track to at least this
HALF_LIFE_S = 7 * 24 * 3600  # without mentions the importance halves every week ...
FLOOR = 0.05                 # ... but never below this: an old open track is still a track

DETAIL_HIGH, DETAIL_LOW = 0.7, 0.35


def _path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "track-importance.json"


def _load(workspace_dir: str) -> dict[str, Any]:
    try:
        data = json.loads(_path(workspace_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save(workspace_dir: str, data: dict[str, Any]) -> None:
    try:
        _path(workspace_dir).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    except OSError:
        pass


def decayed(value: float, since_s: float) -> float:
    """`value` after `since_s` seconds without a mention: halves per HALF_LIFE_S toward FLOOR."""
    if since_s <= 0 or value <= FLOOR:
        return value
    return max(FLOOR, FLOOR + (value - FLOOR) * 0.5 ** (since_s / HALF_LIFE_S))


def clamp(value: Any, default: float = DEFAULT_INITIAL) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def get(workspace_dir: str, topic_id: str | None, now: float | None = None) -> float | None:
    """The track's importance now (decay applied), None when it was never rated."""
    entry = _load(workspace_dir).get(topic_id or "")
    if not isinstance(entry, dict):
        return None
    moment = now if now is not None else time.time()
    at = entry.get("at")
    return decayed(clamp(entry.get("value")), moment - (moment if at is None else float(at)))


def set_initial(workspace_dir: str, topic_id: str, value: Any, now: float | None = None) -> float:
    """A new track's first rating; an existing rating is kept."""
    existing = get(workspace_dir, topic_id, now)
    if existing is not None:
        return existing
    rated = clamp(value)
    data = _load(workspace_dir)
    data[topic_id] = {"value": rated, "at": now if now is not None else time.time()}
    _save(workspace_dir, data)
    return rated


def bump(workspace_dir: str, topic_id: str, explicit: bool, now: float | None = None) -> float:
    """The owner mentioned the track (explicit: he gave an instruction about it). Returns the new importance."""
    moment = now if now is not None else time.time()
    value = get(workspace_dir, topic_id, moment)
    value = DEFAULT_INITIAL if value is None else value
    value = value + (1.0 - value) * MENTION_GAIN
    if explicit:
        value = max(value, EXPLICIT_FLOOR)
    value = min(1.0, value)
    data = _load(workspace_dir)
    data[topic_id] = {"value": value, "at": moment}
    _save(workspace_dir, data)
    return value


def detail_level(value: float | None) -> str:
    """How fully a reply or report covers the track."""
    if value is None or value >= DETAIL_LOW and value < DETAIL_HIGH:
        return "normal"
    return "full" if value >= DETAIL_HIGH else "brief"


# A new matter's first rating, waiting for the track the save_info call will create for it (per tab).
pending_new: dict[str, float] = {}
