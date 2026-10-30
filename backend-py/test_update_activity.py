"""Is Caroline in use? (app/update_activity.py): what holds an update back and what does not.
Run: python -m pytest test_update_activity.py -q"""

from types import SimpleNamespace

import pytest

from app import update_activity as ua


def session(tab="1", *, ended=False, headless=False, turn=False, idle=10_000.0, now=100_000.0):
    return SimpleNamespace(tab_id=tab, ended=ended, headless=headless, turn_pending=turn, last_user_activity=now - idle)


def snap(sessions, dialogue=False, tasks=None, now=100_000.0):
    return ua.snapshot(sessions, "ws", dialogue, now=now, running_tasks=lambda _w, tab: (tasks or {}).get(tab, {}))


def test_no_session_means_inactive():
    assert snap([]) == {"busy": False, "reasons": [], "idle_s": None}


def test_a_long_idle_window_is_inactive():
    assert snap([session(idle=3_600)]) == {"busy": False, "reasons": [], "idle_s": 3_600}


def test_a_running_turn_is_in_use_even_when_the_owner_is_idle():
    out = snap([session(turn=True, idle=3_600)])
    assert out["busy"] and out["reasons"] == ["turn:1"]


def test_the_owner_active_in_the_last_ten_minutes_is_in_use():
    assert snap([session(idle=599)])["reasons"] == ["user"]
    assert not snap([session(idle=601)])["busy"]


def test_a_running_task_holds_the_update_back():
    out = snap([session(idle=3_600)], tasks={"1": {"t1": {}, "t2": {}}})
    assert out["reasons"] == ["task:1:2"]


def test_the_dialogue_switch_alone_does_not_block_for_ever():
    assert not snap([session(idle=3_600)], dialogue=True)["busy"]
    assert "dialogue" in snap([session(idle=1_200)], dialogue=True)["reasons"]


def test_a_headless_session_has_no_owner_but_its_turn_counts():
    assert not snap([session("akinakes-c1", headless=True, idle=0)])["busy"]
    assert snap([session("akinakes-c1", headless=True, turn=True)])["reasons"] == ["turn:akinakes-c1"]


def test_an_ended_session_counts_for_nothing():
    assert not snap([session(ended=True, turn=True, idle=0)])["busy"]


def test_an_unreadable_task_file_does_not_block():
    def broken(_w, _t):
        raise OSError("locked")
    assert not ua.snapshot([session(idle=3_600)], "ws", False, now=100_000.0, running_tasks=broken)["busy"]


def test_the_endpoint_reports_the_snapshot(monkeypatch):
    pytest.importorskip("claude_agent_sdk")
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "sessions", {"1": SimpleNamespace(tab_id="1", ended=False, headless=False, turn_pending=True, last_user_activity=0.0)})
    monkeypatch.setattr(ua, "snapshot", lambda s, w, d: {"busy": True, "reasons": ["turn:1"], "idle_s": 0})
    body = TestClient(main.app, base_url="http://127.0.0.1").get("/api/update/activity").json()  # the backend answers loopback only
    assert body == {"ok": True, "busy": True, "reasons": ["turn:1"], "idle_s": 0}
