"""Document Service read tools (app/plugins/document_service_plugin.py), against a stubbed HTTP layer.
Run: python -m pytest test_document_service_plugin.py -q"""

import asyncio

import pytest

pytest.importorskip("claude_agent_sdk")

from app.plugins import document_service_plugin as p  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_without_the_url_every_tool_reports_it(monkeypatch):
    monkeypatch.delenv("DOCUMENT_SERVICE_URL", raising=False)
    out = run(p.document_get({"doc_id": "d1"}, None))
    assert out["is_error"] and "DOCUMENT_SERVICE_URL" in out["content"][0]["text"]


def test_outline_has_no_content(monkeypatch):
    async def fake_get(path, params=None):
        return [{"id": "s1", "title": "Intro", "content": "x" * 5000, "synopsis": "about"}]
    monkeypatch.setattr(p, "_get", fake_get)
    out = run(p.document_list_sections({"doc_id": "d1"}, None))
    assert "Intro" in out["text"] and "xxxx" not in out["text"]


def test_long_replies_are_cut(monkeypatch):
    async def fake_get(path, params=None):
        return {"content": "y" * (p.MAX_TEXT_CHARS * 2)}
    monkeypatch.setattr(p, "_get", fake_get)
    assert "(cut;" in run(p.document_get_section({"doc_id": "d1", "section_id": "s1"}, None))["text"]


def test_search_passes_query_and_limit(monkeypatch):
    seen = {}
    async def fake_get(path, params=None):
        seen.update(path=path, params=params)
        return []
    monkeypatch.setattr(p, "_get", fake_get)
    run(p.document_search({"query": "ablation", "limit": 3}, None))
    assert seen == {"path": "/documents/search", "params": {"q": "ablation", "limit": 3}}
