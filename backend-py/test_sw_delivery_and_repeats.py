"""The end-of-turn delivery check and the repeat gate on the sw path (small_model_turn), the same ones the SDK path has
(2026-10-26). Run: python -m pytest test_sw_delivery_and_repeats.py -q"""

import asyncio
from types import SimpleNamespace

import pytest

from app import delivery_report as dr
from app import dialogue_log as dl
from app.chat_session import ChatSession

SHOWN = "Я уже отклонила: 26 заявок на git.multi-portal.org, все ответы сервера HTTP 200, очередь ожидающих пуста."
REPEAT = "Заявки в GitLab я отклонила через административный API: 26 заявок, очередь ожидающих пуста, ответы сервера HTTP 200."
CALLS = [{"n": 1, "tool": "email_send", "args": '{"to": "x"}', "result": "Sent"}]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def session(tmp_path, monkeypatch):
    sent = []

    async def send(envelope):
        sent.append(envelope)

    s = ChatSession(tab_id="2", workspace_dir=str(tmp_path), send=send)
    s.sent = sent

    async def same(text, question):
        return text

    async def nothing(*args, **kwargs):
        return False

    monkeypatch.setattr(s, "_reviewed", same)
    monkeypatch.setattr(s, "_check_permission_guard", nothing)
    monkeypatch.setattr("app.chat_session_mixins.small_model_turn._append_small_model_turn_to_session", lambda *a, **k: None)
    return s


def texts(s):
    return [dr.delivered_text(e) for e in s.sent if dr.delivered_text(e)]


def stub_verifier(monkeypatch, report):
    seen = []

    async def fake(user_text, trace, delivered, tab_id, earlier=""):
        seen.append({"user_text": user_text, "trace": trace, "delivered": delivered})
        return report

    monkeypatch.setattr("app.small_model_engine.verify_delivery", fake)
    return seen


def finish(s, text, real, calls=None):
    """One sw turn from its start (the record) to its answer, without the cascade."""
    s.small_model_turn_is_real_user = real
    if real:
        s._dr_begin_sw("send the reply to Oksana")
    else:
        s._dr_reset()
    s._sw_turn_recorder = SimpleNamespace(session_id="not-this-session", calls=list(calls or []))

    async def go():
        await s._finish_small_model_turn_answered("q", text, False, translate=False)
        await asyncio.sleep(0.05)                       # the check runs as a task of its own

    run(go())


# --- the trace ----------------------------------------------------------------------------

def test_sw_calls_become_trace_lines_and_are_capped():
    assert dr.sw_trace(CALLS) == ['- email_send({"to": "x"}) -> Sent']
    many = [{"tool": f"t{i}", "args": "{}", "result": "ok"} for i in range(100)]
    lines = dr.sw_trace(many)
    assert len(lines) == 40 and lines[-1].startswith("- t99(")


# --- the delivery check -------------------------------------------------------------------

def test_a_real_turn_with_work_is_checked_and_the_report_goes_out(session, monkeypatch):
    seen = stub_verifier(monkeypatch, "Письмо Оксане отправлено, копия в Отправленных.")
    finish(session, "Готово.", real=True, calls=CALLS)
    assert len(seen) == 1
    assert seen[0]["trace"] == ['- email_send({"to": "x"}) -> Sent']
    assert "Готово." in seen[0]["delivered"]
    reports = [e for e in session.sent if e.get("isDeliveryReport")]
    assert len(reports) == 1 and session._dr is None


def test_a_sufficient_delivery_sends_nothing_more(session, monkeypatch):
    stub_verifier(monkeypatch, None)
    finish(session, "Письмо Оксане отправлено.", real=True, calls=CALLS)
    assert texts(session) == ["Письмо Оксане отправлено."]


def test_no_check_without_tool_work_or_for_an_internal_turn(session, monkeypatch):
    seen = stub_verifier(monkeypatch, "report")
    finish(session, "Привет!", real=True, calls=[])                   # the fast lane: nothing was done
    finish(session, "Задача завершена.", real=False, calls=CALLS)       # a reply to an internal message
    assert seen == []


def test_a_stopped_or_failed_turn_leaves_no_record(session):
    session._dr_begin_sw("x")
    session._dr_reset()
    assert session._dr is None
    session._dr_finish_sw(CALLS)                                       # nothing open: nothing happens
    assert session._dr is None


# --- the repeat gate ----------------------------------------------------------------------

def test_a_repeated_reply_to_an_internal_message_is_not_shown(session, monkeypatch):
    stub_verifier(monkeypatch, None)
    dl.append(session.workspace_dir, "2", dl.ASSISTANT, SHOWN, dl.SHOWN)
    finish(session, REPEAT, real=False)
    assert texts(session) == []


def test_the_user_always_gets_the_answer_even_if_it_repeats(session, monkeypatch):
    stub_verifier(monkeypatch, None)
    dl.append(session.workspace_dir, "2", dl.ASSISTANT, SHOWN, dl.SHOWN)
    finish(session, REPEAT, real=True)
    assert texts(session) == [REPEAT]


def test_an_internal_reply_about_another_matter_is_shown(session, monkeypatch):
    stub_verifier(monkeypatch, None)
    dl.append(session.workspace_dir, "2", dl.ASSISTANT, SHOWN, dl.SHOWN)
    finish(session, "Письмо Оксане отправлено, копия сохранена в Отправленных.", real=False)
    assert len(texts(session)) == 1
