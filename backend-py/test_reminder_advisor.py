"""One reminder per topic (2026-10-29): schedule_reminder asks reminder.advisor whether an existing
reminder covers the new one's topic and, if so, schedules nothing and hands back a merge for
update_reminder. Run: python -m pytest test_reminder_advisor.py -q"""

import asyncio
import json

import pytest

from app.microagents import reminder_advisor as ra
from app.plugins import scheduler_plugin as sp

MAIL = "РЕГУЛЯРНЫЙ ОТЧЁТ ПО ПОЧТЕ: kostia.khait@gmail.com, khait@navlink.net"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "WORKSPACE_DIR", str(tmp_path))
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def reminders(workspace):
    return json.loads((workspace / "schedule.json").read_text(encoding="utf-8"))


def advise_with(monkeypatch, answer):
    calls = []

    async def fake(note, schedule, pending):
        calls.append({"note": note, "schedule": schedule, "pending": [r["id"] for r in pending]})
        return answer(pending) if callable(answer) else answer

    monkeypatch.setattr(sp, "_advise", fake)
    return calls


def test_the_first_reminder_is_scheduled_without_asking(workspace):
    run(sp.schedule_reminder({"note": MAIL, "cron": "0 0 6-22/2 * * *"}, None))
    assert len(reminders(workspace)) == 1
    assert run(sp._advise("x", "y", [])) is None


def test_same_topic_schedules_nothing_and_hands_back_the_merge(workspace, monkeypatch):
    run(sp.schedule_reminder({"note": MAIL, "cron": "0 0 6-22/2 * * *"}, None))
    first = reminders(workspace)[0]["id"]
    calls = advise_with(monkeypatch, lambda pending: {
        "decision": "update", "id": pending[0]["id"], "note": MAIL + " Особое внимание: Khait v. Khait.", "cron": "", "why": "same mailboxes"})
    reply = run(sp.schedule_reminder({"note": "Проверка почты kostia.khait@gmail.com, особое внимание Khait v. Khait", "cron": "0 0 */2 * * *"}, None))
    assert len(reminders(workspace)) == 1
    assert calls[0]["pending"] == [first] and calls[0]["schedule"] == "cron '0 0 */2 * * *'"
    assert "Not scheduled" in reply["text"] and first in reply["text"] and "update_reminder" in reply["text"]
    assert "Khait v. Khait" in reply["text"] and not reply.get("is_error")


@pytest.mark.parametrize("answer", [{"decision": "create", "why": "new topic"}, {"decision": "separate", "id": "x", "why": "other box"}, None])
def test_create_separate_or_no_answer_schedule_it(workspace, monkeypatch, answer):
    run(sp.schedule_reminder({"note": MAIL, "cron": "0 0 6-22/2 * * *"}, None))
    advise_with(monkeypatch, answer)
    run(sp.schedule_reminder({"note": "Напомни про день рождения Маши", "due_at_iso": "2030-01-05T09:00:00"}, None))
    assert len(reminders(workspace)) == 2


def test_separate_true_skips_the_advisor(workspace, monkeypatch):
    run(sp.schedule_reminder({"note": MAIL, "cron": "0 0 6-22/2 * * *"}, None))
    calls = advise_with(monkeypatch, {"decision": "update", "id": "?", "note": "?", "why": "?"})
    run(sp.schedule_reminder({"note": MAIL + " вечерний", "cron": "0 0 21 * * *", "separate": True}, None))
    assert calls == [] and len(reminders(workspace)) == 2


def test_fired_and_backup_reminders_are_not_compared(workspace, monkeypatch):
    (workspace / "schedule.json").write_text(json.dumps([
        {"id": "a", "note": "done", "dueAtIso": "2020-01-01T00:00:00+00:00", "fired": True},
        {"id": "b", "note": "backup", "dueAtIso": "2030-01-01T00:00:00+00:00", "fired": False, "kind": sp.BACKUP_KIND, "cron": "0 0 * * * *"},
        {"id": "c", "note": MAIL, "dueAtIso": "2030-01-01T00:00:00+00:00", "fired": False, "cron": "0 0 6-22/2 * * *"},
    ]), encoding="utf-8")
    calls = advise_with(monkeypatch, None)
    run(sp.schedule_reminder({"note": "x", "due_at_iso": "2030-01-05T09:00:00"}, None))
    assert calls[0]["pending"] == ["c"]


def test_update_reminder_refines_in_place(workspace):
    run(sp.schedule_reminder({"note": MAIL, "cron": "0 0 6-22/2 * * *"}, None))
    rid = reminders(workspace)[0]["id"]
    sp.set_reminder_last_report(rid, "Почта проверена")
    reply = run(sp.update_reminder({"id": rid, "note": MAIL + " + khait@multi-portal.org", "cron": "0 0 7-21/2 * * *"}, None))
    [r] = reminders(workspace)
    assert r["id"] == rid and r["note"].endswith("khait@multi-portal.org") and r["cron"] == "0 0 7-21/2 * * *"
    assert r["lastReport"]["text"] == "Почта проверена" and "Updated" in reply["text"]


def test_update_reminder_checks_its_input(workspace):
    run(sp.schedule_reminder({"note": MAIL, "cron": "0 0 6-22/2 * * *"}, None))
    rid = reminders(workspace)[0]["id"]
    assert run(sp.update_reminder({"id": "nope", "note": "x"}, None))["is_error"]
    assert run(sp.update_reminder({"id": rid, "cron": "every two hours"}, None))["is_error"]
    assert run(sp.update_reminder({"id": rid, "event_type": "loud"}, None))["is_error"]
    assert reminders(workspace)[0]["note"] == MAIL


EXISTING = [{"id": "c", "note": MAIL, "schedule": "cron '0 0 6-22/2 * * *'"}]


@pytest.mark.parametrize("output,error", [
    ({"decision": "create", "why": "-"}, False),
    ({"decision": "update", "id": "c", "note": "merged", "why": "-"}, False),
    ({"decision": "separate", "id": "c", "why": "-"}, False),
    ({"decision": "merge", "why": "-"}, True),
    ({"decision": "update", "id": "zzz", "note": "merged", "why": "-"}, True),
    ({"decision": "update", "id": "c", "note": " ", "why": "-"}, True),
])
def test_advisor_output_is_checked(output, error):
    assert (ra.verify_reminder_advisor({"new": {}, "existing": EXISTING}, output) is not None) == error


def test_advisor_prompt_carries_both_sides():
    prompt = ra.prompt_reminder_advisor({"new": {"note": "NEW-NOTE", "schedule": "once"}, "existing": EXISTING}, None)
    assert "NEW-NOTE" in prompt and MAIL in prompt and '"decision"' in prompt
