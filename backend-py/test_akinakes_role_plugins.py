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


def test_bureau_decisions_and_summaries(ds):
    run(ps.bureau_capture_decision({"bureau_id": "b1", "decision": "Publish in May", "context": "after review"}, None))
    run(ps.bureau_capture_decision({"bureau_id": "b1", "decision": "Second author: Ann"}, None))
    decisions = json.loads(run(ps.bureau_list_decisions({"bureau_id": "b1"}, None))["text"])["decisions"]
    assert [d["title"] for d in decisions] == ["Publish in May", "Second author: Ann"]
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
