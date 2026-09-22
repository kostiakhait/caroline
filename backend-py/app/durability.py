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
    separator becomes '-', one-for-one. Used to locate a session's .jsonl
    transcript directly on disk."""
    encoded = re.sub(r"[:\\/]", "-", workspace_dir)
    return Path.home() / ".claude" / "projects" / encoded


def openai_transcripts_dir(workspace_dir: str) -> Path:
    """workspace/openai-transcripts/ -- the conversation log of tabs answered
    by the OpenAI engine, in the same JSONL shape Claude Code writes, so every
    history reader works on it unchanged (see session_transcript_path)."""
    return Path(workspace_dir) / "openai-transcripts"


def session_transcript_path(workspace_dir: str, session_id: str) -> Path:
    """Where a session's transcript lives: an OpenAI thread's own log if one
    exists under that id, else Claude Code's file. Ids never collide across
    engines (Claude session ids and Codex thread ids are distinct UUIDs)."""
    own = openai_transcripts_dir(workspace_dir) / f"{session_id}.jsonl"
    return own if own.exists() else claude_project_dir(workspace_dir) / f"{session_id}.jsonl"


def dehydrated_dir(workspace_dir: str) -> Path:
    """workspace/dehydrated/ -- where the PreCompact hook copies a
    pre-compaction transcript so earlier context stays recoverable, and
    where main.py's expand_dehydrated_ref serves files back from. Exported
    so that handler can validate a requested path is really inside it."""
    return Path(workspace_dir) / "dehydrated"


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


# --- per-tab in-flight background operations, for restart recovery -------
# Per explicit instruction (2026-09-15, "Кэролайн регулярно теряет фоновых
# агентов" -> agreed follow-up fix): app/operations.py's OperationRegistry
# is pure in-memory, process-scoped -- a genuine full backend-process
# restart (crash, a forced relaunch, an in-place update) wipes it
# completely, with zero trace, for ANY operation that had already left
# dispatch()'s fast path. pending-turn-<id>.json alone doesn't cover this:
# it only fires when the TRIGGERING turn itself is still unanswered at
# restart time, but the confirmed live incident (an 8-mailbox cleanup the
# model explicitly promised to report back on) had already replied to the
# user and moved on -- the turn was "done" from the SDK's own perspective,
# only the background operation itself was still running. This is a
# SEPARATE small durability file, one per tab, a dict of operation_id ->
# {tool_name, startedAtIso} for every operation currently past the fast
# path -- written by operations.py's dispatch() when an operation goes
# slow, cleared when it actually finishes (see run()'s own completion).
# Anything still in this file when a tab's session first connects in a
# NEW process lifetime is, by construction, stale (a live process's own
# OperationRegistry would already be tracking it) -- main.py's WS handler
# peeks it the same way it already peeks pending-turn and injects a
# recovery nudge, mirroring that exact pattern.


def _pending_operations_path(workspace_dir: str, tab_id: str) -> Path:
    return Path(workspace_dir) / f"pending-operations-{_sanitize_tab_id(tab_id)}.json"


def _load_pending_operations(workspace_dir: str, tab_id: str) -> dict:
    path = _pending_operations_path(workspace_dir, tab_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log_event("engine", "load_pending_operations_failed", tab_id=tab_id, error=str(exc))
        return {}


def save_pending_operation(workspace_dir: str, tab_id: str, operation_id: str, tool_name: str) -> None:
    from datetime import datetime, timezone

    try:
        operations = _load_pending_operations(workspace_dir, tab_id)
        operations[operation_id] = {"toolName": tool_name, "startedAtIso": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        path = _pending_operations_path(workspace_dir, tab_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(operations, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as exc:
        # Best-effort -- worst case this specific operation isn't covered
        # by restart recovery, but that must be visible in the log, not
        # silently swallowed.
        log_event("engine", "save_pending_operation_failed", tab_id=tab_id, operation_id=operation_id, error=str(exc))


def clear_pending_operation(workspace_dir: str, tab_id: str, operation_id: str) -> None:
    try:
        operations = _load_pending_operations(workspace_dir, tab_id)
        if operation_id not in operations:
            return
        del operations[operation_id]
        path = _pending_operations_path(workspace_dir, tab_id)
        if operations:
            path.write_text(json.dumps(operations, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except Exception as exc:
        log_event("engine", "clear_pending_operation_failed", tab_id=tab_id, operation_id=operation_id, error=str(exc))


def peek_pending_operations(workspace_dir: str, tab_id: str) -> list[dict]:
    """Read-only -- does NOT delete the file (same reasoning as
    peek_pending_turn's own docstring: deletion isn't this function's
    job). Returns a list of {operation_id, tool_name, started_at_iso}."""
    operations = _load_pending_operations(workspace_dir, tab_id)
    return [
        {"operation_id": op_id, "tool_name": entry.get("toolName", "?"), "started_at_iso": entry.get("startedAtIso", "")}
        for op_id, entry in operations.items()
    ]


# --- per-tab Claude session id, for resume: instead of continue: true -----
# continue:true always resumes "the most recent session for this cwd" --
# fine for a single conversation, but with multiple independent tabs
# sharing one workspace/cwd, every tab's continue:true would race to
# resume the SAME most-recent thread. Each tab instead gets resume:<its
# own stored session id>, captured from the SDK's own session_id field.

def _tab_session_id_path(workspace_dir: str, tab_id: str) -> Path:
    return Path(workspace_dir) / f"tab-session-{_sanitize_tab_id(tab_id)}.json"


# One file per tab holds the resume id of EACH engine that has run in it
# ("sessionId" = the Claude session, "openaiThreadId" = the Codex thread), so
# switching a tab between engines never loses either conversation.
_SESSION_KEY = {"claude": "sessionId", "openai": "openaiThreadId"}


def _read_tab_session_file(workspace_dir: str, tab_id: str) -> dict:
    path = _tab_session_id_path(workspace_dir, tab_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log_event("engine", "load_tab_session_id_failed", tab_id=tab_id, error=str(exc))
        return {}


def load_tab_session_id(workspace_dir: str, tab_id: str, engine: str = "claude") -> str | None:
    return _read_tab_session_file(workspace_dir, tab_id).get(_SESSION_KEY[engine])


def save_tab_session_id(workspace_dir: str, tab_id: str, session_id: str, engine: str = "claude") -> None:
    try:
        path = _tab_session_id_path(workspace_dir, tab_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _read_tab_session_file(workspace_dir, tab_id)
        data[_SESSION_KEY[engine]] = session_id
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        log_event("engine", "save_tab_session_id_failed", tab_id=tab_id, error=str(exc))


def clear_tab_session_id(workspace_dir: str, tab_id: str, engine: str | None = "claude") -> None:
    """The next runLoop iteration for this tab omits `resume` from its
    query() options, so the SDK starts a genuinely new session instead of
    resuming anything. Used by compaction's backoff path and by
    reset-unrecoverable-session. engine=None clears every engine's id (a full
    tab wipe)."""
    try:
        path = _tab_session_id_path(workspace_dir, tab_id)
        if engine is None:
            path.unlink(missing_ok=True)
            return
        data = _read_tab_session_file(workspace_dir, tab_id)
        if data.pop(_SESSION_KEY[engine], None) is None:
            return
        if data:
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
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
        archive_path = data.get("archivePath")
        # A pointer to a file that no longer exists (an archive pruned as
        # redundant, deleted by hand) is worse than no pointer: the system
        # prompt would send the model to read something that isn't there.
        if archive_path and not Path(archive_path).exists():
            log_event("engine", "tab_continuity_archive_missing", tab_id=tab_id, archive_path=archive_path)
            return None
        return archive_path
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


# --- per-tab chat mode (claude vs sw), user-facing Settings toggle ---------
# Per explicit instruction (2026-09-14): each tab independently picks which
# backend answers its real turns -- "claude" (the full Claude Agent SDK,
# submit()) or "sw" (the small-model/Camerlengo path billed through the
# SquirrelWisdom PIA wallet, run_small_model_turn()). Defaults to "claude"
# for every tab; see subscription_mode.py's chat_mode_eligible() for the
# gating (both a Claude subscription AND a paid SW balance) that decides
# whether "sw" can be SET at all -- this module only persists whatever was
# already validated, same file-per-tab shape as tab-session-<id>.json etc.

def _chat_mode_path(workspace_dir: str, tab_id: str) -> Path:
    return Path(workspace_dir) / f"chat-mode-{_sanitize_tab_id(tab_id)}.json"


def load_chat_mode(workspace_dir: str, tab_id: str) -> str:
    path = _chat_mode_path(workspace_dir, tab_id)
    if not path.exists():
        return "claude"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mode = data.get("mode")
        return mode if mode in ("claude", "sw", "openai") else "claude"
    except Exception as exc:
        log_event("engine", "load_chat_mode_failed", tab_id=tab_id, error=str(exc))
        return "claude"


def save_chat_mode(workspace_dir: str, tab_id: str, mode: str) -> None:
    try:
        path = _chat_mode_path(workspace_dir, tab_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mode": mode}, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        log_event("engine", "save_chat_mode_failed", tab_id=tab_id, error=str(exc))


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
