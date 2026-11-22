"""When the startup greeting is said (app/main.py _startup_greeting_wanted).
Run: python -m pytest test_startup_greeting.py -q"""

import time

from app import dialogue_log, main


def stub(monkeypatch, ops=None, retry=None, followup=None, entries=()):
    monkeypatch.setattr(main, "peek_pending_operations", lambda ws, tab: ops or [])
    monkeypatch.setattr(main, "peek_pending_api_retry", lambda ws, tab: retry)
    monkeypatch.setattr(main, "peek_pending_one_shot_followup", lambda ws, tab: followup)
    monkeypatch.setattr(dialogue_log, "read_entries", lambda ws, tab: list(entries))


def entry(age_s):
    return {"ms": int((time.time() - age_s) * 1000), "text": "x"}


def test_greets_after_a_long_absence(monkeypatch):
    stub(monkeypatch, entries=[entry(7200)])
    assert main._startup_greeting_wanted("1")


def test_greets_with_no_history(monkeypatch):
    stub(monkeypatch)
    assert main._startup_greeting_wanted("1")


def test_silent_after_a_recent_conversation(monkeypatch):
    stub(monkeypatch, entries=[entry(600)])
    assert not main._startup_greeting_wanted("1")


def test_silent_when_there_is_work_to_resume(monkeypatch):
    stub(monkeypatch, ops=[{"tool_name": "x"}], entries=[entry(7200)])
    assert not main._startup_greeting_wanted("1")
    stub(monkeypatch, retry=object(), entries=[entry(7200)])
    assert not main._startup_greeting_wanted("1")


def test_greets_when_the_check_fails(monkeypatch):
    stub(monkeypatch)
    monkeypatch.setattr(dialogue_log, "read_entries", lambda ws, tab: 1 / 0)
    assert main._startup_greeting_wanted("1")
