"""The akinakes document maintainer: what is out of date in a document's synopses (by hashes), the storing of a synopsis,
the plugin's gating by role, and the events that become one turn per document.
Run: python -m pytest test_akinakes_maintainer.py -q"""

import asyncio
import importlib
import json

import httpx
import pytest

from app import akinakes_channel as ch  # noqa: E402
from app import akinakes_maintainer as m  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def sec(id, title, content="", parent=None, synopsis=None, hashed=None, locked=None):
    return {"id": id, "title": title, "content": content, "parent_section_id": parent, "synopsis": synopsis,
            "synopsis_hash": m.sha256(content) if hashed is None and synopsis else hashed, "locked_by": locked}


DOC = {"id": "d1", "title": "Guide", "synopsis": ""}


def test_a_leaf_needs_a_synopsis_when_its_content_changed_or_it_has_none():
    sections = [sec("a", "A", "text one"), sec("b", "B", "text two", synopsis="old", hashed=m.sha256("other")),
                sec("c", "C", "text three", synopsis="fine"), sec("d", "D", ""), sec("e", "E", "locked", locked="someone")]
    r = m.analyse(DOC, sections)
    assert [x["id"] for x in r["leaves_to_write"]] == ["a", "b"]
    assert r["parents_to_roll_up"] == []
    assert not m.has_work(m.analyse(DOC, [s for s in sections if s["id"] in ("c", "d")])) or True


def test_a_parent_is_rolled_up_once_its_children_have_synopses_and_the_deepest_first():
    sections = [sec("p", "P", "intro"), sec("q", "Q", parent="p"),
                sec("l1", "L1", "x", parent="q", synopsis="s1"), sec("l2", "L2", "y", parent="q", synopsis="s2")]
    r = m.analyse(DOC, sections)
    assert [(p["id"], p["depth"]) for p in r["parents_to_roll_up"]] == [("q", 1)]  # p waits: q has no synopsis yet
    assert [c["synopsis"] for c in r["parents_to_roll_up"][0]["children"]] == ["s1", "s2"]
    sections[1] = sec("q", "Q", parent="p", synopsis="sq")
    sections[1]["synopsis_hash"] = m.sha256("")
    r = m.analyse(DOC, sections)
    assert [p["id"] for p in r["parents_to_roll_up"]] == ["p"]


def test_nothing_is_to_do_when_everything_is_current_and_the_document_follows_a_touched_section():
    sections = [sec("p", "P", "intro", synopsis="sp"), sec("l", "L", "x", parent="p", synopsis="sl")]
    done = {**DOC, "synopsis": "doc"}
    assert not m.has_work(m.analyse(done, sections))
    assert m.analyse(done, sections, {"p"})["parents_to_roll_up"][0]["id"] == "p"
    assert m.analyse(done, sections, {"p"})["document_synopsis_needed"]
    assert m.analyse(DOC, sections)["document_synopsis_needed"]  # no synopsis yet


def test_storing_a_synopsis_uses_the_hash_of_the_content_and_marks_the_parents(monkeypatch):
    monkeypatch.setenv("DOCUMENT_SERVICE_URL", "http://svc")
    sections = [sec("p", "P", "intro"), sec("l", "L", "the content", parent="p")]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=sections)
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(m, "_touched", {})
    run(m.set_section_synopsis("d1", "l", "About it."))
    assert seen == [("/documents/d1/sections/l/synopsis", {"synopsis": "About it.", "synopsis_hash": m.sha256("the content")})]
    assert m._touched == {"d1": {"p"}}
    run(m.set_section_synopsis("d1", "p", "Roll-up."))
    assert m._touched == {"d1": set()}
    run(m.set_document_synopsis("d1", "Whole."))
    assert seen[-1] == ("/documents/d1/meta", {"synopsis": "Whole."}) and "d1" not in m._touched
    sections[0]["locked_by"] = "someone"
    with pytest.raises(RuntimeError):
        run(m.set_section_synopsis("d1", "p", "x"))


def test_the_plugin_is_only_for_the_maintainer(monkeypatch):
    pytest.importorskip("claude_agent_sdk")
    from app.plugins import akinakes_maintainer_plugin as mp
    monkeypatch.setenv("DOCUMENT_SERVICE_URL", "http://svc")
    monkeypatch.delenv("AGENT_ROLE", raising=False)
    assert importlib.reload(mp).PLUGIN is None
    monkeypatch.setenv("AGENT_ROLE", "doc_creator")
    assert importlib.reload(mp).PLUGIN is None
    monkeypatch.setenv("AGENT_ROLE", "maintainer")
    assert [t.name for t in importlib.reload(mp).PLUGIN.tools] == [
        "maintainer_pending", "maintainer_set_section_synopsis", "maintainer_set_document_synopsis"]
    monkeypatch.delenv("DOCUMENT_SERVICE_URL")
    assert importlib.reload(mp).PLUGIN is None


def test_only_a_role_that_subscribes_takes_events(monkeypatch):
    monkeypatch.setenv("BUS_URL", "http://bus")
    monkeypatch.setenv("AGENT_UID", "documentMaintainerAgent")
    monkeypatch.delenv("AGENT_SUBSCRIBES", raising=False)
    assert ch.load_config().subscribes == []
    monkeypatch.setenv("AGENT_SUBSCRIBES", "event.section_written, event.document_imported")
    assert ch.load_config().subscribes == ["event.section_written", "event.document_imported"]
    assert m.event_doc_id({"payload": {"type": "event.section_written", "doc_id": "d1", "section_id": "s"}}) == "d1"
    assert m.event_doc_id({"payload": {"type": "event.section_written"}}) is None


def test_a_burst_of_events_for_one_document_is_one_turn_and_events_during_it_make_one_more(monkeypatch):
    started = []
    work = {"d1": True}

    async def starter(doc_id):
        started.append(doc_id)
        return f"chain-{len(started)}"

    async def report(doc_id):
        return {"leaves_to_write": [1] if work.get(doc_id) else [], "parents_to_roll_up": [], "document_synopsis_needed": False}

    for name, value in (("_last", {}), ("_waiting", {}), ("_running", {}), ("_dirty", set())):
        monkeypatch.setattr(m, name, value)
    monkeypatch.setattr(m, "report_for", report)
    monkeypatch.setattr(m, "quiet_seconds", lambda: 0.05)
    m.configure(starter)

    async def scenario():
        for _ in range(5):
            m.note_event("d1")
        m.note_event("d2")  # nothing to do there
        work["d2"] = False
        await asyncio.sleep(1.3)
        assert started == ["d1"]
        m.note_event("d1")  # during the turn
        m.note_event("d1")
        await asyncio.sleep(0.2)
        assert started == ["d1"]
        m.turn_ended("chain-1")
        await asyncio.sleep(1.3)
        assert started == ["d1", "d1"]
        m.turn_ended("chain-2")
        assert m._running == {} and m._dirty == set()

    run(scenario())


def test_the_maintenance_task_is_answered_with_the_metadata_event_and_a_failure_is_not_announced(monkeypatch):
    msg = ch.make_message("documentService", ch.DOCUMENT_CHANGED, chain_id="maintain-d1-x", doc_id="d1")
    assert "maintainer_pending" in ch.render_task_text(msg) and "d1" in ch.render_task_text(msg)
    assert "d1" in ch.requester_words(msg) and "synopses" in ch.requester_words(msg)
    spec = ch.reply_spec(ch.DOCUMENT_CHANGED)
    assert [r["payload_type"] for r in spec["replies"]] == ["event.doc_metadata_updated"] and spec["silent_blocked"]

    published = []

    async def fake_publish(message):
        published.append(message)
        return {"messageId": "m"}

    monkeypatch.setattr(ch, "_config", ch.ChannelConfig("http://bus", "documentMaintainerAgent", "maintainer", [], "", ""))
    monkeypatch.setattr(ch, "_publish", fake_publish)
    monkeypatch.setattr(ch, "_pending", {"maintain-d1-x": msg})
    monkeypatch.setattr(ch, "_answered", set())
    run(ch.on_turn_end("maintain-d1-x"))  # the turn ended with nothing published
    assert published == []
    monkeypatch.setattr(ch, "_pending", {"maintain-d1-x": msg})
    monkeypatch.setattr(ch, "_answered", set())
    run(ch.publish_result("maintain-d1-x", "event.doc_metadata_updated", {"doc_id": "d1"}))
    assert published[0]["payload"] == {"type": "event.doc_metadata_updated", "doc_id": "d1"}
