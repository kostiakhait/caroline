"""The memory advisor (app/memory_advisor.py) and its microagent's check.
Run: python -m pytest test_memory_advisor.py -q"""

import asyncio

import pytest

from app import memory_advisor
from app.microagents import sw_memory_need


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(monkeypatch):
    state = {"agent": {"status": "ok", "output": {"needs_memory": True, "why": "refers to the user", "queries": ["owner name"]}},
             "searches": [], "reply": {"text": '{"results": [{"fact": "owner is Konstantin"}]}'}, "logged_in": True}

    async def agent(name, module, agent_input):
        if isinstance(state["agent"], Exception):
            raise state["agent"]
        return state["agent"]

    async def search(query, history=None, expand=True):
        state["searches"].append(query)
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return state["reply"]

    monkeypatch.setattr("app.plugins.voice_api._run_local_microagent", agent)
    monkeypatch.setattr("app.plugins.memory_plugin._run_memory_query", search)
    monkeypatch.setattr("app.login_api.is_logged_in", lambda: state["logged_in"])
    return state


def test_a_message_that_implies_memory_gets_the_search_result_glued_to_it(env):
    block = run(memory_advisor.memory_block("Do you remember me?", [], "t"))
    assert block.startswith(memory_advisor.BLOCK_HEADER) and "owner is Konstantin" in block
    assert env["searches"] == ["owner name"]


def test_a_message_that_needs_nothing_is_left_alone(env):
    env["agent"] = {"status": "ok", "output": {"needs_memory": False, "why": "greeting", "queries": []}}
    assert run(memory_advisor.memory_block("Thanks!", [], "t")) == "" and env["searches"] == []


def test_an_advisor_that_cannot_decide_means_memory_is_needed(env):
    env["agent"] = RuntimeError("down")
    assert "owner is Konstantin" in run(memory_advisor.memory_block("Who am I?", [], "t"))
    assert env["searches"] == ["Who am I?"]


def test_a_failed_or_refused_search_leaves_the_message_unchanged(env):
    env["reply"] = RuntimeError("server down")
    assert run(memory_advisor.memory_block("Who am I?", [], "t")) == ""
    env["reply"] = {"text": "log in first", "is_error": True}
    assert run(memory_advisor.memory_block("Who am I?", [], "t")) == ""


def test_nothing_found_is_said_so_the_model_does_not_assume(env):
    env["reply"] = {"text": 'Memory holds nothing relevant to "owner name".'}
    assert "holds nothing relevant" in run(memory_advisor.memory_block("Who am I?", [], "t"))


def test_without_a_login_or_text_nothing_runs(env):
    env["logged_in"] = False
    assert run(memory_advisor.memory_block("Who am I?", [], "t")) == "" and env["searches"] == []
    env["logged_in"] = True
    assert run(memory_advisor.memory_block("  ", [], "t")) == ""


def test_the_block_is_capped(env):
    env["reply"] = {"text": "x" * 50000}
    assert len(run(memory_advisor.memory_block("Who am I?", [], "t"))) < memory_advisor.MAX_BLOCK_CHARS + 600


def test_the_microagent_check():
    ok = {"needs_memory": True, "why": "w", "queries": ["a"]}
    assert sw_memory_need.verify_memory_need({}, ok) is None
    assert sw_memory_need.verify_memory_need({}, {**ok, "queries": []}) is not None
    assert sw_memory_need.verify_memory_need({}, {**ok, "needs_memory": "yes"}) is not None
    assert sw_memory_need.verify_memory_need({}, {**ok, "queries": ["a"] * 4}) is not None
    assert sw_memory_need.verify_memory_need({}, {"needs_memory": False, "why": "w", "queries": []}) is None
    assert "NEW MESSAGE:\nhi" in sw_memory_need.prompt_memory_need({"message": "hi", "dialogue": []}, None)
