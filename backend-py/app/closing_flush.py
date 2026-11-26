"""Closing a tab saves its unsaved work to memory first (2026-11-26).

Per explicit instruction: a closed tab is reached later only through memory, and only on a direct request
(app/closed_tabs.py archives it; a new tab, even with the same number, starts a new, unrelated topic). What goes
into memory is the main model's decision, made by the model itself with save_info -- code never composes it.
Before this, whatever the model had done since its last save_info was lost from memory when the tab closed:
live, a game build and download made in the last turns never reached the topic, and "what did we do in the
closed tab?" was answered from an older state.

The tab's own session cannot do it: the shell frees the number at once, and a new tab with the same number
reuses the same per-tab files. So, before the tab is archived and cleared, its conversation is copied to a
hidden tab of its own (`closing-flush-...`): the Claude session is forked (claude_agent_sdk.fork_session),
the SW history is copied under the fork's id, the OpenAI thread id is carried over (the closed tab never uses it
again), and the chat mode, the dialogue log and the transcript journal go along. A headless ChatSession on that
hidden tab gets one service turn, FLUSH_TEXT; when the turn is over the session is disposed and every file of the
hidden tab is removed.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

from app import dialogue_log, transcript_journal
from app.durability import (
    _chat_mode_path,
    claude_project_dir,
    load_chat_mode,
    load_tab_session_id,
    save_chat_mode,
    save_tab_session_id,
)
from app.logging_setup import log_event

TAB_PREFIX = "closing-flush-"

FLUSH_TEXT = (
    "[This tab has just been closed. This conversation ends here, and from now on its work is reachable only "
    "through your memory. Save to memory now, with save_info, everything done and learned since your last "
    "save_info, up to the last request and its result: what was asked, what was made or changed and how it "
    "stands now, what was replaced or removed, files, links, numbers, versions, what is still open. Then reply "
    "with exactly [[NO_UPDATE]].]"
)


def is_flush_tab(tab_id: str | None) -> bool:
    return bool(tab_id) and str(tab_id).startswith(TAB_PREFIX)


def _copy(src: Path, dst: Path) -> None:
    try:
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    except Exception as exc:  # noqa: BLE001 -- a missing side file must not stop the flush
        log_event("engine", "closing_flush_copy_failed", src=str(src), error=str(exc)[:200])


async def seed(workspace_dir: str, tab_id: str) -> str | None:
    """Copies the tab's conversation to a new hidden tab; returns its id, or None when the tab has no conversation."""
    claude_id = load_tab_session_id(workspace_dir, tab_id)
    openai_id = load_tab_session_id(workspace_dir, tab_id, "openai")
    if not claude_id and not openai_id:
        return None
    hidden = f"{TAB_PREFIX}{uuid.uuid4().hex[:12]}"
    ws = Path(workspace_dir)
    save_chat_mode(workspace_dir, hidden, load_chat_mode(workspace_dir, tab_id))
    if claude_id:
        new_id = None
        if (claude_project_dir(workspace_dir) / f"{claude_id}.jsonl").exists():
            from claude_agent_sdk import fork_session

            new_id = (await asyncio.to_thread(fork_session, claude_id, directory=workspace_dir)).session_id
        new_id = new_id or str(uuid.uuid4())
        _copy(ws / "sw-history" / claude_id, ws / "sw-history" / new_id)
        save_tab_session_id(workspace_dir, hidden, new_id)
    if openai_id:
        save_tab_session_id(workspace_dir, hidden, openai_id, "openai")
    _copy(dialogue_log.log_path(workspace_dir, tab_id), dialogue_log.log_path(workspace_dir, hidden))
    _copy(transcript_journal.journal_path(workspace_dir, tab_id), transcript_journal.journal_path(workspace_dir, hidden))
    log_event("engine", "closing_flush_seeded", tab_id=tab_id, hidden=hidden, claude=bool(claude_id), openai=bool(openai_id))
    return hidden


def cleanup(workspace_dir: str, hidden: str) -> None:
    """Removes every file of the hidden tab: its forked session, its SW history, its per-tab files and chat mode.
    The OpenAI thread is the closed tab's own and is not touched."""
    from app.closed_tabs import _remove, _tab_files

    ws = Path(workspace_dir)
    session_id = load_tab_session_id(workspace_dir, hidden)
    if session_id:
        _remove(claude_project_dir(workspace_dir) / f"{session_id}.jsonl")
        _remove(ws / "sw-history" / session_id)
    for path in _tab_files(workspace_dir, hidden):
        _remove(path)
    _remove(_chat_mode_path(workspace_dir, hidden))
    log_event("engine", "closing_flush_cleaned", hidden=hidden)
