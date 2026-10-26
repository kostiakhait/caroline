"""The end-of-turn check of what the user actually got (app/delivery_report.py, chat_session_mixins/delivery_report.py).
Run: python -m pytest test_delivery_report.py -q"""

import asyncio
from types import SimpleNamespace

import pytest

from app import delivery_report as dr
from app import small_model_engine as sme
from app.chat_session_mixins.delivery_report import DeliveryReportMixin


def assistant(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def tool_use(i="t1"):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": "email_send", "input": {"to": "x"}}]}}


def tool_result(i="t1", out="Sent"):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": i, "content": out}]}}


def sdk(wire, **extra):
    return {"type": "sdk_message", "message": wire, "isVoice": False, **extra}


class Session(DeliveryReportMixin):
    def __init__(self):
        self.tab_id = "t"
        self.sent = []

        async def raw(envelope):
            self.sent.append(envelope)

        self.send = self._dr_wrap_send(raw)


def item(text="send it"):
    return {"message": {}, "is_voice": False, "memory_advice_for": text}


def run(coro):
    return asyncio.run(coro)


def test_delivered_text_is_what_reached_the_window():
    assert dr.delivered_text(sdk(assistant("Hello"))) == "Hello"
    assert dr.delivered_text(sdk(assistant("Checking"), isNarration=True)) == "[progress note] Checking"
    assert dr.delivered_text(sdk(assistant("[[NO_UPDATE]]"))) == ""
    assert dr.delivered_text(sdk(tool_use())) == ""
    assert dr.delivered_text({"type": "status", "state": "x"}) == ""
    assert dr.delivered_text(sdk({"type": "result", "result": "done"})) == ""


def test_record_collects_work_and_delivered():
    rec = dr.TurnRecord(user_text="send it")
    assert not rec.has_work and "nothing was shown" in rec.delivered_transcript()
    rec.observe(tool_use())
    rec.observe(tool_result())
    rec.note_sent(sdk(assistant("Still sending")))
    assert rec.has_work and "email_send" in rec.work.trace[0]
    assert rec.delivered_transcript() == "Still sending"


def test_verdict_reading():
    assert sme.read_delivery_verdict("OK") is None
    assert sme.read_delivery_verdict("") is None
    assert sme.read_delivery_verdict("some chatter without a tag") is None
    assert sme.read_delivery_verdict("<final>The email was sent.</final>") == "The email was sent."
    assert sme.read_delivery_verdict("<final>  </final>") is None


def test_prompt_carries_request_work_and_only_the_delivered_text():
    prompt = sme._build_delivery_prompt("send it", ["- email_send({}) -> Sent"], "Still sending")
    assert "send it" in prompt and "email_send" in prompt and "Still sending" in prompt and "ONLY what they were shown" in prompt.replace("Judge ONLY what they were shown", "ONLY what they were shown")


def test_send_wrapper_records_only_while_a_turn_is_open():
    s = Session()
    run(s.send(sdk(assistant("before any turn"))))
    s._dr_begin(item())
    s._dr_observe(tool_use())
    run(s.send(sdk(assistant("Sending now"))))
    assert s._dr.delivered == ["Sending now"] and len(s.sent) == 2


def test_internal_messages_do_not_open_a_record():
    s = Session()
    s._dr_begin({"message": {}, "is_voice": False, "memory_advice_for": None})
    s._dr_begin({"message": {}, "is_voice": False, "memory_advice_for": "x", "ladder_continuation": True})
    assert s._dr is None


def test_insufficient_delivery_sends_the_report(monkeypatch):
    s = Session()
    calls = []

    async def fake(user_text, trace, delivered, tab_id):
        calls.append((user_text, list(trace), delivered))
        return "The email to Oksana was sent."

    monkeypatch.setattr(sme, "verify_delivery", fake)

    async def go():
        s._dr_begin(item("send it"))
        s._dr_observe(tool_use())
        s._dr_observe(tool_result())
        await s.send(sdk(assistant("Still sending, checking the result.")))
        s._dr_finish(SimpleNamespace(is_error=False))
        assert s._dr is None
        await asyncio.sleep(0.05)

    run(go())
    assert calls and calls[0][2] == "Still sending, checking the result."
    assert dr.delivered_text(s.sent[-1]) == "The email to Oksana was sent."


def test_sufficient_delivery_sends_nothing(monkeypatch):
    s = Session()

    async def fake(*a, **k):
        return None

    monkeypatch.setattr(sme, "verify_delivery", fake)

    async def go():
        s._dr_begin(item())
        s._dr_observe(tool_use())
        s._dr_observe(tool_result())
        await s.send(sdk(assistant("Sent to Oksana.")))
        s._dr_finish(SimpleNamespace(is_error=False))
        await asyncio.sleep(0.05)

    run(go())
    assert len(s.sent) == 1


@pytest.mark.parametrize("message,observe", [(SimpleNamespace(is_error=True), True), (SimpleNamespace(is_error=False), False)])
def test_no_check_for_an_error_or_a_turn_without_work(monkeypatch, message, observe):
    s = Session()
    called = []

    async def fake(*a, **k):
        called.append(1)
        return "x"

    monkeypatch.setattr(sme, "verify_delivery", fake)

    async def go():
        s._dr_begin(item())
        if observe:
            s._dr_observe(tool_use())
            s._dr_observe(tool_result())
        s._dr_finish(message)
        await asyncio.sleep(0.05)

    run(go())
    assert not called and len(s.sent) == 0


def test_verifier_failure_changes_nothing(monkeypatch):
    s = Session()

    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(sme, "verify_delivery", boom)

    async def go():
        s._dr_begin(item())
        s._dr_observe(tool_use())
        s._dr_observe(tool_result())
        s._dr_finish(SimpleNamespace(is_error=False))
        await asyncio.sleep(0.05)

    run(go())
    assert s.sent == []
