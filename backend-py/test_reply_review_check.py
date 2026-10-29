"""reply.review's sanitizer (app/microagents/reply_review_check.py, wired in
plugins/voice_api.review_reply): a revision that adds what the original did not
say is thrown away. Fake agents only. Run with Caroline's bundled Python."""

import asyncio
import time

import pytest

import app.evolution_client
import app.microagent_client
from app.microagents import reply_review_check
from app.plugins import voice_api

ORIGINAL = "Пароль на ящик — Prague2024#, тот же, что у остальных ящиков."
INVENTED = "Извиняюсь, не могу передавать пароли в этом канале. Используйте безопасный способ."


def run(coro):
    return asyncio.run(coro)


class FakeRuntime:
    def __init__(self, verdict=None, fail=False):
        self.verdict, self.fail, self.calls = verdict, fail, []

    def register(self, agent):
        pass

    def new_context(self, budget=None):
        return None

    def run(self, name, payload, context):
        self.calls.append(payload)
        if self.fail:
            raise RuntimeError("model down")
        return {"status": "ok", "output": self.verdict}


@pytest.fixture
def review(monkeypatch):
    def use(revised, runtime):
        async def fake_run_reply_agent(role, agent_input, **kwargs):
            return {"status": "ok", "output": {"text": revised}}
        monkeypatch.setattr(app.evolution_client, "run_reply_agent", fake_run_reply_agent)
        monkeypatch.setattr(app.microagent_client, "build_local_runtime", lambda: runtime)
        monkeypatch.setattr(app.microagent_client, "new_budget", lambda: None)
        return run(voice_api.review_reply(ORIGINAL, task="Какой пароль на ящик?"))
    return use


def test_an_invented_revision_is_thrown_away_and_the_original_shown(review):
    runtime = FakeRuntime({"invented": True, "what": "a refusal and advice"})
    assert review(INVENTED, runtime) == ORIGINAL
    assert runtime.calls == [{"original": ORIGINAL, "revised": INVENTED}]


def test_a_faithful_revision_stays(review):
    shorter = "Пароль на ящик — Prague2024#."
    assert review(shorter, FakeRuntime({"invented": False, "what": ""})) == shorter


@pytest.mark.parametrize("revised", [ORIGINAL, "[[NO_UPDATE]]"])
def test_nothing_to_check_when_unchanged_or_dropped(review, revised):
    runtime = FakeRuntime({"invented": True, "what": "x"})
    assert review(revised, runtime) == revised and runtime.calls == []


def test_a_failed_check_keeps_the_revision(review):
    assert review(INVENTED, FakeRuntime(fail=True)) == INVENTED


def test_a_check_out_of_the_callers_time_shows_the_original(monkeypatch):
    # The voice narrator gives the whole review 6s; the check gets what the review left.
    # A check that has not answered by then is a review that has not answered: the text
    # goes as it was drafted, never the unchecked rewrite.
    class SlowRuntime(FakeRuntime):
        def run(self, name, payload, context):
            time.sleep(0.5)
            return super().run(name, payload, context)

    async def fake_run_reply_agent(role, agent_input, **kwargs):
        return {"status": "ok", "output": {"text": INVENTED}}
    monkeypatch.setattr(app.evolution_client, "run_reply_agent", fake_run_reply_agent)
    monkeypatch.setattr(app.microagent_client, "build_local_runtime", lambda: SlowRuntime({"invented": False, "what": ""}))
    monkeypatch.setattr(app.microagent_client, "new_budget", lambda: None)
    assert run(voice_api.review_reply(ORIGINAL, task="Какой пароль на ящик?", timeout=0.2)) == ORIGINAL


def test_verify():
    assert reply_review_check.verify_review_check({}, {"invented": "yes"})
    assert reply_review_check.verify_review_check({}, {"invented": True, "what": " "})
    assert reply_review_check.verify_review_check({}, {"invented": False, "what": ""}) is None
    prompt = reply_review_check.prompt_review_check({"original": "A", "revised": "B"}, None)
    assert "ORIGINAL:\n<<<\nA\n>>>" in prompt and "REVISED:\n<<<\nB\n>>>" in prompt
