"""app/evolution_journal.py -- a small, Caroline-only record of which
tournament rounds (app/evolution_client.py's run_reply_agent) were run in
service of which open action or active track.

Per explicit special order (2026-10-19): two new background microagents need
this. Agent 1 (app/microagents/reaction_score.py) scores the user's
emotional reaction to something Caroline did and attributes it to a
specific action/track; Agent 2 (app/microagents/evolution_backprop.py) then
needs to find the recent evolution.selector verdicts that were actually
judging work done for that same action/track, to reward or penalize them.

Camerlengo itself has no such link anywhere (microagent_tournaments only
ever stores aggregate champion_wins/challenger_wins/ties counters, never a
per-round row -- confirmed by reading Api2EvolutionStore.py) and, per the
"Camerlengo is a universal server" boundary, this correlation is Caroline-
specific and does not belong there. The client is the only place that ever
sees a full round's detail in the first place (role, tournament id, the
winner, evolution.selector's own stated reason) -- see run_reply_agent's own
tournament-resolution branch, the only writer here.

Storage: <workspace_dir>/evolution-rounds.jsonl, one JSON object per line,
trimmed to the most recent MAX_ENTRIES on every write -- same bounded-size
philosophy as action_registry.py's own MAX_OPEN_ACTIONS_PER_TAB, just time-
ordered instead of LRU. Process-wide (not per-tab): a tournament round is a
property of (role, scope), not of any one tab, and the two new agents need
to find rounds regardless of which tab originally triggered the call."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.logging_setup import log_event

MAX_ENTRIES = 200


def _path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "evolution-rounds.jsonl"


def append(
    workspace_dir: str, *, role: str, api_path: str, tournament_id: int, winner: str,
    verdict_reason: str | None, action_id: str | None, track_id: str | None,
) -> None:
    """One journal entry per tournament round actually resolved by this
    client (see run_reply_agent's own "mode == tournament" branch -- the
    only caller). Best-effort: a failure here never affects the real call
    whose result is already decided by the time this runs."""
    entry = {
        "ts": time.time(), "role": role, "api_path": api_path, "tournament_id": tournament_id,
        "winner": winner, "verdict_reason": verdict_reason, "action_id": action_id, "track_id": track_id,
    }
    try:
        path = _path(workspace_dir)
        lines: list[str] = []
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
        lines.append(json.dumps(entry, ensure_ascii=False, default=str))
        lines = lines[-MAX_ENTRIES:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:
        log_event("engine", "evolution_journal_append_failed", error=str(exc))


def recent_rounds_for(
    workspace_dir: str, *, action_ids: list[str] | None = None, track_id: str | None = None, limit: int = 10,
) -> list[dict[str, Any]]:
    """Newest first, matching any of the given action_ids OR the given
    track_id -- the only read path app/microagents/reaction_score.py and
    evolution_backprop.py's own callers need. Never raises; a missing or
    unreadable file just means "no rounds to attribute yet"."""
    action_id_set = set(action_ids or [])
    path = _path(workspace_dir)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        log_event("engine", "evolution_journal_read_failed", error=str(exc))
        return []
    matched: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue
        if (entry.get("action_id") and entry["action_id"] in action_id_set) or (track_id and entry.get("track_id") == track_id):
            matched.append(entry)
            if len(matched) >= limit:
                break
    return matched
