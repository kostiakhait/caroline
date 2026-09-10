"""Ports backend/src/durability.ts -- survives a FULL app restart (not just
the in-process watchdog restart ChatSession's own failure handling replays
from memory): if Caroline's process gets closed or crashes while a turn is
still in flight, everything in the ChatSession object is lost, but the
user's message may already be sitting in the conversation with no reply.
Persisted here so the next process lifetime can notice and proactively
finish it instead of leaving it dangling.

Three per-tab files under workspaceDir: tab-session-<id>.json (resume id),
pending-turn-<id>.json (a not-yet-answered turn, read-only peek -- see
peek_pending_turn's own docstring for why never delete-on-read), and
tab-continuity-<id>.json (the continuity-archive pointer, written only by
ChatSession's own reset-unrecoverable-session path).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.logging_setup import log_event


def _sanitize_tab_id(tab_id: str) -> str:
    """Keeps a caller-supplied tabId (ultimately from a WS query string)
    from escaping the workspace dir."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", tab_id)
    return sanitized or "default"


def claude_project_dir(workspace_dir: str) -> Path:
    """Matches Claude Code's own project-dir slugging: every ':' and path
    separator becomes '-', one-for-one. Used both here and by
    compaction.py to locate a session's .jsonl transcript directly on
    disk."""
    encoded = re.sub(r"[:\\/]", "-", workspace_dir)
    return Path.home() / ".claude" / "projects" / encoded


# --- pending turn (crash-mid-turn recovery) --------------------------------

@dataclass
class PendingTurn:
    text: str
    attachments: list
    submitted_at_iso: str


def _pending_turn_path(workspace_dir: str, tab_id: str) -> Path:
    return Path(workspace_dir) / f"pending-turn-{_sanitize_tab_id(tab_id)}.json"


def save_pending_turn(workspace_dir: str, tab_id: str, text: str, attachments: list) -> None:
    from datetime import datetime, timezone
    turn = {"text": text, "attachments": attachments, "submittedAtIso": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    try:
        path = _pending_turn_path(workspace_dir, tab_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(turn, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as exc:
        # Best-effort -- worst case this specific restart doesn't
        # auto-resume, but that must be visible in the log, not silently
        # swallowed.
        log_event("engine", "save_pending_turn_failed", tab_id=tab_id, error=str(exc))


def clear_pending_turn(workspace_dir: str, tab_id: str) -> None:
    try:
        _pending_turn_path(workspace_dir, tab_id).unlink(missing_ok=True)
    except Exception as exc:
        log_event("engine", "clear_pending_turn_failed", tab_id=tab_id, error=str(exc))


def peek_pending_turn(workspace_dir: str, tab_id: str) -> PendingTurn | None:
    """Read-only -- does NOT delete the file. A previous delete-on-read
    version lost the user's unanswered message permanently during a run
    of quick kill-and-restart cycles (a process could read-and-delete,
    then itself get killed before any WS client connected to receive the
    injection). Deletion isn't this function's job: the eventual resume
    message is itself submitted like any other turn, which calls
    save_pending_turn() again (overwriting this file) and
    clear_pending_turn() normally once that turn completes -- the same
    safety net keeps covering the resume attempt itself if IT also gets
    interrupted."""
    path = _pending_turn_path(workspace_dir, tab_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PendingTurn(text=data["text"], attachments=data.get("attachments") or [], submitted_at_iso=data.get("submittedAtIso", ""))
    except Exception as exc:
        log_event("engine", "peek_pending_turn_failed", tab_id=tab_id, error=str(exc))
        return None


# --- per-tab Claude session id, for resume: instead of continue: true -----
# continue:true always resumes "the most recent session for this cwd" --
# fine for a single conversation, but with multiple independent tabs
# sharing one workspace/cwd, every tab's continue:true would race to
# resume the SAME most-recent thread. Each tab instead gets resume:<its
# own stored session id>, captured from the SDK's own session_id field.

def _tab_session_id_path(workspace_dir: str, tab_id: str) -> Path:
    return Path(workspace_dir) / f"tab-session-{_sanitize_tab_id(tab_id)}.json"


def load_tab_session_id(workspace_dir: str, tab_id: str) -> str | None:
    path = _tab_session_id_path(workspace_dir, tab_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("sessionId")
    except Exception as exc:
        log_event("engine", "load_tab_session_id_failed", tab_id=tab_id, error=str(exc))
        return None


def save_tab_session_id(workspace_dir: str, tab_id: str, session_id: str) -> None:
    try:
        path = _tab_session_id_path(workspace_dir, tab_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sessionId": session_id}, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        log_event("engine", "save_tab_session_id_failed", tab_id=tab_id, error=str(exc))


def clear_tab_session_id(workspace_dir: str, tab_id: str) -> None:
    """The next runLoop iteration for this tab omits `resume` from its
    query() options, so the SDK starts a genuinely new session instead of
    resuming anything. Used by compaction's backoff path and by
    reset-unrecoverable-session."""
    try:
        _tab_session_id_path(workspace_dir, tab_id).unlink(missing_ok=True)
    except Exception as exc:
        log_event("engine", "clear_tab_session_id_failed", tab_id=tab_id, error=str(exc))


# --- per-tab continuity-archive pointer ------------------------------------
# Read fresh into EVERY query()'s own systemPrompt for as long as it's set
# -- a real, standing instruction for the whole session's lifetime, not a
# single message that scrolls out of attention (a one-shot version of this
# caused a confirmed-live incident: the model flatly denied having just
# done something from a few turns back in a fresh session it had no other
# reason to reconsider).

def _tab_continuity_archive_path(workspace_dir: str, tab_id: str) -> Path:
    return Path(workspace_dir) / f"tab-continuity-{_sanitize_tab_id(tab_id)}.json"


def load_tab_continuity_archive(workspace_dir: str, tab_id: str) -> str | None:
    path = _tab_continuity_archive_path(workspace_dir, tab_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("archivePath")
    except Exception as exc:
        log_event("engine", "load_tab_continuity_archive_failed", tab_id=tab_id, error=str(exc))
        return None


def save_tab_continuity_archive(workspace_dir: str, tab_id: str, archive_path: str) -> None:
    try:
        path = _tab_continuity_archive_path(workspace_dir, tab_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"archivePath": archive_path}, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        log_event("engine", "save_tab_continuity_archive_failed", tab_id=tab_id, error=str(exc))


def clear_tab_continuity_archive(workspace_dir: str, tab_id: str) -> None:
    try:
        _tab_continuity_archive_path(workspace_dir, tab_id).unlink(missing_ok=True)
    except Exception as exc:
        log_event("engine", "clear_tab_continuity_archive_failed", tab_id=tab_id, error=str(exc))


# --- per-tab compaction pointer --------------------------------------------
# Same pattern/reasoning as the continuity-archive pointer above, but for
# routine age-based compaction (see compaction.py) instead of an
# unrecoverable-session error -- see policies.py's
# compaction_pointer_instruction. Naturally overwritten on every
# subsequent compaction; no separate clear function needed.

def _tab_compaction_note_path(workspace_dir: str, tab_id: str) -> Path:
    return Path(workspace_dir) / f"tab-compaction-{_sanitize_tab_id(tab_id)}.json"


def load_tab_compaction_note(workspace_dir: str, tab_id: str) -> tuple[str | None, str | None]:
    path = _tab_compaction_note_path(workspace_dir, tab_id)
    if not path.exists():
        return (None, None)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (data.get("parentPath"), data.get("compactedAtIso"))
    except Exception as exc:
        log_event("engine", "load_tab_compaction_note_failed", tab_id=tab_id, error=str(exc))
        return (None, None)


def save_tab_compaction_note(workspace_dir: str, tab_id: str, parent_path: str, compacted_at_iso: str) -> None:
    try:
        path = _tab_compaction_note_path(workspace_dir, tab_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"parentPath": parent_path, "compactedAtIso": compacted_at_iso}, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        log_event("engine", "save_tab_compaction_note_failed", tab_id=tab_id, error=str(exc))


def find_most_recent_claude_session_id(workspace_dir: str) -> str | None:
    """One-time migration for users upgrading from pre-multi-tab Caroline:
    reads Claude Code's own session transcript directory for this
    workspace directly off disk and returns the most recently modified
    one's id, mirroring what continue:true would have picked. Never
    raises -- worst case the caller just falls through to starting fresh."""
    try:
        project_dir = claude_project_dir(workspace_dir)
        if not project_dir.exists():
            return None
        best: tuple[str, float] | None = None
        for entry in project_dir.iterdir():
            if entry.suffix != ".jsonl":
                continue
            mtime = entry.stat().st_mtime
            if best is None or mtime > best[1]:
                best = (entry.stem, mtime)
        return best[0] if best else None
    except Exception as exc:
        log_event("engine", "find_most_recent_claude_session_id_failed", workspace_dir=workspace_dir, error=str(exc))
        return None
