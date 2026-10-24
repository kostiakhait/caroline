"""The Wikipedia tools (app/plugins/wikipedia_plugin.py) against a fake server --
no network, no session, no money. Run with Caroline's bundled Python."""

import asyncio

import pytest

from app.plugins import wikipedia_plugin as w
from app.plugins.sw_api import SessionExpiredError


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def server(monkeypatch):
    sent = []

    async def call_v2(command, **extra):
        sent.append((command, extra))
        if command == "ai:wikipediaSearch":
            return {".status": "ok", "results": [{"title": "Roald Amundsen", "url": "https://en.wikipedia.org/wiki/Roald_Amundsen",
                                                  "extract": "Norwegian polar explorer."}]}
        if command == "ai:wikipediaArticle":
            if extra["title"] == "Missing":
                return {".status": "ok", "found": False}
            return {".status": "ok", "found": True, "content": "x" * 25_000, "truncated": False, "url": "https://en.wikipedia.org/wiki/T"}
        raise AssertionError(command)

    monkeypatch.setattr(w, "call_v2", call_v2)
    return sent


def test_search_sends_the_language_and_marks_the_text_as_page_data(server):
    text = run(w.wikipedia_search({"query": "Amundsen", "lang": "RU"}, None))["text"]
    assert server == [("ai:wikipediaSearch", {"key": w.CAROLINE_SW_KEY, "query": "Amundsen", "lang": "ru"})]
    assert "<<<PAGES" in text and "Roald Amundsen -- https://en.wikipedia.org/wiki/Roald_Amundsen" in text


def test_read_asks_for_the_whole_article_and_hands_it_out_in_parts(server):
    first = run(w.wikipedia_read({"title": "T"}, None))["text"]
    assert server[0][1]["maxChars"] == w._ARTICLE_MAX_CHARS and server[0][1]["lang"] == "en"
    assert "offset" in first                                   # a continuation hint: the article is longer than one part
    rest = run(w.wikipedia_read({"title": "T", "offset": 20_000}, None))["text"]
    assert "x" * 5_000 in rest


def test_read_says_plainly_when_there_is_no_such_article(server):
    assert "no article titled 'Missing'" in run(w.wikipedia_read({"title": "Missing"}, None))["text"]


class FakeSessions:
    def __init__(self, expire_first=False):
        self.expire_first, self.sessions = expire_first, []

    async def with_session(self, fn):
        try:
            self.sessions.append("s1")
            return await fn("s1")
        except SessionExpiredError:
            self.sessions.append("s2")
            return await fn("s2")


def test_facts_go_under_the_users_session_with_one_attempt_and_the_long_timeout(monkeypatch):
    posted = []

    async def post_json(body, timeout=30.0, retries=3):
        posted.append((body, timeout, retries))
        return {".status": "ok", "facts": [{"fact": "Amundsen reached the South Pole on 14 December 1911",
                                             "where": "Roald Amundsen", "relevance": 0.9}]}

    monkeypatch.setattr(w, "_post_json", post_json)
    monkeypatch.setattr(w, "_sessions", FakeSessions())
    text = run(w.wikipedia_facts({"question": "Who reached the South Pole first?"}, None))["text"]
    body, timeout, retries = posted[0]
    assert body["command"] == "ai:factExtract" and body["session"] == "s1" and body["wikipedia"] is True
    assert timeout == 600.0 and retries == 1
    assert "1. Amundsen reached the South Pole on 14 December 1911 (from: Roald Amundsen) [relevance 0.9]" in text


def test_facts_retry_once_with_a_fresh_session_when_it_expired(monkeypatch):
    answers = [{".status": "error", ".reason": "Unknown or expired session"}, {".status": "ok", "facts": []}]

    async def post_json(body, timeout=30.0, retries=3):
        return answers.pop(0)

    sessions = FakeSessions()
    monkeypatch.setattr(w, "_post_json", post_json)
    monkeypatch.setattr(w, "_sessions", sessions)
    text = run(w.wikipedia_facts({"question": "q"}, None))["text"]
    assert sessions.sessions == ["s1", "s2"] and "found nothing" in text
