"""app/action_registry.py -- the action-boundary tracking system's core
state: a per-tab, max-5, LRU-evicted registry of open "actions".

Per explicit instruction (2026-10-18), after two confirmed live incidents in
PermissionGuard (a re-blocked, already-approved send; Caroline narrating a
delivery that never happened), the user's own 6-point spec:
1. A pending operation is identified and matched (via LLM) against already-
   open ones at the start of the step that needs it -- see
   app/microagents/permission_identify.py's own open_actions/
   matched_action_id extension.
2. When the underlying operation completes (e.g. the email is actually
   sent), the action closes -- see app/operations.py's new post-success
   action-closer hook.
3. After every real user message, a separate agent decides whether that
   message grants or revokes sanction for any open action -- see
   app/microagents/action_sanction.py.
4. PermissionGuard is skipped entirely for an action whose `approved` flag
   is already true -- see chat_session_mixins/permission_guard.py's
   _check_action_before_send.
5. At most MAX_OPEN_ACTIONS_PER_TAB actions open at once; LRU eviction.
   Being evicted (or never having existed) means NOT sanctioned, exactly
   like today.
6. An action nobody ever flagged just falls through to today's unchanged
   behavior -- for free, since `approved` defaults to False.

Deliberately a SEPARATE concept from app/memory_topics.py's "track"/topic
system (long-lived, cross-tab, semantic, server-matched) -- an Action is
short-lived, per-tab, and may carry a `track_hint` purely for admin/debug
visibility, never used for matching (matching is action-to-action, via the
identify step's own LLM judgment).

Persisted to disk per tab (user's explicit choice, 2026-10-18) -- see
durability.py's save_action_state/load_action_state/clear_action_state,
same per-tab-file convention as pending-operations-<id>.json."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

MAX_OPEN_ACTIONS_PER_TAB = 5


@dataclass
class Action:
    id: str
    summary: str
    track_hint: Optional[str] = None
    approved: bool = False
    created_at: float = field(default_factory=time.time)
    last_touched_at: float = field(default_factory=time.time)

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id, "summary": self.summary, "trackHint": self.track_hint, "approved": self.approved,
            "createdAt": self.created_at, "lastTouchedAt": self.last_touched_at,
        }

    @classmethod
    def from_wire(cls, row: dict[str, Any]) -> "Action":
        return cls(
            id=row["id"], summary=row["summary"], track_hint=row.get("trackHint"),
            approved=bool(row.get("approved")), created_at=row.get("createdAt") or time.time(),
            last_touched_at=row.get("lastTouchedAt") or time.time(),
        )


class ActionRegistry:
    """Per-tab. Not thread-safe beyond what the single-threaded asyncio
    event loop already guarantees -- same assumption every other per-tab,
    in-process ChatSession attribute in this codebase makes (e.g.
    pending_unapproved_action, permission_guard_consecutive_blocks)."""

    def __init__(self) -> None:
        self._by_id: dict[str, Action] = {}

    def open_actions(self) -> list[Action]:
        return list(self._by_id.values())

    def is_empty(self) -> bool:
        return not self._by_id

    def get(self, action_id: str) -> Optional[Action]:
        return self._by_id.get(action_id)

    def open_new(self, summary: str, track_hint: str | None = None) -> Action:
        self._evict_lru_if_full()
        action = Action(id=uuid.uuid4().hex[:12], summary=summary, track_hint=track_hint)
        self._by_id[action.id] = action
        return action

    def _evict_lru_if_full(self) -> None:
        if len(self._by_id) < MAX_OPEN_ACTIONS_PER_TAB:
            return
        # The least-recently-touched one goes -- never the one about to be
        # added (it isn't in _by_id yet when this runs). Per spec point 5:
        # once evicted, it is simply no longer in the list, which IS "not
        # sanctioned" -- no separate bookkeeping needed for that.
        oldest_id = min(self._by_id, key=lambda aid: self._by_id[aid].last_touched_at)
        del self._by_id[oldest_id]

    def touch(self, action_id: str) -> None:
        action = self._by_id.get(action_id)
        if action is not None:
            action.last_touched_at = time.time()

    def set_approved(self, action_id: str, approved: bool) -> None:
        action = self._by_id.get(action_id)
        if action is not None:
            action.approved = approved
            action.last_touched_at = time.time()

    def close(self, action_id: str) -> None:
        self._by_id.pop(action_id, None)

    def to_wire(self) -> list[dict[str, Any]]:
        return [a.to_wire() for a in self._by_id.values()]

    @classmethod
    def from_wire(cls, rows: list[dict[str, Any]]) -> "ActionRegistry":
        registry = cls()
        for row in rows:
            action = Action.from_wire(row)
            registry._by_id[action.id] = action
        return registry
