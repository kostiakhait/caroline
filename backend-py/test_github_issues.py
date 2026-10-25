"""Regressions for the GitHub issues fixed 2026-10-24 (kostiakhait/caroline #3-#9,
#12 -- the backend-py parts). Fakes and temporary directories only."""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

import app.plugins.scheduler_plugin as sp


def run(coro):
    return asyncio.run(coro)


# ---- #5: a malformed reminder date never blocks the valid ones -----------

@pytest.fixture
def schedule(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "WORKSPACE_DIR", str(tmp_path))
    return tmp_path / "schedule.json"


def _one_tick(on_due):
    async def go():
        task = sp.start_due_check_loop(on_due, interval_s=3600)
        await asyncio.sleep(0.05)
        task.cancel()
    run(go())


def test_an_invalid_date_is_refused_before_it_is_saved(schedule):
    result = run(sp.schedule_reminder({"due_at_iso": "not-a-date", "note": "x"}, None))
    assert result.get("is_error")
    assert not schedule.exists() or json.loads(schedule.read_text(encoding="utf-8")) == []


def test_a_broken_saved_row_is_set_aside_and_the_valid_ones_fire(schedule):
    past = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat()
    schedule.write_text(json.dumps([
        {"id": "bad", "dueAtIso": "not-a-date", "note": "x", "fired": False},
        {"id": "good", "dueAtIso": past, "note": "y", "fired": False},
    ]), encoding="utf-8")
    delivered = []
    _one_tick(lambda r: delivered.append(r["id"]) or True)
    assert delivered == ["good"]
    rows = {r["id"]: r for r in json.loads(schedule.read_text(encoding="utf-8"))}
    assert rows["good"]["fired"] is True
    assert rows["bad"]["invalidDue"]
    delivered.clear()
    _one_tick(lambda r: delivered.append(r["id"]) or True)
    assert delivered == []   # nothing fires twice, the broken row stays aside


def test_a_bad_recurring_spec_is_an_error_not_a_crash(schedule):
    result = run(sp.schedule_reminder({"due_at_iso": "nope", "note": "x", "recurring": "daily"}, None))
    assert result.get("is_error")


# ---- #6: a failed Office read-back keeps the server copy and says so -----

import base64 as _b64

import app.plugins.office_editor as oe


def _office(monkeypatch, read_answer):
    calls = []

    async def call(command, payload):
        calls.append(command)
        if command == "file:read":
            if isinstance(read_answer, Exception):
                raise read_answer
            return read_answer
        return {}

    monkeypatch.setattr(oe, "reforce_call", call)
    return calls


def test_an_error_answer_keeps_the_remote_copy_and_the_local_file(tmp_path, monkeypatch):
    local = tmp_path / "doc.docx"
    local.write_bytes(b"original")
    calls = _office(monkeypatch, oe.ReforceError("Temporary read failure"))
    error = run(oe.finish_office_edit_session("remote/doc.docx", str(local)))
    assert error and "Temporary read failure" in error
    assert "file:delete" not in calls
    assert local.read_bytes() == b"original"


def test_no_content_is_a_failure_not_an_empty_file(tmp_path, monkeypatch):
    local = tmp_path / "doc.docx"
    local.write_bytes(b"original")
    calls = _office(monkeypatch, {".status": "ok"})
    assert run(oe.finish_office_edit_session("remote/doc.docx", str(local)))
    assert "file:delete" not in calls and local.read_bytes() == b"original"


def test_a_good_read_back_writes_then_deletes(tmp_path, monkeypatch):
    local = tmp_path / "doc.docx"
    local.write_bytes(b"original")
    calls = _office(monkeypatch, {"content": _b64.b64encode(b"edited").decode()})
    assert run(oe.finish_office_edit_session("remote/doc.docx", str(local))) is None
    assert local.read_bytes() == b"edited" and calls[-1] == "file:delete"
