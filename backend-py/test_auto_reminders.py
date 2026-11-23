"""Automatic reminders (app/auto_reminders.py, microagents/reminder_extract.py). Run: python -m pytest test_auto_reminders.py -q"""

import asyncio

from app import auto_reminders as ar
from app.microagents import reminder_extract as rx

NOW = "2026-11-20T11:00-05:00"
TEXT = "Hello, I can see you on Friday November 20 at 1 PM at my office, please confirm."


def test_an_unanswered_extraction_is_tried_again_then_given_up(tmp_path, monkeypatch):
    """2026-11-23: the text's hash was remembered before the extraction, so one failed call lost its reminders."""
    calls = []

    async def no_answer(text, source):
        calls.append(text)
        return None

    async def answered(text, source):
        calls.append(text)
        return []

    monkeypatch.setattr(ar, "_failures", {})
    monkeypatch.setattr(ar, "_extract", no_answer)
    for _ in range(ar.MAX_EXTRACT_FAILURES + 2):
        assert asyncio.run(ar.consider(TEXT, "a letter", None, workspace_dir=str(tmp_path))) == 0
    assert len(calls) == ar.MAX_EXTRACT_FAILURES and ar.already_seen(str(tmp_path), TEXT)   # tried, then given up
    other = TEXT + " See you."
    monkeypatch.setattr(ar, "_extract", answered)
    asyncio.run(ar.consider(other, "a letter", None, workspace_dir=str(tmp_path)))
    asyncio.run(ar.consider(other, "a letter", None, workspace_dir=str(tmp_path)))
    assert calls.count(other) == 1                                                          # an answer, even "none", is final


def test_a_text_is_looked_at_once(tmp_path):
    assert ar.first_time(str(tmp_path), TEXT)
    assert not ar.first_time(str(tmp_path), "  " + TEXT.replace(" ", "  "))   # same words, other spacing
    assert ar.first_time(str(tmp_path), TEXT + " Thanks")


def test_machine_text_and_scraps_are_skipped():
    assert not ar.worth_looking_at("⏰ Reminder due (x): something something something")
    assert not ar.worth_looking_at("ok")
    assert ar.worth_looking_at(TEXT)


def test_verify_wants_future_times_with_offsets():
    ok = {"note": "Session with Anita at 1 PM", "remind_at_iso": "2026-11-20T12:15:00-05:00", "event": "Fri 1 PM"}
    inp = {"now": NOW}
    assert rx.verify_reminder_extract(inp, {"reminders": [ok]}) is None
    assert rx.verify_reminder_extract(inp, {"reminders": []}) is None
    assert rx.verify_reminder_extract(inp, {"reminders": [{**ok, "remind_at_iso": "2026-11-20T10:00:00-05:00"}]})   # past
    assert rx.verify_reminder_extract(inp, {"reminders": [{**ok, "remind_at_iso": "2026-11-20T12:15:00"}]})        # no offset
    assert rx.verify_reminder_extract(inp, {"reminders": [ok] * 4})


def test_consider_sets_the_reminder_and_notifies_once(tmp_path, monkeypatch):
    scheduled, noticed = [], []

    async def fake_extract(text, source):
        return [{"note": "Session with Anita at 1 PM", "remind_at_iso": "2026-11-20T12:15:00-05:00", "event": "Fri 1 PM"}]

    async def fake_schedule(args, _rp):
        scheduled.append(args)
        return {"text": "Scheduled (id x) for ..."}

    async def fake_notice(tab_id, lines):
        noticed.append(lines)

    monkeypatch.setattr(ar, "_extract", fake_extract)
    monkeypatch.setattr(ar, "_notice", fake_notice)
    monkeypatch.setattr("app.plugins.scheduler_plugin.schedule_reminder", fake_schedule)
    assert asyncio.run(ar.consider(TEXT, "a letter", "1", workspace_dir=str(tmp_path))) == 1
    assert scheduled[0]["event_type"] == "user" and scheduled[0]["due_at_iso"].startswith("2026-11-20T12:15")
    assert noticed and "Session with Anita" in noticed[0][0]
    assert asyncio.run(ar.consider(TEXT, "a letter", "1", workspace_dir=str(tmp_path))) == 0     # the same text is not judged twice
    assert len(scheduled) == 1


def test_a_refused_duplicate_is_not_announced(tmp_path, monkeypatch):
    async def fake_extract(text, source):
        return [{"note": "n", "remind_at_iso": "2026-11-20T12:15:00-05:00", "event": "e"}]

    async def fake_schedule(args, _rp):
        return {"text": "Not scheduled: reminder abc already covers this topic"}

    noticed = []

    async def fake_notice(tab_id, lines):
        noticed.append(lines)

    monkeypatch.setattr(ar, "_extract", fake_extract)
    monkeypatch.setattr(ar, "_notice", fake_notice)
    monkeypatch.setattr("app.plugins.scheduler_plugin.schedule_reminder", fake_schedule)
    assert asyncio.run(ar.consider(TEXT, "a letter", "1", workspace_dir=str(tmp_path))) == 0
    assert not noticed
