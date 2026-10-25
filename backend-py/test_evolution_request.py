"""The evolution judge's request context on the client (app/evolution_request.py,
plugins/memory_plugin._memory, evolution_client.run_reply_agent). Fakes only."""

import asyncio

import pytest

import app.evolution_request as er
from app import session_context


def run(coro):
    return asyncio.run(coro)


class FakeSession:
    def __init__(self, context):
        self.context, self.asked = context, []

    def evolution_request_context(self, lines):
        self.asked.append(lines)
        return self.context


@pytest.fixture
def tab(monkeypatch):
    sessions = {}
    monkeypatch.setattr(er, "get_sessions_getter", lambda: sessions.get)
    monkeypatch.setattr(er, "get_tab_id", lambda: "1")
    return sessions


def test_the_context_comes_from_the_tabs_session_with_the_users_number_of_lines(tab):
    session = FakeSession({"userRequest": "find Olga's phone", "recentDialogue": ["user: find Olga's phone"]})
    tab["1"] = session
    assert er.current_request_context() == {"userRequest": "find Olga's phone", "recentDialogue": ["user: find Olga's phone"]}
    assert session.asked == [5]


def test_no_session_no_context(tab, monkeypatch):
    assert er.current_request_context() == {}
    monkeypatch.setattr(er, "get_tab_id", lambda: None)
    assert er.current_request_context() == {}


def test_a_broken_session_never_breaks_the_call(tab):
    class Broken:
        def evolution_request_context(self, lines):
            raise RuntimeError("boom")
    tab["1"] = Broken()
    assert er.current_request_context() == {}


def test_memory_commands_carry_the_context(tab, monkeypatch):
    from app.plugins import memory_plugin as mp
    tab["1"] = FakeSession({"userRequest": "who is Olga?", "recentDialogue": ["user: who is Olga?"]})
    sent = []

    async def post(body):
        sent.append(body)
        return {"status": "ok"}

    class Sessions:
        async def with_session(self, fn):
            return await fn("s1")

    monkeypatch.setattr(mp, "_post", post)
    monkeypatch.setattr(mp, "_sessions", Sessions())
    monkeypatch.setattr(mp.memory_turns, "current_turn_id", lambda tab_id: None)
    run(mp._memory("memory:requestStart", query="Olga"))
    assert sent[0]["userRequest"] == "who is Olga?" and sent[0]["recentDialogue"] == ["user: who is Olga?"]


def test_the_client_judge_gets_rules_request_and_dialogue(tab, monkeypatch):
    import app.evolution_client as ec
    tab["1"] = FakeSession({"userRequest": "say it in Russian", "recentDialogue": ["user: say it in Russian", "assistant: ok"]})
    judged = []

    async def resolve(api_path, role, version):
        return {"mode": "tournament", "tournamentId": 7,
                "champion": {"agentName": "c", "rulesText": "CHAMPION RULES", "ladderJson": None},
                "challenger": {"agentName": "x", "rulesText": "OTHER RULES", "ladderJson": None}}

    def run_sync(agent, agent_input):
        judged.append(agent_input)
        return {"status": "ok", "output": {"winner": "tie", "reason": "same"}}

    async def post(command, **kwargs):
        return {}

    monkeypatch.setattr(ec, "_resolve", resolve)
    monkeypatch.setattr(ec, "_run_pair_sync", lambda a, b, i: ({"status": "ok", "output": {"text": "1"}}, {"status": "ok", "output": {"text": "2"}}))
    monkeypatch.setattr(ec, "_run_sync", run_sync)
    monkeypatch.setattr(ec, "_evolution_post", post)
    monkeypatch.setattr(ec, "resolve_agent_for_clone", lambda *a, **k: object())
    role = next(iter(ec.EVOLVABLE_ROLES))
    run(ec.run_reply_agent(role, {"text": "hi", "task": "t"}))
    select_input = judged[-1]
    assert select_input["role_rules"] == "CHAMPION RULES"
    assert select_input["user_request"] == "say it in Russian"
    assert select_input["recent_dialogue"] == "user: say it in Russian\nassistant: ok"
