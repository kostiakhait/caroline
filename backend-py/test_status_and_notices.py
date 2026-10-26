"""The status of a tab with messages still in flight, repeated replies to internal messages, and the context a background activity gets
(2026-10-26). Run: python -m pytest test_status_and_notices.py -q"""

import asyncio
import time

import pytest

from app import background_context as bc
from app import dialogue_log as dl
from app import operations
from app.chat_session import ChatSession
from app.session_context import set_inject_proactive


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def session(tmp_path):
    sent = []

    async def send(envelope):
        sent.append(envelope)

    s = ChatSession(tab_id="2", workspace_dir=str(tmp_path), send=send)
    s.sent = sent
    return s


def states(s):
    return [e["state"] for e in s.sent if e.get("type") == "status"]


# --- the status ---------------------------------------------------------------------------

def test_a_tab_with_a_message_in_flight_is_working_even_when_turn_pending_is_clear(session):
    assert session._compute_public_status()[0] == "ready"
    session.turn_pending = False
    session._inflight_begin()
    assert session._compute_public_status()[0] == "working"
    session._inflight_end()
    assert session._compute_public_status()[0] == "ready"


def test_two_messages_in_flight_keep_it_working_until_both_results_are_in(session):
    session._inflight_begin()
    session._inflight_begin()
    session._inflight_end()
    assert session._compute_public_status()[0] == "working"
    session._inflight_end()
    assert session._compute_public_status()[0] == "ready"
    session._inflight_end()                                  # a result with nothing in flight never goes below zero
    assert session._inflight_messages == 0


def test_a_result_that_never_comes_does_not_leave_the_tab_working(session):
    session._inflight_begin()
    session._inflight_since = time.monotonic() - 1000
    session.last_activity = time.monotonic() - 1000
    assert session._compute_public_status()[0] == "ready"


def test_a_reset_clears_what_is_in_flight(session):
    session._inflight_begin()
    session._inflight_reset()
    assert session._compute_public_status()[0] == "ready"


def test_the_window_is_told_only_when_the_state_changes(session):
    async def go():
        session._inflight_begin()
        session._inflight_begin()
        await asyncio.sleep(0.01)
        session._inflight_end()
        await asyncio.sleep(0.01)
        session._inflight_end()
        await asyncio.sleep(0.01)

    run(go())
    assert states(session) == ["working", "ready"]          # the Stop button appears and goes away once each


# --- the repeats --------------------------------------------------------------------------

SHOWN = "Я уже отклонила: 26 заявок на git.multi-portal.org, все ответы сервера HTTP 200, очередь ожидающих пуста."
REPEAT = "Заявки в GitLab я отклонила через административный API: 26 заявок, очередь ожидающих пуста, ответы сервера HTTP 200."


def test_a_repeat_within_the_window_is_a_repeat_an_old_one_or_another_matter_is_not(session):
    dl.append(session.workspace_dir, "2", dl.ASSISTANT, SHOWN, dl.SHOWN)
    assert session._recent_repeat_of(REPEAT) is True
    assert session._recent_repeat_of("Письмо Оксане отправлено, копия сохранена в Отправленных.") is False
    session.REPEAT_WINDOW_S = 0.0001                           # the same text, but long ago
    time.sleep(0.01)
    assert session._recent_repeat_of(REPEAT) is False


def test_a_background_session_has_no_repeat_gate(tmp_path):
    async def nothing(*a, **k):
        return None

    s = ChatSession(tab_id="1-bg-abc", workspace_dir=str(tmp_path), send=nothing, headless=True)
    dl.append(str(tmp_path), "1-bg-abc", dl.ASSISTANT, SHOWN, dl.SHOWN)
    assert s._recent_repeat_of(REPEAT) is False


def _wire(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _stub_the_pipeline(session, monkeypatch):
    async def fake_review(text, task=None):
        return text

    async def fake_translate(text, language, **kwargs):
        return text

    async def nothing(*args, **kwargs):
        return False

    monkeypatch.setattr("app.plugins.voice_api.review_reply", fake_review)
    monkeypatch.setattr("app.plugins.voice_api.translate_text", fake_translate)
    for name in ("_check_permission_guard", "_check_question_already_answered", "_check_delivery_completeness"):
        monkeypatch.setattr(session, name, nothing)
    monkeypatch.setattr(session, "_narration_task", lambda: "task")


def test_a_repeated_reply_to_an_internal_message_is_dropped_but_a_real_answer_is_not(session, monkeypatch):
    _stub_the_pipeline(session, monkeypatch)
    dl.append(session.workspace_dir, "2", dl.ASSISTANT, SHOWN, dl.SHOWN)
    session.pending_is_real_user = False
    assert run(session._translate_wire_visible_text(_wire(REPEAT))) is None          # the burst of reports of one finished job
    session.pending_is_real_user = True
    assert run(session._translate_wire_visible_text(_wire(REPEAT))) is not None      # the user asked: answer, always


def test_a_relayed_repeat_is_suppressed(session, monkeypatch):
    _stub_the_pipeline(session, monkeypatch)
    dl.append(session.workspace_dir, "2", dl.ASSISTANT, SHOWN, dl.SHOWN)
    run(session.deliver_relayed_reply(REPEAT, task="Reject pending requests"))
    assert not [e for e in session.sent if e.get("type") == "sdk_message"]


# --- the context of a background activity ------------------------------------------------

class FakeAction:
    summary = "Send the draft to Nick Jiang"


class FakeRegistry:
    def open_actions(self):
        return [FakeAction()]


class FakeSession:
    action_registry = FakeRegistry()


def test_a_background_activity_gets_the_conversation_the_open_actions_and_the_guidance(tmp_path):
    from app import action_ledger

    ws = str(tmp_path)
    dl.append(ws, "1", dl.USER, "Заявки в гитлабе отклонить. Молча")
    dl.append(ws, "1", dl.ASSISTANT, "withheld text", dl.WITHHELD)
    action_ledger.append(ws, "1", "email_send", {"to": ["oksana@x.org"], "subject": "Re: Misha"})
    block = bc.context_block(ws, "1", lambda tab: FakeSession())
    assert "Заявки в гитлабе отклонить. Молча" in block and "withheld text" not in block
    assert "Send the draft to Nick Jiang" in block and "ALREADY SENT" in block
    assert "never ask them to confirm it again" in block and block.rstrip().endswith("reply exactly [[NO_UPDATE]].")
    assert bc.context_block(str(tmp_path / "empty"), "9", None) == ""


def test_the_guidance_survives_a_very_long_record(tmp_path):
    ws = str(tmp_path)
    for i in range(12):
        dl.append(ws, "1", dl.USER, f"{i} " + "слово " * 800)
    block = bc.context_block(ws, "1", None)
    assert len(block) <= bc.MAX_CHARS and bc.GUIDANCE in block


def test_a_finished_operation_notice_carries_the_record_and_is_conditional(tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "WORKSPACE_DIR", str(tmp_path))
    dl.append(str(tmp_path), "2", dl.USER, "Отклони заявки в гитлабе")
    got = []
    set_inject_proactive(got.append)
    op = operations.Operation(id="op1", tool_name="email_mark", tab_id="2")
    op.status, op.result = "done", {"text": "ok"}
    operations._notify_operation_completed("email", op)
    assert got and "Отклони заявки в гитлабе" in got[0]
    assert "ONLY if the user has not been told" in got[0] and got[0].rstrip().endswith("]")


def test_silence_after_a_result_means_nothing_is_in_flight_any_more(session):
    """Merged messages leave no result of their own: the counter would stay above zero and the window on "working"."""
    session._inflight_begin()
    session._inflight_begin()
    marker = time.monotonic()
    session.last_activity = marker - 1
    session._inflight_settle(marker)
    assert session._inflight_messages == 0 and session._compute_public_status()[0] == "ready"


def test_activity_after_a_result_keeps_the_next_message_in_flight(session):
    session._inflight_begin()
    session._inflight_begin()
    marker = time.monotonic()
    session.last_activity = marker + 2          # the CLI started the next message
    session._inflight_settle(marker)
    assert session._inflight_messages == 2


def test_ready_reaches_the_window_after_the_settle(session):
    async def go():
        session._inflight_begin()
        session._inflight_begin()
        await asyncio.sleep(0.01)
        session.INFLIGHT_SETTLE_S = 0.05
        session._inflight_end()                  # one result in, one message merged away
        session.last_activity = time.monotonic() - 1
        await asyncio.sleep(0.2)

    run(go())
    assert states(session) == ["working", "ready"]
