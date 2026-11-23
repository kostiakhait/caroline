"""The memory advisor inside the merge window (2026-11-23): its block is ready when the turn starts, and the fast lane
asked ahead with that block is used, not asked again. Run: python -m pytest test_memory_ahead.py -q"""

import asyncio

import pytest

from app import memory_advisor
from app.chat_session import ChatSession
from app.chat_session_mixins import turn_submission as ts

LINES = ["User: hi", "Caroline: hello"]


@pytest.fixture
def session(tmp_path, monkeypatch):
    calls = {"memory": [], "fast": []}

    async def memory_block(text, dialogue, tab_id=None):
        calls["memory"].append(text)
        await asyncio.sleep(0.05)
        return "[memory: the owner's dentist is Dr. Lee]"

    async def try_fast_answer(workspace_dir, persona, text, lines, language, is_voice):
        calls["fast"].append(text)
        return "Dr. Lee.", None, None

    async def nothing(*args, **kwargs):
        return False

    monkeypatch.setattr(memory_advisor, "memory_block", memory_block)
    monkeypatch.setattr(ts, "try_fast_answer", try_fast_answer)
    monkeypatch.setattr(ts, "load_chat_mode", lambda ws, tab: "sw")
    monkeypatch.setattr(ts, "_read_recent_dialogue_lines", lambda *a, **k: list(LINES))
    monkeypatch.setattr(ts, "current_language_name", lambda tab: "English")
    monkeypatch.setattr(ts, "get_persona", lambda ws: object())
    s = ChatSession(tab_id="1", workspace_dir=str(tmp_path), send=nothing)
    s._review_ahead = lambda *a, **k: None
    s._speech_ahead = lambda *a, **k: None
    return s, calls


def hold(s, text, voice):
    s._held_user_messages.append((text, [], voice))
    s._start_memory_ahead()
    s._start_fast_lane_ahead()


def test_a_spoken_message_gets_memory_and_the_fast_lane_in_the_window(session):
    s, calls = session

    async def go():
        hold(s, "who is my dentist", True)
        await asyncio.sleep(0.15)                           # the window runs on
        block, task = await s._memory_block_for_turn("who is my dentist", LINES[-6:])
        text = f"who is my dentist\n\n{block}"
        fast = await s._fast_lane_for_turn(object(), text, LINES, "English", True, raw_text="who is my dentist", memory_task=task)
        return block, task, fast

    block, task, fast = asyncio.run(go())
    assert block.startswith("[memory:") and task is not None
    assert fast[0] == "Dr. Lee."
    assert calls["memory"] == ["who is my dentist"]                                   # once, in the window
    assert calls["fast"] == ["who is my dentist\n\n[memory: the owner's dentist is Dr. Lee]"]   # asked with the block, once


def test_a_block_asked_anew_does_not_take_a_fast_lane_asked_with_another(session):
    s, calls = session

    async def go():
        hold(s, "who is my dentist", True)
        await asyncio.sleep(0.15)
        s._memory_ahead = None                              # the turn's block did not come from the window's call
        block, task = await s._memory_block_for_turn("who is my dentist", LINES[-6:])
        text = f"who is my dentist\n\n{block}"
        await s._fast_lane_for_turn(object(), text, LINES, "English", True, raw_text="who is my dentist", memory_task=task)
        return task

    task = asyncio.run(go())
    assert task is None
    assert len(calls["fast"]) == 2                          # the one asked ahead was not taken


def test_a_typed_burst_asks_memory_once_after_the_pause(session, monkeypatch):
    s, calls = session
    monkeypatch.setattr(ts, "MEMORY_AHEAD_TYPED_DELAY_S", 0.05)

    async def go():
        hold(s, "my dentist", False)
        await asyncio.sleep(0.02)
        hold(s, "what is his name", False)                  # within the pause: the first start never happened
        await asyncio.sleep(0.2)
        return await s._memory_block_for_turn("my dentist\n\nwhat is his name", LINES[-6:])

    block, task = asyncio.run(go())
    assert calls["memory"] == ["my dentist\n\nwhat is his name"] and task is not None
    assert calls["fast"] == []                              # typed: no fast lane ahead, as before


def test_a_changed_text_asks_memory_again(session):
    s, calls = session

    async def go():
        hold(s, "who is my dentist", True)
        await asyncio.sleep(0.15)
        return await s._memory_block_for_turn("who is my dentist? and my doctor", LINES[-6:])

    block, task = asyncio.run(go())
    assert task is None and calls["memory"] == ["who is my dentist", "who is my dentist? and my doctor"]
