"""Agentic web search (app/web_research.py, app/web_browser.py, the web_research
plugin) with a fake browser and fake agents -- no network, no model, no
Chromium. Run with Caroline's bundled Python (the plugin loader imports
claude_agent_sdk)."""

import asyncio
import base64

import pytest

from app import web_browser, web_research
from app.plugins import web_research_plugin
from app.web_browser import Page, SearchResult, WebBrowserError


def run(coro):
    return asyncio.run(coro)


def test_a_quote_counts_only_when_it_is_in_the_page():
    page = "Roald Amundsen reached the South Pole\n on 14 December 1911."
    assert web_research.quote_in_text("reached the south pole on 14 December 1911", page)
    assert web_research.quote_in_text('"Roald Amundsen reached the South Pole."', page)
    assert not web_research.quote_in_text("reached the South Pole in 1912", page)
    assert not web_research.quote_in_text("   ", page)


def test_long_text_is_cut_into_parts_at_paragraphs():
    text = ("x" * 900 + "\n") * 30
    parts = web_research.chunks_of(text, size=2000)
    assert all(len(p) <= 2000 for p in parts) and "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_result_links_are_unwrapped():
    target = "https://example.org/a?b=1"
    bing = "https://www.bing.com/ck/a?u=a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    assert web_browser._real_url("bing", bing) == target
    assert web_browser._real_url("duckduckgo", "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa%3Fb%3D1") == target
    assert web_browser._real_url("google", "https://www.google.com/url?q=https://example.org/a%3Fb%3D1") == target
    assert web_browser._real_url("yandex", target) == target


def test_bing_and_yandex_are_deferred():
    assert web_browser.ENGINES == ("google", "duckduckgo")
    with pytest.raises(WebBrowserError, match="deferred"):
        run(web_browser.search("yandex", "q", 10))


def test_no_time_left_is_an_error_not_an_endless_wait():
    with pytest.raises(WebBrowserError):
        web_browser._require_time(0)


PAGES = {
    "https://a.example/pole": "Roald Amundsen reached the South Pole on 14 December 1911. He led a Norwegian team.",
    "https://b.example/pole": "The Norwegian expedition arrived first, on 14 December 1911.",
}


class FakeAgents:
    calls: list = []
    judge_says_enough = True

    def __init__(self):
        FakeAgents.calls = []

    async def run(self, name, payload, timeout_s):
        FakeAgents.calls.append(name)
        if name == "web.plan":
            if payload["tried"]:
                return {}
            return {"queries": [{"query": "who reached the South Pole first", "engines": ["google", "duckduckgo"]}]}
        if name == "web.pick":
            return {"open": [r["label"] for r in payload["results"]]}
        if name == "web.extract":
            if "a.example" in payload["url"]:
                return {"facts": [
                    {"statement": "Amundsen reached the South Pole on 14 December 1911", "quote": "Roald Amundsen reached the South Pole on 14 December 1911"},
                    {"statement": "Amundsen used dogs", "quote": "He travelled with 52 dogs"},          # not in the page
                ], "follow": []}
            return {"facts": [{"statement": "The Norwegians arrived first", "quote": "The Norwegian expedition arrived first"}], "follow": []}
        if name == "web.judge":
            return {"enough": True, "missing": ""} if FakeAgents.judge_says_enough else {"enough": False, "missing": "the date"}
        if name == "web.answer":
            return {"answer": "Roald Amundsen, on 14 December 1911 [1][2].", "used": ["1", "2"]}
        raise AssertionError(name)


@pytest.fixture
def fake_web(monkeypatch):
    searched = []

    async def search(engine, query, timeout_s):
        searched.append(engine)
        if engine == "google":
            raise WebBrowserError("google gave nothing")
        return [SearchResult(title=u, url=u, snippet="", engine=engine, query=query) for u in PAGES]

    async def read(url, timeout_s):
        return Page(url=url, title=url, text=PAGES[url])

    monkeypatch.setattr(web_browser, "search", search)
    monkeypatch.setattr(web_browser, "read", read)
    monkeypatch.setattr(web_research, "_Agents", FakeAgents)
    FakeAgents.judge_says_enough = True
    return searched


def test_research_keeps_only_quoted_facts_and_answers_with_them(fake_web):
    result = run(web_research.research("Who reached the South Pole first?"))
    assert result.answer.startswith("Roald Amundsen")
    assert [f.url for f in result.facts] == ["https://a.example/pole", "https://b.example/pole"]
    assert result.all_facts == 2                       # the dog fact had no real quote and was dropped
    assert result.stopped_because == "the facts answer the question" and not result.incomplete
    assert result.pages_read == 2
    assert sorted(fake_web) == ["duckduckgo", "google"]    # both planned engines, at once


def test_research_stops_when_a_round_adds_nothing(fake_web):
    FakeAgents.judge_says_enough = False
    result = run(web_research.research("Who reached the South Pole first?"))
    assert result.stopped_because == "the last round found no new facts"
    assert "web.answer" in FakeAgents.calls


def test_research_stops_at_the_time_limit(fake_web, monkeypatch):
    async def slow_read(url, timeout_s):
        await asyncio.sleep(5)
        return Page(url=url, title=url, text=PAGES[url])

    monkeypatch.setattr(web_browser, "read", slow_read)
    monkeypatch.setattr(web_research, "RESEARCH_TIME_LIMIT_S", 0.5)
    result = run(web_research.research("Who reached the South Pole first?"))
    assert result.incomplete and result.stopped_because == "time limit" and result.answer == ""


def test_find_falls_back_to_another_engine_when_the_chosen_ones_fail(fake_web):
    results, used = run(web_research.find("q", ["google"], 10))
    assert used == ["duckduckgo"] and len(results) == 2


def test_web_find_says_which_engine_answered(fake_web):
    text = run(web_research_plugin.web_find_tool({"query": "q", "engine": "google"}, None))["text"]
    assert "google failed, these are from duckduckgo" in text and "https://a.example/pole" in text


def test_web_research_tool_lists_sources_as_page_data(fake_web):
    text = run(web_research_plugin.web_research_tool({"question": "Who reached the South Pole first?"}, None))["text"]
    assert "<<<PAGES" in text and "Sources:" in text and '"Roald Amundsen reached the South Pole on 14 December 1911"' in text
