"""The timing of microagent runs and of the memory advisor's steps (2026-10-26: the advisor waited 13-25 s per message and
nothing said where the time went). Measurement only: nothing about a run or its result changes.
Run: python -m pytest test_microagent_timing.py -q"""

import asyncio
import threading
import time

import pytest

from app import memory_advisor
from app import microagent_client as mc


@pytest.fixture
def events(monkeypatch):
    seen = []
    monkeypatch.setattr(mc, "log_event", lambda component, event, **fields: seen.append((event, fields)))
    monkeypatch.setattr(memory_advisor, "log_event", lambda component, event, **fields: seen.append((event, fields)))
    return seen


def of(events, name):
    return [f for e, f in events if e == name]


def test_a_model_call_is_logged_with_its_agent_duration_and_spend(events, monkeypatch):
    def fake_post(body, timeout=30.0, retries=3):
        time.sleep(0.02)
        return {".status": "ok", "text": "{}", "usage": {"prompt_tokens": 120, "completion_tokens": 30, "reasoning_tokens": 25}}

    monkeypatch.setattr(mc, "_post_json_sync", fake_post)
    result = {}

    def worker():
        result["reply"] = mc._llm_call("nano", "a prompt", 60.0, "low")

    thread = threading.Thread(target=worker, name="microagent:sw.memory.need")
    thread.start()
    thread.join()
    assert result["reply"]["text"] == "{}"                                  # the reply is unchanged
    call = of(events, "microagent_llm_call")[0]
    assert call["agent"] == "sw.memory.need" and call["model_class"] == "nano" and call["reasoning"] == "low"
    assert call["ok"] and call["ms"] >= 15 and call["prompt_chars"] == 8 and call["rung_call"] == 1
    assert call["prompt_tokens"] == 120 and call["completion_tokens"] == 30 and call["reasoning_tokens"] == 25
    assert call["inflight_calls"] == 1


def test_a_failed_model_call_is_logged_and_still_raised(events, monkeypatch):
    def failing(body, timeout=30.0, retries=3):
        raise RuntimeError("server said no")

    monkeypatch.setattr(mc, "_post_json_sync", failing)
    with pytest.raises(RuntimeError, match="server said no"):
        mc._llm_call("mini", "p", 60.0, None)
    call = of(events, "microagent_llm_call")[0]
    assert call["ok"] is False and "server said no" in call["error"]


def test_a_run_reports_queue_wait_duration_calls_and_concurrency(events, monkeypatch):
    monkeypatch.setattr(mc, "_post_json_sync", lambda body, timeout=30.0, retries=3: {".status": "ok", "text": "x", "usage": None})

    class FakeRuntime:
        def run(self, agent, agent_input, context):
            inner = threading.Thread(target=lambda: (mc._llm_call("nano", "p", 1.0), mc._llm_call("nano", "p", 1.0)),
                                     name=f"microagent:{agent}")
            inner.start()
            inner.join()
            return {"status": "ok", "output": {"needs_memory": False}}

    submitted = time.monotonic() - 0.05          # it waited 50 ms for a free thread
    result = mc.run_timed(FakeRuntime(), "sw.memory.need", {}, None, submitted)
    assert result["status"] == "ok"
    run = of(events, "microagent_run")[0]
    assert run["agent"] == "sw.memory.need" and run["model_calls"] == 2 and run["status"] == "ok"
    assert run["queued_ms"] >= 45 and run["run_ms"] >= 0 and run["concurrent_runs"] == 1 and run["threads"] >= 1


def test_the_advisors_steps_are_timed(events, monkeypatch):
    async def fake_decide(message, dialogue, tab_id):
        await asyncio.sleep(0.02)
        return True, ["who is Nick"], "refers to a person"

    async def fake_run_query(query):
        await asyncio.sleep(0.02)
        return {"text": "Nick Jiang is a distributor."}

    monkeypatch.setattr(memory_advisor, "_decide", fake_decide)
    monkeypatch.setattr("app.plugins.memory_plugin._run_memory_query", fake_run_query)
    monkeypatch.setattr("app.login_api.is_logged_in", lambda: True)
    block = asyncio.run(memory_advisor.memory_block("Напиши Нику", ["User: hi"], "2"))
    assert "Nick Jiang is a distributor." in block
    decide = of(events, "memory_advisor_decide_timing")[0]
    assert decide["ms"] >= 15 and decide["needed"] is True and decide["queries"] == 1 and decide["dialogue_lines"] == 1
    search = of(events, "memory_advisor_search_timing")[0]
    assert search["ms"] >= 15 and search["chars"] == len("Nick Jiang is a distributor.") and search["query"] == "who is Nick"
    done = of(events, "memory_advisor_searched")[0]
    assert done["search_ms"] >= 15 and done["ms"] >= done["search_ms"]


def test_a_search_that_times_out_is_timed_too(events, monkeypatch):
    async def slow(query):
        await asyncio.sleep(1)

    monkeypatch.setattr("app.plugins.memory_plugin._run_memory_query", slow)
    monkeypatch.setattr(memory_advisor, "SEARCH_TIMEOUT_S", 0.05)
    assert asyncio.run(memory_advisor._search("q")) is None
    failed = of(events, "memory_advisor_search_failed")[0]
    assert failed["error_type"] == "TimeoutError" and failed["ms"] >= 40 and failed["query"] == "q"
