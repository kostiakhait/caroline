"""Closed tabs: what a tab held is kept when the tab is closed, and a new tab never inherits it (2026-11-24).

Per explicit instruction: when a tab is closed its content must be saved and stay available on request, but a NEW
tab -- even one that gets the same number -- starts clean. Before this, closing a tab only removed its window: the
session, the journals and the window's own transcript stayed under the tab's number, so the next tab with that
number opened with the old conversation in it.

`close_tab` moves everything the tab held into workspace/closed-tabs/<YYYYMMDD-HHMMSS>-tab<N>/:
- transcript.md -- the conversation as the user saw it, oldest first (the transcript journal, completed from the
  session transcript and from the window's own copy the shell hands over);
- meta.json -- tab number, name, when it was closed, the first and last message, how many messages;
- raw/ -- the tab's own files as they were (session transcript, SW history, dialogue log, journal, topics, ...),
  so nothing is lost even where transcript.md is a summary of them.
Then every per-tab state file is removed, so the number is free for a clean tab. The chat-mode choice (Claude / SW
/ OpenAI) and the model-ladder statistics are settings, not content, and stay.

The model reaches the archive through app/plugins/closed_tabs_plugin.py (list_closed_tabs, read_closed_tab)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app import dialogue_log, transcript_journal
from app.agent_registry import _state_path as _agents_state_path
from app.agent_registry import status_file_path as _agents_status_path
from app.durability import (
    _action_state_path,
    _pending_api_retry_path,
    _pending_one_shot_followup_path,
    _pending_operations_path,
    _pending_turn_path,
    _sanitize_tab_id,
    _tab_continuity_archive_path,
    _tab_session_id_path,
    claude_project_dir,
    load_tab_continuity_archive,
    load_tab_session_id,
)
from app.history import read_recent_history_for_session
from app.logging_setup import log_event

# edition:cut openai transcripts
from app.durability import openai_transcripts_dir

# edition:end

ARCHIVE_DIR_NAME = "closed-tabs"
TRANSCRIPT_FILE = "transcript.md"
META_FILE = "meta.json"
# Everything the session transcript holds -- a closed tab's archive is its whole conversation, not the recent part.
_ALL_HISTORY = 1_000_000


def archive_root(workspace_dir: str) -> Path:
    return Path(workspace_dir) / ARCHIVE_DIR_NAME


def _tab_files(workspace_dir: str, tab_id: str) -> list[Path]:
    """Every per-tab file that is content or pending work of this tab (not its settings)."""
    ws = Path(workspace_dir)
    safe = _sanitize_tab_id(tab_id)
    return [
        transcript_journal.journal_path(workspace_dir, tab_id),
        dialogue_log.log_path(workspace_dir, tab_id),
        ws / f"companion-visible-{tab_id}.json",
        ws / f"recent-24h-dialogue-{safe}.txt",
        ws / f"system-prompt-{safe}.md",
        ws / f"topics-{safe}.json",
        ws / f"last-language-{safe}.json",
        _action_state_path(workspace_dir, tab_id),
        _pending_turn_path(workspace_dir, tab_id),
        _pending_operations_path(workspace_dir, tab_id),
        _pending_api_retry_path(workspace_dir, tab_id),
        _pending_one_shot_followup_path(workspace_dir, tab_id),
        _agents_state_path(workspace_dir, tab_id),
        _agents_status_path(workspace_dir, tab_id),
        _tab_continuity_archive_path(workspace_dir, tab_id),
        _tab_session_id_path(workspace_dir, tab_id),
    ]


def _session_files(workspace_dir: str, tab_id: str) -> list[tuple[Path, str]]:
    """(source, name inside raw/) of the conversation files the tab's session ids point to."""
    out: list[tuple[Path, str]] = []
    session_id = load_tab_session_id(workspace_dir, tab_id)
    if session_id:
        out.append((claude_project_dir(workspace_dir) / f"{session_id}.jsonl", f"session-{session_id}.jsonl"))
        out.append((Path(workspace_dir) / "sw-history" / session_id, f"sw-history-{session_id}"))
    # edition:cut openai transcripts
    thread_id = load_tab_session_id(workspace_dir, tab_id, "openai")
    if thread_id:
        out.append((openai_transcripts_dir(workspace_dir) / f"{thread_id}.jsonl", f"openai-{thread_id}.jsonl"))
    # edition:end
    continuity = load_tab_continuity_archive(workspace_dir, tab_id)
    if continuity:
        out.append((Path(continuity), f"continuity-{Path(continuity).name}"))
    return out


def _copy(src: Path, dst: Path) -> bool:
    try:
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        else:
            return False
        return True
    except Exception as exc:  # noqa: BLE001 -- one unreadable file must not stop the rest of the archive
        log_event("engine", "closed_tab_copy_failed", src=str(src), error=str(exc)[:200])
        return False


def _remove(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        log_event("engine", "closed_tab_remove_failed", path=str(path), error=str(exc)[:200])


def _fmt_ts(ms: int) -> str:
    if not ms:
        return "?"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def render_transcript(entries: list[dict[str, Any]], title: str) -> str:
    lines = [f"# {title}", ""]
    for e in entries:
        who = "User" if e.get("role") == "user" else "Caroline"
        lines.append(f"## {_fmt_ts(transcript_journal._ts_ms(e))} -- {who}")
        lines.append("")
        lines.append((e.get("text") or "").rstrip())
        names = [a.get("name") for a in (e.get("attachments") or []) if isinstance(a, dict) and a.get("name")]
        if names:
            lines.append("")
            lines.append("Attachments: " + ", ".join(names))
        lines.append("")
    return "\n".join(lines)


def _new_archive_dir(workspace_dir: str, tab_id: str) -> Path:
    root = archive_root(workspace_dir)
    base = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-tab{_sanitize_tab_id(tab_id)}"
    candidate, n = root / base, 1
    while candidate.exists():
        n += 1
        candidate = root / f"{base}-{n}"
    candidate.mkdir(parents=True)
    return candidate


def archive_tab(workspace_dir: str, tab_id: str, name: str | None = None, client_entries: list[Any] | None = None) -> dict[str, Any] | None:
    """Copies the tab's content into a new archive folder; returns its meta (None when the tab held nothing).
    Nothing is removed here -- see clear_tab_files."""
    if client_entries:
        transcript_journal.append_entries(workspace_dir, tab_id, client_entries)
    session_id = load_tab_session_id(workspace_dir, tab_id)
    if session_id:
        try:
            transcript_journal.append_entries(
                workspace_dir, tab_id, read_recent_history_for_session(workspace_dir, session_id, limit=_ALL_HISTORY)
            )
        except Exception as exc:  # noqa: BLE001 -- the journal and the raw copy still keep the conversation
            log_event("engine", "closed_tab_history_read_failed", tab_id=tab_id, error=str(exc)[:200])
    entries = transcript_journal.read_entries(workspace_dir, tab_id)
    present = [p for p in _tab_files(workspace_dir, tab_id) if p.exists()]
    session_files = [(src, dst) for src, dst in _session_files(workspace_dir, tab_id) if src.exists()]
    if not entries and not session_files and not any(p.name.startswith("dialogue-log-") for p in present):
        log_event("engine", "closed_tab_nothing_to_archive", tab_id=tab_id)
        return None

    folder = _new_archive_dir(workspace_dir, tab_id)
    raw = folder / "raw"
    for p in present:
        _copy(p, raw / p.name)
    for src, dst in session_files:
        _copy(src, raw / dst)

    closed_at = datetime.now().astimezone()
    tab_label = f"Tab {tab_id}" + (f" ({name})" if name and name != f"Tab {tab_id}" else "")
    first_user = next((e.get("text") or "" for e in entries if e.get("role") == "user"), "")
    meta = {
        "id": folder.name,
        "tabId": tab_id,
        "name": name or f"Tab {tab_id}",
        "closedAt": closed_at.isoformat(timespec="seconds"),
        "firstMessageAt": _fmt_ts(transcript_journal._ts_ms(entries[0])) if entries else None,
        "lastMessageAt": _fmt_ts(transcript_journal._ts_ms(entries[-1])) if entries else None,
        "messages": len(entries),
        "firstUserMessage": " ".join(first_user.split())[:200],
        "sessionId": session_id,
    }
    title = f"{tab_label}, closed {closed_at.strftime('%Y-%m-%d %H:%M')}"
    (folder / TRANSCRIPT_FILE).write_text(render_transcript(entries, title), encoding="utf-8")
    (folder / META_FILE).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log_event("engine", "closed_tab_archived", tab_id=tab_id, archive=folder.name, messages=len(entries))
    return meta


def clear_tab_files(workspace_dir: str, tab_id: str) -> None:
    """Removes every per-tab content/pending file and the conversation files the session ids point to, so the next
    tab with this number starts clean. Run after archive_tab and after the live session (if any) was stopped."""
    for src, dst in _session_files(workspace_dir, tab_id):
        # the continuity archive lives in the shared dehydrated/ dir, where other records may point to it; it stays
        # there (archive_prune removes it once nothing does) -- only the tab's pointer to it goes below
        if not dst.startswith("continuity-"):
            _remove(src)
    transcript_journal.forget(workspace_dir, tab_id)
    for p in _tab_files(workspace_dir, tab_id):
        _remove(p)
    log_event("engine", "closed_tab_cleared", tab_id=tab_id)


def list_archives(workspace_dir: str) -> list[dict[str, Any]]:
    """Every closed tab's meta, newest first."""
    root = archive_root(workspace_dir)
    if not root.exists():
        return []
    out = []
    for folder in root.iterdir():
        meta_path = folder / META_FILE
        if not meta_path.exists():
            continue
        try:
            out.append(json.loads(meta_path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            log_event("engine", "closed_tab_meta_unreadable", archive=folder.name, error=str(exc)[:200])
    out.sort(key=lambda m: m.get("closedAt") or "", reverse=True)
    return out


def archive_folder(workspace_dir: str, archive_id: str) -> Path | None:
    """The folder of one archive, or None -- the id must name a folder directly inside closed-tabs/."""
    root = archive_root(workspace_dir).resolve()
    folder = (root / archive_id).resolve()
    if folder.parent != root or not (folder / META_FILE).exists():
        return None
    return folder
