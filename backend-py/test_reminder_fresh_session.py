"""Every reminder firing starts a fresh session and is given only what it reported last time
(2026-10-29): the 2-hourly mail report kept resuming one conversation and ran on ~740k tokens
of context per call after four days. Run: python -m pytest test_reminder_fresh_session.py -q"""

import asyncio
import json

import pytest

from app import pseudo_tab
from app.durability import load_tab_session_id, save_tab_session_id
from app.plugins import scheduler_plugin as sp


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(pseudo_tab, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(pseudo_tab, "_pseudo_sessions", {})
    monkeypatch.setattr(pseudo_tab, "_pseudo_sessions_starting", set())
    return tmp_path


def _reminders(workspace):
    return json.loads((workspace / "schedule.json").read_text(encoding="utf-8"))


def test_the_last_report_is_kept_in_the_reminder_and_cut(workspace):
    asyncio.run(sp.schedule_reminder({"note": "mail report", "cron": "0 0 */2 * * *"}, None))
    rid = _reminders(workspace)[0]["id"]
    sp.set_reminder_last_report(rid, "x" * 5000)
    sp.set_reminder_last_report("no-such-reminder", "ignored")
    report = _reminders(workspace)[0]["lastReport"]
    assert len(report["text"]) == sp.LAST_REPORT_MAX_CHARS and report["atIso"]


class FakeSession:
    def __init__(self):
        self.ended = False
        self.api_retry_timer = None
        self.one_shot_followup_timer = None

    def dispose(self):
        self.ended = True


def _fake_start(monkeypatch):
    async def start(real_tab_id, pseudo_id, get_target):
        return FakeSession()

    monkeypatch.setattr(pseudo_tab, "_start_pseudo_session", start)


def test_fresh_drops_the_saved_conversation_before_the_session_is_created(workspace, monkeypatch):
    _fake_start(monkeypatch)
    pid = pseudo_tab.pseudo_tab_id("1", "rem1")
    save_tab_session_id(str(workspace), pid, "old-session")

    async def scenario():
        assert pseudo_tab.get_or_create_pseudo_session_sync("1", "rem1", lambda t: None, fresh=True) is None
        await asyncio.sleep(0)
        return pseudo_tab.get_or_create_pseudo_session_sync("1", "rem1", lambda t: None, fresh=True)

    assert isinstance(asyncio.run(scenario()), FakeSession)
    assert load_tab_session_id(str(workspace), pid) is None


def test_without_fresh_the_saved_conversation_stays(workspace, monkeypatch):
    _fake_start(monkeypatch)
    pid = pseudo_tab.pseudo_tab_id("1", "task7")
    save_tab_session_id(str(workspace), pid, "old-session")

    async def scenario():
        pseudo_tab.get_or_create_pseudo_session_sync("1", "task7", lambda t: None)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert load_tab_session_id(str(workspace), pid) == "old-session"


def test_a_session_already_alive_is_not_reset(workspace):
    pid = pseudo_tab.pseudo_tab_id("1", "rem1")
    alive = FakeSession()
    pseudo_tab._pseudo_sessions[pid] = alive
    save_tab_session_id(str(workspace), pid, "waiting-session")
    assert pseudo_tab.get_or_create_pseudo_session_sync("1", "rem1", lambda t: None, fresh=True) is alive
    assert load_tab_session_id(str(workspace), pid) == "waiting-session"


def test_a_relayed_report_is_saved_into_the_reminder(workspace):
    asyncio.run(sp.schedule_reminder({"note": "mail report", "cron": "0 0 */2 * * *"}, None))
    rid = _reminders(workspace)[0]["id"]
    pid = pseudo_tab.pseudo_tab_id("1", rid)
    send = pseudo_tab._make_pseudo_session_send("1", pid, lambda t: None)

    async def scenario():
        await send({"type": "sdk_message", "message": {"type": "assistant", "message": {"content": [{"type": "text", "text": "Почта проверена, ничего нового нет"}]}}})
        await send({"type": "sdk_message", "message": {"type": "result"}})

    asyncio.run(scenario())
    assert _reminders(workspace)[0]["lastReport"]["text"] == "Почта проверена, ничего нового нет"
