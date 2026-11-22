"""A switch away from sw stops the running sw turn and hands its task to the new engine (2026-10-29):
the tab was switched to Claude while an sw turn ran, the sw turn went on for 17 minutes up to Sonnet
over OpenRouter while Claude already answered the user's next messages.
Run: python -m pytest test_engine_switch_sw_handover.py -q"""

import asyncio

import pytest

from app.chat_session import ChatSession
from app.chat_session_shared import SW_HANDOVER_NOTE, _SYNTHETIC_TURN_MARKER, save_chat_mode


@pytest.fixture
def session(tmp_path):
    async def send(envelope):
        pass

    s = ChatSession(tab_id="2", workspace_dir=str(tmp_path), send=send)
    s.submitted = []
    s.submit = lambda text, attachments=None, is_real_user=True, is_voice=False, **kw: s.submitted.append((text, is_real_user, is_voice))
    return s


def start_sw_turn(s, question, real=True):
    """An sw turn that runs until cancelled and then cleans up as _run_small_model_turn's own handler does."""
    async def run():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            s.turn_pending = False
            s._end_small_model_turn(start_queued_real_turn=False)
            raise

    s.last_real_user_question = question
    s.small_model_turn_is_real_user = real
    s.small_model_active = True
    s.turn_pending = True
    s._small_model_task = asyncio.get_running_loop().create_task(run())
    return s._small_model_task


def test_switch_to_claude_stops_the_sw_turn_and_hands_its_task_over(session):
    async def scenario():
        save_chat_mode(session.workspace_dir, session.tab_id, "sw")
        task = start_sw_turn(session, "Опубликуй вакансию на LinkedIn")
        await asyncio.sleep(0)
        session.small_model_pending_comments = ["Текст не меняй", f"{_SYNTHETIC_TURN_MARKER}internal nudge"]
        session.small_model_queued_real_turns = [("И ещё одно", [], False)]
        session.turn_is_voice = True
        save_chat_mode(session.workspace_dir, session.tab_id, "claude")
        assert session.switch_engine_if_needed() == "switching"
        for _ in range(5):
            await asyncio.sleep(0)
        return task

    task = asyncio.run(scenario())
    assert task.cancelled()
    assert not session.small_model_active and not session.turn_pending
    assert session.submitted == [
        (SW_HANDOVER_NOTE + "\n\nОпубликуй вакансию на LinkedIn\n\nТекст не меняй", True, True),
        ("И ещё одно", True, False),
    ]


def test_a_proactive_sw_turn_is_stopped_without_handing_its_nudge_over(session):
    async def scenario():
        task = start_sw_turn(session, "старый вопрос", real=False)
        save_chat_mode(session.workspace_dir, session.tab_id, "claude")
        assert session.switch_engine_if_needed() == "switching"
        for _ in range(5):
            await asyncio.sleep(0)
        return task

    assert asyncio.run(scenario()).cancelled()
    assert session.submitted == []


def test_a_turn_cancelled_before_it_ran_is_still_cleaned_up(session):
    async def scenario():
        start_sw_turn(session, "вопрос")
        save_chat_mode(session.workspace_dir, session.tab_id, "claude")
        session.switch_engine_if_needed()
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert not session.small_model_active and not session.turn_pending and session._small_model_task is None
    assert session.submitted == [(SW_HANDOVER_NOTE + "\n\nвопрос", True, False)]


def test_no_sw_turn_running_changes_nothing(session):
    save_chat_mode(session.workspace_dir, session.tab_id, "claude")
    assert session.switch_engine_if_needed() == "unchanged"
    assert session.submitted == []


def test_a_switch_to_sw_leaves_the_sw_turn_alone(session):
    async def scenario():
        task = start_sw_turn(session, "вопрос")
        save_chat_mode(session.workspace_dir, session.tab_id, "sw")
        result = session.switch_engine_if_needed()
        await asyncio.sleep(0)
        alive = not task.done()
        task.cancel()
        return result, alive

    assert asyncio.run(scenario()) == ("unchanged", True)
