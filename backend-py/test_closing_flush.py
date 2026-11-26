"""Closing a tab: its conversation is copied to a hidden tab for one save-to-memory turn, the closed tab is cleared,
and the copy is removed afterwards (app/closing_flush.py, 2026-11-26).
Run: python -m pytest test_closing_flush.py -q"""

import asyncio
import types

from app import closed_tabs, closing_flush, dialogue_log, memory_nudge
from app import transcript_journal as tj
from app.durability import _chat_mode_path, load_chat_mode, load_tab_session_id, save_chat_mode, save_tab_session_id


def e(role, text, ts):
    return {"role": role, "text": text, "ts": ts, "attachments": []}


def _env(tmp_path, monkeypatch):
    projects = tmp_path / "claude-projects"
    projects.mkdir()
    import app.durability as durability
    for module in (durability, closed_tabs, closing_flush):
        monkeypatch.setattr(module, "claude_project_dir", lambda _w: projects)

    def fork(session_id, directory=None):
        new = f"{session_id}-fork"
        (projects / f"{new}.jsonl").write_text((projects / f"{session_id}.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        return types.SimpleNamespace(session_id=new)

    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "fork_session", fork)
    return projects


def test_the_copy_holds_the_conversation_and_survives_the_close(tmp_path, monkeypatch):
    ws, projects = str(tmp_path), _env(tmp_path, monkeypatch)
    (projects / "s-1.jsonl").write_text('{"x": 1}\n', encoding="utf-8")
    (tmp_path / "sw-history" / "s-1").mkdir(parents=True)
    (tmp_path / "sw-history" / "s-1" / "h.json").write_text("[]", encoding="utf-8")
    save_tab_session_id(ws, "2", "s-1")
    save_tab_session_id(ws, "2", "thread-9", "openai")
    save_chat_mode(ws, "2", "claude")
    tj.append_entries(ws, "2", [e("user", "сделай гонку", 1_700_000_000_000)])
    dialogue_log.append(ws, "2", "user", "сделай гонку")

    hidden = asyncio.run(closing_flush.seed(ws, "2"))
    assert closing_flush.is_flush_tab(hidden)
    closed_tabs.archive_tab(ws, "2", "Tab 2", [])
    closed_tabs.clear_tab_files(ws, "2")

    # the closed tab is clean; the copy has its own session (a fork), SW history, thread, mode and dialogue
    assert load_tab_session_id(ws, "2") is None and not (projects / "s-1.jsonl").exists()
    assert load_tab_session_id(ws, hidden) == "s-1-fork" and (projects / "s-1-fork.jsonl").exists()
    assert (tmp_path / "sw-history" / "s-1-fork" / "h.json").exists()
    assert load_tab_session_id(ws, hidden, "openai") == "thread-9"
    assert load_chat_mode(ws, hidden) == "claude"
    assert [x["text"] for x in tj.read_entries(ws, hidden)] == ["сделай гонку"]
    assert dialogue_log.read_entries(ws, hidden)

    closing_flush.cleanup(ws, hidden)
    assert load_tab_session_id(ws, hidden) is None
    assert not (projects / "s-1-fork.jsonl").exists() and not (tmp_path / "sw-history" / "s-1-fork").exists()
    assert tj.read_entries(ws, hidden) == [] and not dialogue_log.log_path(ws, hidden).exists()
    assert not _chat_mode_path(ws, hidden).exists()


def test_a_tab_without_a_conversation_needs_no_copy(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    assert asyncio.run(closing_flush.seed(str(tmp_path), "5")) is None


def test_an_sw_tab_without_a_claude_transcript_gets_its_history_copied(tmp_path, monkeypatch):
    ws, _projects = str(tmp_path), _env(tmp_path, monkeypatch)
    (tmp_path / "sw-history" / "sw-7").mkdir(parents=True)
    (tmp_path / "sw-history" / "sw-7" / "h.json").write_text("[1]", encoding="utf-8")
    save_tab_session_id(ws, "4", "sw-7")
    save_chat_mode(ws, "4", "sw")
    hidden = asyncio.run(closing_flush.seed(ws, "4"))
    new_id = load_tab_session_id(ws, hidden)
    assert new_id != "sw-7" and (tmp_path / "sw-history" / new_id / "h.json").read_text(encoding="utf-8") == "[1]"
    assert load_chat_mode(ws, hidden) == "sw"


def test_unsaved_state_is_unknown_until_seen_and_forgotten_on_close():
    tab = "t-flush-test"
    assert memory_nudge.unsaved_state(tab) is None
    memory_nudge.note_tool_call(tab, "ferret_build")
    assert memory_nudge.unsaved_state(tab) is True
    memory_nudge.note_tool_call(tab, "save_info")
    assert memory_nudge.unsaved_state(tab) is False
    memory_nudge.note_tool_call(tab, "ferret_build")
    memory_nudge.forget(tab)
    assert memory_nudge.unsaved_state(tab) is None
