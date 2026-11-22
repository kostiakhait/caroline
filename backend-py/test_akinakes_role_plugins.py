"""The akinakes role plugins: project state and bureau decisions stored exactly as the old secretaries stored them
(app/plugins/project_state_plugin.py), against an in-memory Document Service; and the plugins that need a service
are not loaded without it.
Run: python -m pytest test_akinakes_role_plugins.py -q"""

import asyncio
import importlib
import json

import pytest

pytest.importorskip("claude_agent_sdk")
from app.plugins import project_state_plugin as ps  # noqa: E402


class FakeDS:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    async def call(self, method, path, *, params=None, body=None):
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/documents":
            tags = set((params or {}).get("tags", "").split(",")) - {""}
            pid = (params or {}).get("project_id")
            return [d for d in self.docs.values() if tags <= set(d["tags"]) and (pid is None or d.get("project_id") == pid)]
        if method == "POST" and path == "/documents":
            doc = {**body, "id": f"d{len(self.docs) + 1}", "sections": []}
            self.docs[doc["id"]] = doc
            return doc
        if method == "GET" and len(parts) == 2:
            return self.docs[parts[1]]
        if method == "POST" and parts[2:] == ["sections"]:
            section = {**body, "id": f"s{len(self.docs[parts[1]]['sections']) + 1}"}
            self.docs[parts[1]]["sections"].append(section)
            return section
        if method == "PATCH" and parts[2] == "sections":
            section = next(s for s in self.docs[parts[1]]["sections"] if s["id"] == parts[3])
            section["content"] = body["content"]
            return section
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def ds(monkeypatch):
    fake = FakeDS()
    monkeypatch.setattr(ps, "_call", fake.call)
    return fake


def run(coro):
    return asyncio.run(coro)


def state(ds, project_id):
    doc = next(d for d in ds.docs.values() if d.get("project_id") == project_id)
    return json.loads(doc["sections"][0]["content"])


def test_project_state_is_kept_as_the_old_secretary_kept_it(ds):
    run(ps.project_add_objective({"project_id": "p1", "objective": "Ship v2"}, None))
    run(ps.project_add_decision({"project_id": "p1", "decision": "Use LoRa", "chain_id": "c7"}, None))
    run(ps.project_assign_task({"project_id": "p1", "task_title": "Order sensors", "assignee": "bob"}, None))
    assert len(ds.docs) == 1
    doc = next(iter(ds.docs.values()))
    assert doc["type"] == "project-state" and doc["tags"] == ["project-state"] and len(doc["sections"]) == 1
    assert state(ds, "p1") == {
        "objectives": [{"text": "Ship v2", "set_by": "user"}],
        "decisions": [{"text": "Use LoRa", "decided_by": "user", "chain_id": "c7"}],
        "tasks": [{"title": "Order sensors", "assignee": "bob", "status": "assigned", "chain_id": None}],
        "status": "active",
    }
    got = json.loads(run(ps.project_state_get({"project_id": "p1"}, None))["text"])
    assert got["doc_id"] == doc["id"] and got["state"]["objectives"][0]["text"] == "Ship v2"
    assert run(ps.project_add_objective({"objective": "x"}, None))["is_error"]


def test_set_objective_replaces_the_earlier_ones(ds):
    run(ps.project_add_objective({"project_id": "p1", "objective": "Ship v2"}, None))
    run(ps.project_add_objective({"project_id": "p1", "objective": "Ship v2"}, None))
    run(ps.project_add_decision({"project_id": "p1", "decision": "Use LoRa"}, None))
    run(ps.project_set_objective({"project_id": "p1", "objective": "Ship v3", "set_by": "ann"}, None))
    assert state(ds, "p1")["objectives"] == [{"text": "Ship v3", "set_by": "ann"}]
    assert state(ds, "p1")["decisions"][0]["text"] == "Use LoRa"
    assert run(ps.project_set_objective({"objective": "x"}, None))["is_error"]


def test_bureau_decisions_and_summaries(ds):
    run(ps.bureau_capture_decision({"bureau_id": "b1", "decision": "We publish the paper in May, after the review",
                                    "title": "Publish in May", "context": "after review"}, None))
    run(ps.bureau_capture_decision({"bureau_id": "b1", "decision": "Ann is the second author", "title": "Second author: Ann"}, None))
    decisions = json.loads(run(ps.bureau_list_decisions({"bureau_id": "b1"}, None))["text"])["decisions"]
    assert [d["title"] for d in decisions] == ["Publish in May", "Second author: Ann"]
    # no title: refused, nothing cut out of the decision text
    assert run(ps.bureau_capture_decision({"bureau_id": "b1", "decision": "Untitled"}, None))["is_error"]
    assert "*Context:* after review" in decisions[0]["content"]
    summary = json.loads(run(ps.bureau_add_summary({"bureau_id": "b1", "summary": "We agreed."}, None))["text"])
    doc = ds.docs[summary["doc_id"]]
    assert doc["type"] == "bureau-summary" and doc["tags"] == ["bureau-summary", "b1"] and doc["sections"][0]["content"] == "We agreed."
    assert run(ps.bureau_capture_decision({"bureau_id": "b1"}, None))["is_error"]


@pytest.mark.parametrize("module, env", [
    ("app.plugins.document_write_plugin", "DOCUMENT_SERVICE_URL"),
    ("app.plugins.experiment_service_plugin", "EXPERIMENT_SERVICE_URL"),
    ("app.plugins.research_corpus_plugin", "AKINAKES_RESEARCH_DIR"),
])
def test_a_service_plugin_is_not_loaded_without_its_service(monkeypatch, module, env):
    monkeypatch.delenv(env, raising=False)
    assert importlib.reload(importlib.import_module(module)).PLUGIN is None
    monkeypatch.setenv(env, "http://svc")
    assert importlib.reload(importlib.import_module(module)).PLUGIN is not None


def test_file_plugin_only_for_the_document_creator(monkeypatch):
    from app.plugins import akinakes_file_plugin as fp
    monkeypatch.setenv("DOCUMENT_SERVICE_URL", "http://svc")
    monkeypatch.delenv("AGENT_ROLE", raising=False)
    assert importlib.reload(fp).PLUGIN is None
    monkeypatch.setenv("AGENT_ROLE", "writer")
    assert importlib.reload(fp).PLUGIN is None
    monkeypatch.setenv("AGENT_ROLE", "doc_creator")
    assert [t.name for t in importlib.reload(fp).PLUGIN.tools] == ["document_render_file"]
    monkeypatch.delenv("DOCUMENT_SERVICE_URL")
    assert importlib.reload(fp).PLUGIN is None


def test_render_file_exports_then_stores(monkeypatch):
    import httpx
    from app.plugins import akinakes_file_plugin as fp
    monkeypatch.setenv("DOCUMENT_SERVICE_URL", "http://svc")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params)))
        return httpx.Response(200, content=b"PK-file") if request.method == "GET" else httpx.Response(200, json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(fp.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    got = json.loads(run(fp.document_render_file({"doc_id": "d1", "format": "docx"}, None))["text"])
    assert got == {"doc_id": "d1", "format": "docx", "bytes": 7}
    assert seen == [("GET", "/documents/d1/export", {"format": "docx"}), ("POST", "/documents/d1/file", {})]
    assert run(fp.document_render_file({"doc_id": "d1", "format": "md"}, None))["is_error"]
