"""The start rung of a turn, decided by a model, for every engine's ladder
(2026-10-28, explicit instruction; docs/STAKES_AWARE_LADDER_PLAN.md).

advise() runs the turn.rung.advice microagent (app/microagents/turn_rung_advice.py)
on the user's message, the recent dialogue, the user's reaction and this tab's
rung history, and returns the step to start on -- or None when there is no
advice (failure, timeout), and the engine then starts where it always did.
Never raises, never blocks a turn for longer than ADVICE_TIMEOUT_S.

The rung history is what the ladder remembers: per tab, the last HISTORY_KEPT
turns -- where each started, where it ended, why it went up -- written by each
engine when its turn settles, kept in memory and mirrored to
workspace/rung-history-<tab>.json so a restart does not reset it. It is input
for the advice, not a rule."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Awaitable

from app.durability import _sanitize_tab_id
from app.logging_setup import log_event

HISTORY_KEPT = 10
# Same order as small_model_engine.COMPLEXITY_TIMEOUT_S; started alongside the turn's other preparation.
ADVICE_TIMEOUT_S = 12.0
# How long the advice waits for turn.reaction.score of the same message (it is started at submit()).
REACTION_WAIT_S = 4.0
REQUEST_CHARS = 200

# (model, what it is good for), cheapest first -- what the microagent is shown.
CLAUDE_LADDER: tuple[tuple[str, str], ...] = (
    ("haiku", "fast and cheap; small talk, lookups, simple errands and routine tool work"),
    ("sonnet", "strong; most real work, reading documents and correspondence, careful answers"),
    ("opus", "strongest; high-stakes judgement, long documents, legal/medical/financial reasoning"),
)
_SW_GOOD_FOR = (
    "fastest and cheapest; small talk, lookups, one-step errands",
    "cheap; routine multi-step tool work",
    "strong; reading documents and correspondence, careful answers",
    "strongest; high-stakes judgement, long documents, legal/medical/financial reasoning",
)
# Codex runs one model; its ladder is the reasoning effort. Step 0 is what a turn
# runs with today (Codex's own default, "medium"). An effort set on turn/start
# stays for the thread's later turns, so a turn back on step 0 sends it again.
CODEX_EFFORTS: tuple[tuple[str, str], ...] = (
    ("medium", "the default effort; most conversation and routine work"),
    ("high", "more careful reasoning; documents, correspondence, answers the user relies on"),
    ("xhigh", "the most thorough reasoning; high-stakes judgement, legal/medical/financial matters"),
)


def sw_ladder(tiers: list[str]) -> list[tuple[str, str]]:
    return [(model, _SW_GOOD_FOR[min(i, len(_SW_GOOD_FOR) - 1)]) for i, model in enumerate(tiers)]


# --- the rung history ------------------------------------------------------------

_lock = threading.Lock()
_memory: dict[str, list[dict[str, Any]]] = {}


def _path(workspace_dir: str, tab_id: str) -> Path:
    return Path(workspace_dir) / f"rung-history-{_sanitize_tab_id(tab_id)}.json"


def recent(workspace_dir: str, tab_id: str) -> list[dict[str, Any]]:
    key = str(_path(workspace_dir, tab_id))
    with _lock:
        if key not in _memory:
            try:
                loaded = json.loads(Path(key).read_text(encoding="utf-8"))
                _memory[key] = loaded if isinstance(loaded, list) else []
            except (OSError, ValueError):
                _memory[key] = []
        return list(_memory[key])


def record(workspace_dir: str, tab_id: str, engine: str, request: str, ladder: list[tuple[str, str]] | tuple,
           start_rung: int, end_rung: int, escalation_notes: list[str] | None = None) -> None:
    """One settled turn. Never raises -- the history never costs a turn."""
    try:
        names = [m for m, _ in ladder]
        entry = {
            "ts": time.time(), "engine": engine, "request": " ".join((request or "").split())[:REQUEST_CHARS],
            "start_rung": start_rung, "end_rung": end_rung,
            "started": names[start_rung] if 0 <= start_rung < len(names) else str(start_rung),
            "ended": names[end_rung] if 0 <= end_rung < len(names) else str(end_rung),
            "escalation_notes": [str(n)[:300] for n in (escalation_notes or [])][-3:],
        }
        history = recent(workspace_dir, tab_id) + [entry]
        history = history[-HISTORY_KEPT:]
        path = _path(workspace_dir, tab_id)
        with _lock:
            _memory[str(path)] = history
            tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        log_event("engine", "rung_history_recorded", tab_id=tab_id, engine=engine, start=entry["started"], end=entry["ended"])
    except Exception as exc:  # noqa: BLE001
        log_event("engine", "rung_history_record_failed", tab_id=tab_id, engine=engine, error=str(exc)[:200])


def _history_for_prompt(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = time.time()
    out = []
    for h in history:
        minutes = max(0, int((now - float(h.get("ts") or now)) / 60))
        ago = f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"
        out.append({"ago": ago, "engine": h.get("engine"), "request": h.get("request"), "started": h.get("started"),
                    "ended": h.get("ended"), "escalation_notes": h.get("escalation_notes") or []})
    return out


# --- the advice ------------------------------------------------------------------

async def _reaction_of(reaction: Any) -> float | None:
    if reaction is None or isinstance(reaction, (int, float)):
        return None if reaction is None else float(reaction)
    try:
        value = await asyncio.wait_for(asyncio.shield(reaction), timeout=REACTION_WAIT_S)
    except Exception:  # noqa: BLE001 -- not ready in time, or failed: no reaction
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


async def _advise(engine: str, ladder: list[tuple[str, str]] | tuple, request: str, dialogue: str,
                  workspace_dir: str, tab_id: str, reaction: Any) -> dict[str, Any] | None:
    from app.microagents.turn_rung_advice import RUNG_ADVICE
    from app.plugins.voice_api import _run_local_microagent

    agent_input = {
        "request": request[-4000:],
        "dialogue": (dialogue or "")[-5000:],
        "history": _history_for_prompt(recent(workspace_dir, tab_id)),
        "reaction": await _reaction_of(reaction),
        "ladder": [{"model": m, "good_for": g} for m, g in ladder],
    }
    result = await _run_local_microagent(RUNG_ADVICE, "app.microagents.turn_rung_advice", agent_input)
    if not result or result.get("status") != "ok":
        raise RuntimeError(str((result or {}).get("error") or "no local microagent runtime"))
    return {**(result.get("output") or {}), "reaction": agent_input["reaction"]}


async def advise(engine: str, ladder: list[tuple[str, str]] | tuple, request: str, dialogue: str,
                 workspace_dir: str, tab_id: str, reaction: float | Awaitable[Any] | None = None) -> tuple[int | None, str]:
    """(start rung, why), or (None, "") with no advice. Every run is logged, the failures too."""
    started = time.monotonic()
    try:
        output = await asyncio.wait_for(
            _advise(engine, ladder, request, dialogue, workspace_dir, tab_id, reaction), timeout=ADVICE_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 -- no advice: the engine starts where it always did
        log_event("engine", "turn_rung_advice", tab_id=tab_id, engine=engine, start_rung=None, failed=True,
                  error=str(exc)[:200] or type(exc).__name__, ms=int((time.monotonic() - started) * 1000))
        return None, ""
    rung = (output or {}).get("start_rung")
    why = str((output or {}).get("why") or "")
    if not isinstance(rung, int) or isinstance(rung, bool) or not 0 <= rung < len(ladder):
        rung = None
    log_event("engine", "turn_rung_advice", tab_id=tab_id, engine=engine, start_rung=rung,
              model=ladder[rung][0] if rung is not None else None, why=why[:300],
              reaction=(output or {}).get("reaction"), ms=int((time.monotonic() - started) * 1000))
    return rung, why
