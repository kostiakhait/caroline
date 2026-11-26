"""The library advisor (app/library_advisor.py) and its microagent's check.
Run: python -m pytest test_library_advisor.py -q"""

import asyncio

import pytest

from app import library_advisor
from app.microagents import sw_library_need


def run(coro):
    return asyncio.run(coro)


ASPHALT = {"id": "u-46a", "type": "texture", "rubric": "surfaces/textures/ground", "versions": ["1.0.0"],
           "description": "Texture set. Aged road asphalt, dark grey."}
SKY = {"id": "u-sky", "type": "hdri", "rubric": "sky", "versions": ["1.0.0", "1.1.0"], "description": "Overcast sky."}


@pytest.fixture
def env(monkeypatch):
    state = {"agent": {"status": "ok", "output": {"needs_library": True, "why": "needs a road",
                                                  "queries": ["asphalt road texture", "overcast sky hdri"]}},
             "searches": [], "cards": {"asphalt road texture": [ASPHALT], "overcast sky hdri": [SKY, ASPHALT]}, "logged_in": True}

    async def agent(name, module, agent_input):
        if isinstance(state["agent"], Exception):
            raise state["agent"]
        return state["agent"]

    async def search(query):
        state["searches"].append(query)
        return state["cards"].get(query)

    monkeypatch.setenv("CAROLINE_FERRET_EDITOR", "1")
    monkeypatch.setattr("app.plugins.voice_api._run_local_microagent", agent)
    monkeypatch.setattr(library_advisor, "_search", search)
    monkeypatch.setattr("app.login_api.is_logged_in", lambda: state["logged_in"])
    return state


def test_the_needed_things_come_back_as_cards_with_id_and_latest_version(env):
    block = run(library_advisor.library_block("Make a race track", [], "t"))
    assert block.startswith(library_advisor.BLOCK_HEADER)
    assert "u-46a@1.0.0" in block and "u-sky@1.1.0" in block
    assert env["searches"] == ["asphalt road texture", "overcast sky hdri"]


def test_a_card_found_by_two_queries_is_listed_once(env):
    block = run(library_advisor.library_block("Make a race track", [], "t"))
    assert block.count("u-46a@1.0.0") == 1


def test_nothing_needed_nothing_glued(env):
    env["agent"] = {"status": "ok", "output": {"needs_library": False, "why": "thanks", "queries": []}}
    assert run(library_advisor.library_block("Thanks!", [], "t")) == "" and env["searches"] == []


def test_failures_leave_the_message_alone(env):
    env["agent"] = RuntimeError("down")
    assert run(library_advisor.library_block("Make a road", [], "t")) == ""
    env["agent"] = {"status": "ok", "output": {"needs_library": True, "why": "x", "queries": ["unknown"]}}
    assert run(library_advisor.library_block("Make a road", [], "t")) == ""   # the search itself failed


def test_an_empty_result_is_said_so(env):
    env["agent"] = {"status": "ok", "output": {"needs_library": True, "why": "x", "queries": ["unicorn model"]}}
    env["cards"]["unicorn model"] = []
    assert "nothing" in run(library_advisor.library_block("A unicorn", [], "t"))


def test_only_in_the_constructor_and_when_logged_in(env, monkeypatch):
    monkeypatch.delenv("CAROLINE_FERRET_EDITOR")
    assert run(library_advisor.library_block("Make a road", [], "t")) == "" and env["searches"] == []
    monkeypatch.setenv("CAROLINE_FERRET_EDITOR", "1")
    env["logged_in"] = False
    assert run(library_advisor.library_block("Make a road", [], "t")) == ""


def test_the_block_is_capped(env):
    env["cards"]["asphalt road texture"] = [dict(ASPHALT, id=f"u-{i}", description="x" * 400) for i in range(4)]
    env["agent"]["output"]["queries"] = ["asphalt road texture"] * 5
    assert len(run(library_advisor.library_block("Road", [], "t"))) <= library_advisor.MAX_BLOCK_CHARS


def test_the_microagent_check():
    ok = {"needs_library": True, "why": "w", "queries": ["a road texture"]}
    assert sw_library_need.verify_library_need({}, ok) is None
    assert sw_library_need.verify_library_need({}, dict(ok, queries=[]))
    assert sw_library_need.verify_library_need({}, dict(ok, queries=["q"] * 6))
    assert sw_library_need.verify_library_need({}, dict(ok, needs_library="yes"))
    assert sw_library_need.verify_library_need({}, {"needs_library": False, "why": "w", "queries": []}) is None
