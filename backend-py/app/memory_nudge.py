"""Reminds the main model to save what it learns, mechanically.

The always-on memory instruction (policies.memory_check_first_instruction)
is not enough on long tool-heavy work: confirmed live 2026-10-10, a
browser task of ~80 tool calls found six candidates, their resumes and
what was agreed with them, and saved none of it -- the user later asked
"wasn't all this in the notes?" and memory held nothing. So the engine
counts plugin tool calls per tab since the last save_info and, every
CALLS_BEFORE_REMINDER of them, adds a one-line reminder to the result of
the call that crossed the line. What to save is still the model's choice.

Also tells ChatSession whether anything happened since the last save, so
a context compaction is preceded by a turn that saves it (see
ChatSession._check_forced_compaction).
"""

from __future__ import annotations

CALLS_BEFORE_REMINDER = 10
SAVE_TOOLS = frozenset({"save_info"})
# Tools that are part of remembering, not of the work: they neither count
# towards the reminder nor make anything "unsaved".
MEMORY_TOOLS = frozenset({"request_info", "save_document", "owner_profile_remember", "owner_profile_recall"})

REMINDER_TEXT = (
    f"[Memory: {CALLS_BEFORE_REMINDER} tool calls since your last save_info. If you have learned anything new "
    "and specific since then -- names, contacts, links, numbers, file locations, what was decided or done and "
    "its result -- save it with save_info now, then carry on.]"
)

_calls_since_save: dict[str, int] = {}
_unsaved: dict[str, bool] = {}


def note_tool_call(tab_id: str | None, tool_name: str) -> str | None:
    """Counts one plugin tool call; returns the reminder to append to its
    result when one is due, else None."""
    if not tab_id:
        return None
    if tool_name in SAVE_TOOLS:
        _calls_since_save[tab_id] = 0
        _unsaved[tab_id] = False
        return None
    if tool_name in MEMORY_TOOLS:
        return None
    _unsaved[tab_id] = True
    count = _calls_since_save.get(tab_id, 0) + 1
    if count >= CALLS_BEFORE_REMINDER:
        _calls_since_save[tab_id] = 0
        return REMINDER_TEXT
    _calls_since_save[tab_id] = count
    return None


def has_unsaved_work(tab_id: str) -> bool:
    """True when the tab called a working tool since its last save_info."""
    return _unsaved.get(tab_id, False)
