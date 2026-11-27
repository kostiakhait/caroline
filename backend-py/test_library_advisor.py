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
                                                  "queries": ["asphalt", "sky"]}},
             "searches": [], "cards": {"asphalt": ([ASPHALT], 1), "sky": ([SKY, ASPHALT], 2)}, "logged_in": True}

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
    assert env["searches"] == ["asphalt", "sky"]


def test_a_card_found_by_two_queries_is_listed_once(env):
    block = run(library_advisor.library_block("Make a race track", [], "t"))
    assert block.count("u-46a@1.0.0") == 1


def test_every_thing_is_searched_and_every_card_shown_whole(env):
    many = [f"thing {i}" for i in range(25)]
    env["agent"]["output"]["queries"] = many
    long = dict(ASPHALT, description="x" * 3000)
    for i, q in enumerate(many):
        env["cards"][q] = ([dict(long, id=f"u-{i}")], 1)
    block = run(library_advisor.library_block("A big scene", [], "t"))
    assert env["searches"] == many
    assert all(f"u-{i}@1.0.0" in block for i in range(25))
    assert "x" * 3000 in block


def test_more_matches_than_the_first_page_say_how_to_page_on(env):
    env["cards"]["asphalt"] = ([ASPHALT], 57)
    block = run(library_advisor.library_block("Make a road", [], "t"))
    assert '57 match in all' in block and 'library_search {q: "asphalt", offset: 1}' in block


def test_nothing_needed_nothing_glued(env):
    env["agent"] = {"status": "ok", "output": {"needs_library": False, "why": "thanks", "queries": []}}
    assert run(library_advisor.library_block("Thanks!", [], "t")) == "" and env["searches"] == []


def test_failures_leave_the_message_alone(env):
    env["agent"] = RuntimeError("down")
    assert run(library_advisor.library_block("Make a road", [], "t")) == ""
    env["agent"] = {"status": "ok", "output": {"needs_library": True, "why": "x", "queries": ["unknown"]}}
    assert run(library_advisor.library_block("Make a road", [], "t")) == ""   # the search itself failed


def test_an_empty_result_is_said_so(env):
    env["agent"] = {"status": "ok", "output": {"needs_library": True, "why": "x", "queries": ["unicorn"]}}
    env["cards"]["unicorn"] = ([], 0)
    assert "nothing in the library" in run(library_advisor.library_block("A unicorn", [], "t"))


def test_only_in_the_constructor_and_when_logged_in(env, monkeypatch):
    monkeypatch.delenv("CAROLINE_FERRET_EDITOR")
    assert run(library_advisor.library_block("Make a road", [], "t")) == "" and env["searches"] == []
    monkeypatch.setenv("CAROLINE_FERRET_EDITOR", "1")
    env["logged_in"] = False
    assert run(library_advisor.library_block("Make a road", [], "t")) == ""


def test_the_microagent_check():
    ok = {"needs_library": True, "why": "w", "queries": ["a road texture"]}
    assert sw_library_need.verify_library_need({}, ok) is None
    assert sw_library_need.verify_library_need({}, dict(ok, queries=["q"] * 40)) is None    # no limit on how many things
    assert sw_library_need.verify_library_need({}, dict(ok, queries=[]))
    assert sw_library_need.verify_library_need({}, dict(ok, needs_library="yes"))
    assert sw_library_need.verify_library_need({}, {"needs_library": False, "why": "w", "queries": []}) is None


def test_a_download_from_the_web_is_not_held_back_by_the_library(env, monkeypatch):
    from app.plugins import ferret_editor_plugin as plugin
    fetched = []

    class NoNet:
        def __init__(self, *a, **k): fetched.append(1); raise OSError("no network in tests")
    monkeypatch.setattr("httpx.AsyncClient", NoNet)
    run(plugin._project_import_url({"url": "https://dl.polyhaven.org/file/asphalt_02_diff_4k.jpg", "to": "assets/"}, None))
    assert fetched == [1] and env["searches"] == []
