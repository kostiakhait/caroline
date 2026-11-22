"""Track routing (app/microagents/track_route.py, memory_advisor.route_block). Run: python -m pytest test_track_route.py -q"""

import asyncio

from app import memory_advisor, memory_topics, track_importance as ti
from app.microagents import track_route as tr

INPUT = {"tracks": [{"id": "a", "description": "Anita session"}, {"id": "v", "description": "Mail monitoring"}]}


def test_verify_accepts_only_known_tracks_and_sane_values():
    ok = {"track_id": "a", "matter": "the session with Anita", "explicit": True, "importance": 0}
    assert tr.verify_track_route(INPUT, ok) is None
    assert tr.verify_track_route(INPUT, {**ok, "track_id": ""}) is None
    assert tr.verify_track_route(INPUT, {**ok, "track_id": "zzz"})
    assert tr.verify_track_route(INPUT, {**ok, "matter": " "})
    assert tr.verify_track_route(INPUT, {**ok, "importance": 1.5})
    assert tr.verify_track_route(INPUT, {**ok, "explicit": "yes"})


def test_the_prompt_lists_tracks_with_importance():
    text = tr.prompt_track_route({"message": "m", "tracks": [{"id": "a", "description": "Anita", "importance": 0.8}], "current_id": "a"}, None)
    assert "id: a -- Anita -- importance 0.80" in text and "LAST WORKED ON IN THIS TAB: a" in text


def test_route_block_bumps_importance_sets_current_and_tells_the_engine(tmp_path, monkeypatch):
    ws = str(tmp_path)
    monkeypatch.setattr("app.workspace_dir.WORKSPACE_DIR", ws)
    memory_topics.update_topic(ws, {"id": "a", "description": "Anita session", "status": "open", "last_episode_at": "2099-01-01T00:00:00Z"})
    ti.set_initial(ws, "a", 0.4)

    async def fake(agent, module, payload):
        return {"status": "ok", "output": {"track_id": "a", "matter": "the session with Anita", "explicit": True, "importance": 0}}

    monkeypatch.setattr("app.plugins.voice_api._run_local_microagent", fake)
    block = asyncio.run(memory_advisor.route_block("check that the reply went out", [], "1"))
    assert 'about "the session with Anita"' in block and "Answer ONLY about this matter" in block
    assert "cover it fully" in block   # an explicit instruction lifted the track to full detail
    assert ti.get(ws, "a") >= ti.EXPLICIT_FLOOR - 0.01
    assert memory_topics.get_current_topic(ws, "1") == "a"


def test_a_new_matter_waits_for_its_track(tmp_path, monkeypatch):
    ws = str(tmp_path)
    monkeypatch.setattr("app.workspace_dir.WORKSPACE_DIR", ws)

    async def fake(agent, module, payload):
        return {"status": "ok", "output": {"track_id": "", "matter": "the vacancy", "explicit": False, "importance": 0.6}}

    monkeypatch.setattr("app.plugins.voice_api._run_local_microagent", fake)
    block = asyncio.run(memory_advisor.route_block("interesting vacancy", [], "7"))
    assert "NEW matter" in block and ti.pending_new["7"] == 0.6


def test_a_failed_router_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.workspace_dir.WORKSPACE_DIR", str(tmp_path))

    async def fake(agent, module, payload):
        raise RuntimeError("down")

    monkeypatch.setattr("app.plugins.voice_api._run_local_microagent", fake)
    assert asyncio.run(memory_advisor.route_block("x", [], "1")) == ""
