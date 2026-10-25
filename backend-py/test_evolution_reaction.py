"""The user's reaction for the evolution (2026-10-24): scored on a real message,
sent as the reaction to the PREVIOUS turn (app/memory_turns.previous_turn_id,
permission_guard._check_reaction_score, evolution_client.send_reaction), and the
client's roles resolved with the tab's current turn id. Fakes only."""

import asyncio

import app.evolution_client as ec
from app import memory_turns
from app.chat_session_mixins import permission_guard as pg


def run(coro):
    return asyncio.run(coro)


def test_a_new_turn_remembers_the_one_it_replaced():
    tab = "reaction-test-tab"
    assert memory_turns.previous_turn_id(tab) is None
    first = memory_turns.start_turn(tab, "hi")
    second = memory_turns.start_service_turn(tab)
    assert memory_turns.previous_turn_id(tab) == first
    memory_turns.start_turn(tab, "again")
    assert memory_turns.previous_turn_id(tab) == second


class FakeRegistry:
    def open_actions(self):
        return []


class FakeSession:
    def __init__(self):
        self.tab_id, self.workspace_dir, self.action_registry = "1", "w", FakeRegistry()


def _mixin_method():
    for value in vars(pg).values():
        if isinstance(value, type) and "_check_reaction_score" in vars(value):
            return vars(value)["_check_reaction_score"]
    raise AssertionError("no _check_reaction_score")


def _score(monkeypatch, score):
    import app.memory_topics as topics
    import app.plugins.voice_api as voice
    sent = []

    async def score_reaction(message, open_actions, tracks):
        return {"reaction_score": score}

    async def send_reaction(turn_id, value):
        sent.append((turn_id, value))
        return []

    monkeypatch.setattr(voice, "score_reaction", score_reaction)
    monkeypatch.setattr(ec, "send_reaction", send_reaction)
    monkeypatch.setattr(topics, "get_current_topic", lambda *a: None)
    return sent


def test_the_score_goes_to_the_previous_turn(monkeypatch):
    sent = _score(monkeypatch, -0.6)
    run(_mixin_method()(FakeSession(), "this is wrong", "prev-turn"))
    assert sent == [("prev-turn", -0.6)]


def test_no_real_reaction_goes_as_zero(monkeypatch):
    sent = _score(monkeypatch, 0.1)
    run(_mixin_method()(FakeSession(), "ok next", "prev-turn"))
    assert sent == [("prev-turn", 0.0)]


def test_no_previous_turn_nothing_is_sent(monkeypatch):
    sent = _score(monkeypatch, 0.9)
    run(_mixin_method()(FakeSession(), "great", None))
    assert sent == []


def test_resolve_carries_the_tabs_current_turn(monkeypatch):
    posted = []

    async def post(command, **fields):
        posted.append((command, fields))
        return {"results": [{"mode": "plain"}]}

    monkeypatch.setattr(ec, "_evolution_post", post)
    import app.session_context as sc
    monkeypatch.setattr(sc, "get_tab_id", lambda: "resolve-tab")
    turn_id = memory_turns.start_turn("resolve-tab", "hi")
    run(ec._resolve("reply:review", "reply.review", "2"))
    assert posted[0][1]["turnId"] == turn_id


def test_send_reaction_posts_the_turn_and_score(monkeypatch):
    posted = []

    async def post(command, **fields):
        posted.append((command, fields))
        return {"applied": [{"role": "reply.review", "action": "points"}]}

    monkeypatch.setattr(ec, "_evolution_post", post)
    assert run(ec.send_reaction("t1", 0.5)) == [{"role": "reply.review", "action": "points"}]
    assert posted == [("evolution:reaction", {"turnId": "t1", "score": 0.5})]
