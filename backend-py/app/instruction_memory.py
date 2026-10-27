"""The owner's standing instructions -- "always do X", "never do Y" -- kept locally and put into
EVERY system prompt (both engines), so a rule the owner gave once is in front of the model on every
turn, not found only when a memory search happens to surface it.

Why not save_info: that memory is a remote store read through a per-message search; a standing rule
that the search does not return in time is a rule the model never saw (seen live, 2026-10-26: she kept
volunteering findings the owner had not asked for). The prompt block is always-on, so it must stay
small -- same reasoning as working_memory.py / owner_profile.py (the --append-system-prompt length
overflow) -- and a bounded block needs a rule for what goes when it is full.

Eviction (explicit instruction, 2026-10-26: "вытесняла по важности и LFU"): an entry is ranked by
(priority, use_count, last_used_at) and the lowest rank goes first -- importance decides, then how
often the instruction has really been used (LFU), then how recently. Priority is 1 (a preference) to
5 (a hard rule, evicted only after everything below it). use_count rises when the owner states the same
instruction again (saying it twice is a signal) and when the model reports having applied it
(instruction_applied); merely being injected into a prompt does not count, or every entry would count
the same.

Local JSON in the workspace dir, same pattern as working_memory.py.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

MIN_PRIORITY = 1
MAX_PRIORITY = 5
DEFAULT_PRIORITY = 3
MAX_ITEMS = 30
MAX_TOTAL_CHARS = 3000
MAX_ITEM_CHARS = 400


@dataclass
class Instruction:
    id: str
    text: str
    priority: int
    created_at: float
    last_used_at: float
    use_count: int


def _path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "instruction_memory.json"


def load_instructions(workspace_dir: str) -> list[Instruction]:
    try:
        raw = json.loads(_path(workspace_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    out: list[Instruction] = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            out.append(Instruction(**entry))
        except TypeError:
            continue  # a malformed/old-shape entry -- drop rather than fail the whole load
    return out


def _save(workspace_dir: str, items: list[Instruction]) -> None:
    path = _path(workspace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(i) for i in items], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _eviction_rank(item: Instruction) -> tuple[int, int, float]:
    """Lower sorts first = evicted first."""
    return (item.priority, item.use_count, item.last_used_at)


def _render_lines(items: list[Instruction]) -> list[str]:
    return [f"- [{i.priority}] ({i.id}) {i.text}" for i in sorted(items, key=lambda i: (-i.priority, -i.use_count, -i.last_used_at))]


def render(items: list[Instruction]) -> str:
    if not items:
        return ""
    header = (
        "Standing instructions from your owner. They outrank your own judgement about what would be helpful, "
        "and apply on every turn until the owner changes them (the number is the priority, 5 = hard rule; "
        "the id is for instruction_forget/instruction_applied). Do not mention this list unless asked:"
    )
    return header + "\n" + "\n".join(_render_lines(items))


def _evict(items: list[Instruction]) -> list[Instruction]:
    """Trims in place; returns what was evicted."""
    evicted: list[Instruction] = []
    # The budget is the entries themselves; the fixed header is not theirs to pay for.
    while items and (len(items) > MAX_ITEMS or len("\n".join(_render_lines(items))) > MAX_TOTAL_CHARS):
        worst = min(items, key=_eviction_rank)
        items.remove(worst)
        evicted.append(worst)
    return evicted


def _normalized(text: str) -> str:
    return re.sub(r"\W+", " ", text.casefold()).strip()


def clamp_priority(value: object) -> int:
    try:
        return max(MIN_PRIORITY, min(MAX_PRIORITY, int(value)))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY


def add_instruction(workspace_dir: str, text: str, priority: object = None) -> tuple[Instruction, list[Instruction], bool]:
    """Returns (the stored instruction, whatever it pushed out, whether it was already there). The
    same instruction stated again (same text up to case/punctuation) is not duplicated: its use_count
    rises and its priority becomes the higher of the two."""
    text = " ".join((text or "").split())[:MAX_ITEM_CHARS]
    if not text:
        raise ValueError("An instruction needs text.")
    prio = clamp_priority(priority) if priority is not None else None
    items = load_instructions(workspace_dir)
    now = time.time()
    key = _normalized(text)
    existing = next((i for i in items if _normalized(i.text) == key), None)
    if existing is not None:
        existing.use_count += 1
        existing.last_used_at = now
        if prio is not None:
            existing.priority = max(existing.priority, prio)
        item, repeated = existing, True
    else:
        item = Instruction(
            id=uuid.uuid4().hex[:6], text=text, priority=prio if prio is not None else DEFAULT_PRIORITY,
            created_at=now, last_used_at=now, use_count=1,
        )
        items.append(item)
        repeated = False
    evicted = _evict(items)
    _save(workspace_dir, items)
    return item, evicted, repeated  # `item` itself is in `evicted` when a full store of higher ranks pushed it out


def mark_applied(workspace_dir: str, instruction_id: str) -> Instruction | None:
    items = load_instructions(workspace_dir)
    item = next((i for i in items if i.id == instruction_id), None)
    if item is None:
        return None
    item.use_count += 1
    item.last_used_at = time.time()
    _save(workspace_dir, items)
    return item


def forget_instruction(workspace_dir: str, instruction_id: str) -> bool:
    items = load_instructions(workspace_dir)
    kept = [i for i in items if i.id != instruction_id]
    if len(kept) == len(items):
        return False
    _save(workspace_dir, kept)
    return True


def instruction_memory_system_prompt_clause(workspace_dir: str) -> str:
    """Always-on, in both engines' system prompts. "" while nothing is stored."""
    return render(load_instructions(workspace_dir))
