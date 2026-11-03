"""Method memory (app/method_memory.py) and its microagent's check.
Run: python -m pytest test_method_memory.py -q"""

import asyncio

import pytest

from app import method_memory
from app.microagents import sw_method_extract, sw_memory_need


def run(coro):
    return asyncio.run(coro)


TRACE = [
    "- list_my_windows({}) -> [chatgpt window]",
    "- app_browser_type({'text': 'find emails'}) -> ok",
    "- request_info({'query': 'x'}) -> nothing",
]


@pytest.fixture
def env(monkeypatch):
    state = {"agent": {"status": "ok", "output": {"worth_saving": True, "method": "To ask ChatGPT: use its open browser window."}},
             "saved": [], "agent_inputs": []}

    async def agent(name, module, agent_input):
        state["agent_inputs"].append(agent_input)
        if isinstance(state["agent"], Exception):
            raise state["agent"]
        return state["agent"]

    async def save(text, tab_id):
        state["saved"].append(text)
        return True

    monkeypatch.setattr("app.plugins.voice_api._run_local_microagent", agent)
    monkeypatch.setattr("app.plugins.memory_plugin.save_fact_from_code", save)
    return state


def test_a_worthwhile_method_is_saved_and_memory_calls_are_not_part_of_the_work(env):
    assert run(method_memory.remember_method("ask ChatGPT", TRACE, "done", "t")) is True
    assert env["saved"] == ["To ask ChatGPT: use its open browser window."]
    assert len(env["agent_inputs"][0]["trace"]) == 2


def test_a_one_step_job_costs_no_model_call(env):
    assert run(method_memory.remember_method("read a file", TRACE[:1], "done", "t")) is False
    assert env["agent_inputs"] == [] and env["saved"] == []


def test_a_routine_turn_is_not_saved(env):
    env["agent"] = {"status": "ok", "output": {"worth_saving": False, "method": ""}}
    assert run(method_memory.remember_method("x", TRACE, "done", "t")) is False and env["saved"] == []


def test_failures_of_the_agent_never_raise(env):
    env["agent"] = RuntimeError("down")
    assert run(method_memory.remember_method("x", TRACE, "done", "t")) is False
    env["agent"] = None
    assert run(method_memory.remember_method("x", TRACE, "done", "t")) is False


def test_the_agent_answer_check():
    ok = {"worth_saving": True, "method": "To do X: do Y."}
    assert sw_method_extract.verify_method_extract({}, ok) is None
    assert sw_method_extract.verify_method_extract({}, {"worth_saving": False, "method": ""}) is None
    assert sw_method_extract.verify_method_extract({}, {**ok, "method": ""}) is not None
    assert sw_method_extract.verify_method_extract({}, {"worth_saving": False, "method": "x"}) is not None
    assert sw_method_extract.verify_method_extract({}, {**ok, "method": "x" * 401}) is not None
    assert sw_method_extract.verify_method_extract({}, {"worth_saving": "yes", "method": "x"}) is not None


def test_the_prompts_build():
    assert "OWNER'S REQUEST" in sw_method_extract.prompt_method_extract({"request": "r", "trace": TRACE, "delivered": "d"}, None)
    assert "how I did" in sw_memory_need.prompt_memory_need({"message": "m", "dialogue": []}, None)
