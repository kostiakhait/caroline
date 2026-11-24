"""A closed tab's content goes to the closed-tabs archive and a new tab with the same number starts clean (2026-11-24).
Run: python -m pytest test_closed_tabs.py -q"""

import asyncio
import json

from app import closed_tabs, dialogue_log
from app import transcript_journal as tj
from app.durability import (
    _pending_turn_path,
    load_tab_session_id,
    save_tab_session_id,
)
from app.plugins import closed_tabs_plugin


def e(role, text, ts):
    return {"role": role, "text": text, "ts": ts, "attachments": []}


def test_close_archives_everything_and_frees_the_number(tmp_path, monkeypatch):
    ws = str(tmp_path)
    monkeypatch.setattr(closed_tabs, "claude_project_dir", lambda _w: tmp_path / "claude-projects")
    import app.durability as durability
    monkeypatch.setattr(durability, "claude_project_dir", lambda _w: tmp_path / "claude-projects")
    sess = tmp_path / "claude-projects" / "s-1.jsonl"
    sess.parent.mkdir(parents=True)
    sess.write_text("", encoding="utf-8")
    tj.append_entries(ws, "3", [e("user", "посчитай налоги", 1_700_000_000_000), e("assistant", "готово: 1200", 1_700_000_060_000)])
    dialogue_log.append(ws, "3", "user", "посчитай налоги")
    save_tab_session_id(ws, "3", "s-1")
    _pending_turn_path(ws, "3").write_text("{}", encoding="utf-8")
    (tmp_path / "topics-3.json").write_text("[]", encoding="utf-8")
    # the window had one message the journal had not got yet
    client = [e("user", "и ещё вопрос", 1_700_000_120_000)]

    meta = closed_tabs.archive_tab(ws, "3", "Налоги", client)
    closed_tabs.clear_tab_files(ws, "3")

    folder = closed_tabs.archive_folder(ws, meta["id"])
    assert folder is not None and meta["tabId"] == "3" and meta["name"] == "Налоги" and meta["messages"] == 3
    text = (folder / "transcript.md").read_text(encoding="utf-8")
    assert text.index("посчитай налоги") < text.index("готово: 1200") < text.index("и ещё вопрос")
    raw = {p.name for p in (folder / "raw").iterdir()}
    assert {"3.jsonl", "dialogue-log-3.jsonl", "tab-session-3.json", "pending-turn-3.json", "topics-3.json", "session-s-1.jsonl"} <= raw

    # the number is clean: no session, no journal, nothing pending, the session transcript itself gone
    assert load_tab_session_id(ws, "3") is None
    assert tj.read_entries(ws, "3") == []
    assert not _pending_turn_path(ws, "3").exists()
    assert not (tmp_path / "topics-3.json").exists()
    assert not sess.exists()
    assert dialogue_log.read_entries(ws, "3") == []
    # a new tab 3 journals from scratch (the cached keys of the old journal are gone too)
    assert tj.append_entries(ws, "3", [e("user", "посчитай налоги", 1_700_000_000_000)]) == 1


def test_an_empty_tab_leaves_no_archive(tmp_path):
    ws = str(tmp_path)
    assert closed_tabs.archive_tab(ws, "2", None, []) is None
    assert closed_tabs.list_archives(ws) == []


def test_tools_list_and_read(tmp_path, monkeypatch):
    ws = str(tmp_path)
    monkeypatch.setattr(closed_tabs_plugin, "WORKSPACE_DIR", ws)
    tj.append_entries(ws, "4", [e("user", "план отпуска", 1_700_000_000_000), e("assistant", "Рим, потом Неаполь", 1_700_000_001_000)])
    meta = closed_tabs.archive_tab(ws, "4", "Отпуск", None)
    closed_tabs.clear_tab_files(ws, "4")

    listed = asyncio.run(closed_tabs_plugin.list_closed_tabs({}, None))["text"]
    assert meta["id"] in listed and "Отпуск" in listed and "план отпуска" in listed
    assert "Отпуск" in asyncio.run(closed_tabs_plugin.list_closed_tabs({"contains": "неаполь"}, None))["text"]
    assert "No closed tabs" in asyncio.run(closed_tabs_plugin.list_closed_tabs({"contains": "лыжи"}, None))["text"]

    by_tab = asyncio.run(closed_tabs_plugin.read_closed_tab({"tab": "4"}, None))["text"]
    assert "Рим, потом Неаполь" in by_tab
    by_id = asyncio.run(closed_tabs_plugin.read_closed_tab({"id": meta["id"], "contains": "рим"}, None))["text"]
    assert "Рим" in by_id and "план отпуска" not in by_id
    bad = asyncio.run(closed_tabs_plugin.read_closed_tab({"id": "../x"}, None))
    assert bad.get("is_error")
    assert json.loads((closed_tabs.archive_folder(ws, meta["id"]) / "meta.json").read_text(encoding="utf-8"))["messages"] == 2
