"""A reminder's pseudo-session must outlive a turn that ended on the usage cap
(app/pseudo_tab.py): disposing it cancelled the session's own 90s retry, and
the 10:00 and 12:00 mailbox checks of 2026-10-23 were lost that way.
Run: python -m pytest test_pseudo_tab_retry.py -q"""

import asyncio

import pytest

from app import pseudo_tab


class FakeSession:
    def __init__(self):
        self.api_retry_timer = None
        self.one_shot_followup_timer = None
        self.ended = False
        self.disposed = False

    def dispose(self):
        self.disposed = True
        self.ended = True


RESULT = {"type": "sdk_message", "message": {"type": "result"}}
CAP_REPORT = {"type": "sdk_message", "message": {"type": "assistant", "message": {"content": [{"type": "text", "text": "You've hit your session limit"}]}}}


@pytest.fixture(autouse=True)
def marks(monkeypatch):
    calls = []
    monkeypatch.setattr(pseudo_tab, "_mark_awaiting_retry", lambda pseudo_id, waiting: calls.append(waiting))
    return calls


@pytest.fixture
def session(monkeypatch):
    fake = FakeSession()
    pseudo_id = pseudo_tab.pseudo_tab_id("1", "reminder-1")
    monkeypatch.setitem(pseudo_tab._pseudo_sessions, pseudo_id, fake)
    yield fake
    pseudo_tab._pseudo_sessions.pop(pseudo_id, None)
    pseudo_tab._pseudo_turn_last_visible_text.pop(pseudo_id, None)


def send_events(*events):
    relayed = []

    class Target:
        async def deliver_relayed_reply(self, text, task=None, is_voice=False):
            relayed.append(text)

    send = pseudo_tab._make_pseudo_session_send("1", pseudo_tab.pseudo_tab_id("1", "reminder-1"), lambda _tab: Target())

    async def run():
        for event in events:
            await send(event)
        await asyncio.sleep(0)

    asyncio.run(run())
    return relayed


@pytest.mark.parametrize("timer", ["api_retry_timer", "one_shot_followup_timer"])
def test_a_turn_ended_on_the_cap_keeps_the_session_and_relays_nothing(session, marks, timer):
    setattr(session, timer, object())
    assert send_events(CAP_REPORT, RESULT) == []
    assert not session.disposed
    assert pseudo_tab.is_waiting_on_retry("1", "reminder-1")
    assert marks == [True]


def test_the_real_reply_after_the_retry_disposes_and_relays(session, marks):
    session.one_shot_followup_timer = object()
    send_events(CAP_REPORT, RESULT)
    session.one_shot_followup_timer = None            # the retry fired
    answer = {"type": "sdk_message", "message": {"type": "assistant", "message": {"content": [{"type": "text", "text": "2 new letters"}]}}}
    assert send_events(answer, RESULT) == ["2 new letters"]
    assert session.disposed
    assert not pseudo_tab.is_waiting_on_retry("1", "reminder-1")
    assert marks == [True, False]


def test_activity_id_from_pseudo():
    assert pseudo_tab.activity_id_from_pseudo(pseudo_tab.pseudo_tab_id("5", "91d4cf25")) == "91d4cf25"


# --- the mark in schedule.json, and the resume after a restart --------------------

from app.plugins import scheduler_plugin  # noqa: E402


@pytest.fixture
def schedule(monkeypatch, tmp_path):
    path = tmp_path / "schedule.json"
    monkeypatch.setattr(scheduler_plugin, "_schedule_path", lambda: path)
    monkeypatch.setattr(scheduler_plugin, "_resumed_this_process", set())
    later = "2099-01-01T10:00:00+00:00"
    scheduler_plugin._save_reminders([
        {"id": "mail", "cron": "0 0 */2 * * *", "dueAtIso": later, "note": "check mail"},
        {"id": "once", "dueAtIso": "2020-01-01T10:00:00+00:00", "fired": True, "note": "one-off"},
    ])
    return later


def run_checks(on_due, ticks=1):
    async def run():
        task = scheduler_plugin.start_due_check_loop(on_due, interval_s=0.01)
        await asyncio.sleep(0.01 * ticks + 0.05)
        task.cancel()

    asyncio.run(run())


def test_the_mark_is_set_and_cleared_and_unknown_ids_are_ignored(schedule):
    scheduler_plugin.set_reminder_awaiting_retry("mail", True)
    assert scheduler_plugin._load_reminders()[0]["awaitingRetry"]
    scheduler_plugin.set_reminder_awaiting_retry("some-sub-agent", True)   # not a reminder: nothing happens
    scheduler_plugin.set_reminder_awaiting_retry("mail", False)
    assert "awaitingRetry" not in scheduler_plugin._load_reminders()[0]


def test_after_a_restart_a_marked_reminder_fires_once_and_keeps_its_next_time(schedule):
    later = schedule
    scheduler_plugin.set_reminder_awaiting_retry("mail", True)
    scheduler_plugin.set_reminder_awaiting_retry("once", True)    # a one-off already marked fired, cut off by the cap
    fired = []
    run_checks(lambda r: fired.append(r["id"]) or True, ticks=5)
    assert sorted(fired) == ["mail", "once"]                     # once each, not on every tick
    mail = scheduler_plugin._load_reminders()[0]
    assert mail["dueAtIso"] == later                             # the schedule itself is untouched


def test_an_unmarked_reminder_not_due_does_not_fire(schedule):
    fired = []
    run_checks(lambda r: fired.append(r["id"]) or True, ticks=3)
    assert fired == []


def test_a_normal_turn_is_disposed_as_before(session):
    send_events(RESULT)
    assert session.disposed


def test_no_session_is_not_waiting():
    assert not pseudo_tab.is_waiting_on_retry("1", "no-such-reminder")
