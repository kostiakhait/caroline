"""A small, deliberate, source-agnostic short-term fact cache -- NOT a
transparent cache of any particular API's responses (that's notes_plugin.py's
own read-through cache, a separate and unrelated mechanism). Caroline decides
herself what goes in here (a credential, a contact, a command that worked, a
reference, an event, or anything else worth keeping handy for a while) via
owner_profile_plugin.py's sibling, working_memory_plugin.py -- this module is
only the storage/eviction/rendering layer.

Per explicit instruction (2026-10-04): frequently-needed facts don't only
come from Notes, and Notes may not exist at all for a given install (no SW
account) -- so this has to be its own mechanism, independent of Notes, not
folded into that read-cache. It's auto-injected into every system prompt
(same always-injected-but-must-stay-tiny shape as app/owner_profile.py), which
is exactly why real eviction matters here: an unbounded cache would pollute
every single prompt, not just the odd tool call.

Same local-JSON-in-workspace-dir pattern as persona.py/owner_profile.py.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

CATEGORIES = ("credentials", "contacts", "commands", "references", "events", "facts")

# Per-category breadth cap and a global render-size cap -- see this module's
# own doc comment for why both exist: the per-category cap bounds how many
# DIFFERENT things pile up, the char cap bounds the actual prompt-token cost
# regardless of how many short vs. long entries that breaks down into. 2000
# chars mirrors owner_profile.py's own "deliberately tiny, not the
# --append-system-prompt overflow incident again" reasoning.
MAX_PER_CATEGORY = 8
MAX_TOTAL_CHARS = 2000


@dataclass
class MemoryFact:
    key: str
    value: str
    created_at: float
    last_used_at: float
    use_count: int


def _working_memory_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "working_memory.json"


def _empty_store() -> dict[str, list[MemoryFact]]:
    return {c: [] for c in CATEGORIES}


def load_working_memory(workspace_dir: str) -> dict[str, list[MemoryFact]]:
    try:
        raw = json.loads(_working_memory_path(workspace_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return _empty_store()
    store = _empty_store()
    for category in CATEGORIES:
        for entry in raw.get(category) or []:
            try:
                store[category].append(MemoryFact(**entry))
            except TypeError:
                continue  # a malformed/old-shape entry -- drop rather than crash the whole load
    return store


def _save_working_memory(workspace_dir: str, store: dict[str, list[MemoryFact]]) -> None:
    raw = {category: [asdict(f) for f in facts] for category, facts in store.items()}
    _working_memory_path(workspace_dir).write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _eviction_score(fact: MemoryFact) -> tuple[int, float]:
    """Lower sorts first = evicted first: fewest real uses, then oldest last-use
    as the tiebreak. Deliberately NOT based on created_at or prompt-presence --
    see this module's own doc comment on why the frequency signal only counts
    genuine remember_fact/touch_fact calls."""
    return (fact.use_count, fact.last_used_at)


def _render(store: dict[str, list[MemoryFact]]) -> str:
    lines = []
    for category in CATEGORIES:
        for fact in store[category]:
            lines.append(f"[{category}] {fact.key}: {fact.value}")
    if not lines:
        return ""
    header = "Things you've chosen to keep handy (update via remember_fact, remove via forget_fact if stale):"
    return header + "\n" + "\n".join(lines)


def _evict(store: dict[str, list[MemoryFact]]) -> None:
    # 1. Per-category breadth cap.
    for category in CATEGORIES:
        facts = store[category]
        if len(facts) > MAX_PER_CATEGORY:
            facts.sort(key=_eviction_score)
            del facts[: len(facts) - MAX_PER_CATEGORY]
    # 2. Global char-budget cap -- evict the globally-lowest-scoring entry,
    # across every category, one at a time, until the rendered block fits.
    while len(_render(store)) > MAX_TOTAL_CHARS:
        worst: tuple[str, MemoryFact] | None = None
        for category in CATEGORIES:
            for fact in store[category]:
                if worst is None or _eviction_score(fact) < _eviction_score(worst[1]):
                    worst = (category, fact)
        if worst is None:
            break  # nothing left to evict (shouldn't happen -- an empty store renders "")
        store[worst[0]].remove(worst[1])


def remember_fact(workspace_dir: str, category: str, key: str, value: str) -> MemoryFact:
    if category not in CATEGORIES:
        raise ValueError(f'Unknown category "{category}" -- must be one of {", ".join(CATEGORIES)}.')
    store = load_working_memory(workspace_dir)
    now = time.time()
    existing = next((f for f in store[category] if f.key == key), None)
    if existing is not None:
        existing.value = value
        existing.last_used_at = now
        existing.use_count += 1
        fact = existing
    else:
        fact = MemoryFact(key=key, value=value, created_at=now, last_used_at=now, use_count=1)
        store[category].append(fact)
    _evict(store)
    _save_working_memory(workspace_dir, store)
    return fact


def touch_fact(workspace_dir: str, category: str, key: str) -> MemoryFact | None:
    """Marks a fact as genuinely reused again WITHOUT restating its value --
    see this module's own doc comment for why this, not a redundant
    remember_fact call, is what should drive the frequency signal."""
    if category not in CATEGORIES:
        raise ValueError(f'Unknown category "{category}" -- must be one of {", ".join(CATEGORIES)}.')
    store = load_working_memory(workspace_dir)
    fact = next((f for f in store[category] if f.key == key), None)
    if fact is None:
        return None
    fact.last_used_at = time.time()
    fact.use_count += 1
    _save_working_memory(workspace_dir, store)
    return fact


def forget_fact(workspace_dir: str, category: str, key: str) -> bool:
    if category not in CATEGORIES:
        raise ValueError(f'Unknown category "{category}" -- must be one of {", ".join(CATEGORIES)}.')
    store = load_working_memory(workspace_dir)
    facts = store[category]
    before = len(facts)
    store[category] = [f for f in facts if f.key != key]
    if len(store[category]) == before:
        return False
    _save_working_memory(workspace_dir, store)
    return True


def list_facts(workspace_dir: str, category: str | None = None) -> list[MemoryFact]:
    store = load_working_memory(workspace_dir)
    if category is not None:
        if category not in CATEGORIES:
            raise ValueError(f'Unknown category "{category}" -- must be one of {", ".join(CATEGORIES)}.')
        return list(store[category])
    return [f for cat in CATEGORIES for f in store[cat]]


def working_memory_system_prompt_clause(store: dict[str, list[MemoryFact]]) -> str:
    """Always-on (called from chat_session.py's own system_prompt_parts, right next
    to owner_profile_system_prompt_clause). Returns "" when the store is empty --
    unlike owner_profile's unset-state nudge, there's nothing Caroline is
    obligated to go looking for here, so silence is the correct default."""
    return _render(store)
