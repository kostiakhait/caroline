"""The memory backup done by code (app/vault_backup.py). Notes is replaced by an in-memory store here.
Run: python -m pytest test_vault_backup.py -q"""

import asyncio
import json

import pytest

from app import vault_backup as vb
from app.plugins import notes_plugin


class FakeNotes:
    def __init__(self):
        self.notes: dict[str, dict] = {}
        self.attachments: list[dict] = []
        self.blobs: dict[str, bytes] = {}
        self.writes: list[str] = []
        self._n = 0

    async def with_session(self, fn):
        return await fn("S")

    async def list_notes(self, session, folder=None, include_deleted=False):
        return [{"id": i, **n} for i, n in self.notes.items() if (folder is None or n["folder"] == folder) and not n["deleted"]]

    async def create_note(self, session, text, folder=None, is_folder_marker=False):
        self._n += 1
        nid = f"n{self._n}"
        self.notes[nid] = {"text": text, "folder": folder or "", "deleted": False}
        self.writes.append(text.split("\n", 1)[0])
        return {"id": nid, **self.notes[nid]}

    async def update_note(self, session, note_id, text=None, folder=None):
        self.notes[note_id]["text"] = text
        self.writes.append(text.split("\n", 1)[0])
        return {"id": note_id, **self.notes[note_id]}

    async def get_note(self, session, note_id):
        return {"id": note_id, **self.notes[note_id]}

    async def list_attachments(self, session, note_id=None):
        return [a for a in self.attachments if note_id is None or a["noteId"] == note_id]

    async def attach_file(self, session, note_id, local_file_path, original_name=None):
        entry = {"filename": f"f{len(self.attachments)}", "originalName": original_name, "noteId": note_id}
        self.attachments.append(entry)
        self.blobs[entry["filename"]] = open(local_file_path, "rb").read()
        return entry

    async def download_attachment(self, session, filename, save_path):
        data = self.blobs[filename]
        open(save_path, "wb").write(data)
        return len(data)


@pytest.fixture
def notes(monkeypatch):
    fake = FakeNotes()
    monkeypatch.setattr(notes_plugin, "_sessions", fake)
    for name in ("list_notes", "create_note", "update_note", "get_note", "list_attachments", "attach_file", "download_attachment"):
        monkeypatch.setattr(notes_plugin, name, getattr(fake, name))
    monkeypatch.setattr(vb, "_account_email", lambda: "owner@example.com")
    return fake


def run(coro):
    return asyncio.run(coro)


def seed(ws):
    (ws / "schedule.json").write_text(json.dumps([
        {"id": "r1", "note": "call mum", "kind": "user"},
        {"id": "old", "note": "backup", "kind": vb.BACKUP_KIND},
    ]), encoding="utf-8")
    lesson = ws / "Skills" / "lessons-learned" / "mail"
    lesson.mkdir(parents=True)
    (lesson / "SKILL.md").write_text("---\nname: mail\n---\nuse the mailbox address only", encoding="utf-8")
    (ws / "dehydrated").mkdir()
    (ws / "dehydrated" / "a.json").write_text("{}", encoding="utf-8")
    (ws / "uploads").mkdir()
    (ws / "uploads" / "b.png").write_bytes(b"png")


def test_backup_copies_reminders_skills_and_files_without_the_old_backup_reminder(tmp_path, notes):
    seed(tmp_path)
    out = run(vb.backup_now(str(tmp_path)))
    assert out["ok"] and out["attached"] == 2
    titles = {n["text"].split("\n", 1)[0] for n in notes.notes.values()}
    assert titles == {vb.BACKUP_NOTE, "vault:skill:mail", vb.FILES_NOTE}
    backup = next(n for n in notes.notes.values() if n["text"].startswith(vb.BACKUP_NOTE + "\n"))
    ids = [r["id"] for r in json.loads(backup["text"].split("\n", 1)[1])["reminders"]]
    assert ids == ["r1"]
    assert {a["originalName"] for a in notes.attachments} == {"dehydrated__a.json", "uploads__b.png"}
    assert all(n["folder"] == vb.VAULT_FOLDER for n in notes.notes.values())


def test_a_second_run_with_nothing_changed_writes_nothing(tmp_path, notes):
    seed(tmp_path)
    run(vb.backup_now(str(tmp_path)))
    notes.writes.clear()
    out = run(vb.backup_now(str(tmp_path)))
    assert out["written"] == [] and out["attached"] == 0 and notes.writes == [] and len(notes.attachments) == 2


def test_a_changed_skill_is_written_again(tmp_path, notes):
    seed(tmp_path)
    run(vb.backup_now(str(tmp_path)))
    (tmp_path / "Skills" / "lessons-learned" / "mail" / "SKILL.md").write_text("new lesson", encoding="utf-8")
    out = run(vb.backup_now(str(tmp_path)))
    assert out["written"] == ["vault:skill:mail"]


def test_restore_on_a_fresh_install_brings_everything_back_and_overwrites_nothing(tmp_path, notes):
    old = tmp_path / "old"
    old.mkdir()
    seed(old)
    run(vb.backup_now(str(old)))
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    (fresh / "schedule.json").write_text(json.dumps([{"id": "r9", "note": "new here"}]), encoding="utf-8")
    out = run(vb.restore_missing(str(fresh)))
    assert out["ok"]
    assert [r["id"] for r in json.loads((fresh / "schedule.json").read_text(encoding="utf-8"))] == ["r9", "r1"]
    assert "use the mailbox address only" in (fresh / "Skills" / "lessons-learned" / "mail" / "SKILL.md").read_text(encoding="utf-8")
    assert (fresh / "dehydrated" / "a.json").read_text(encoding="utf-8") == "{}" and (fresh / "uploads" / "b.png").read_bytes() == b"png"
    # a second restore changes nothing
    assert run(vb.restore_missing(str(fresh)))["restored"] == []


def test_the_old_model_driven_reminder_is_removed_from_the_schedule(tmp_path):
    seed(tmp_path)
    assert vb.remove_old_reminder(str(tmp_path)) is True
    assert [r["id"] for r in json.loads((tmp_path / "schedule.json").read_text(encoding="utf-8"))] == ["r1"]
    assert vb.remove_old_reminder(str(tmp_path)) is False


def test_without_a_login_nothing_is_done(tmp_path, notes, monkeypatch):
    monkeypatch.setattr(vb, "_account_email", lambda: None)
    seed(tmp_path)
    assert run(vb.backup_now(str(tmp_path)))["ok"] is False and notes.notes == {}
    assert run(vb.restore_missing(str(tmp_path)))["ok"] is False


def test_hostile_names_in_notes_never_write_outside_the_workspace(tmp_path, notes):
    ws = tmp_path / "ws"
    ws.mkdir()
    run(notes.create_note("S", "vault:skill:../../evil\nboom", folder=vb.VAULT_FOLDER))
    run(notes.create_note("S", vb.FILES_NOTE, folder=vb.VAULT_FOLDER))
    notes.attachments.append({"filename": "x", "originalName": "uploads__..\\..\\x", "noteId": "n2"})
    notes.attachments.append({"filename": "y", "originalName": "other__file", "noteId": "n2"})
    notes.blobs.update({"x": b"1", "y": b"2"})
    assert run(vb.restore_missing(str(ws)))["restored"] == []
    assert not (tmp_path / "evil").exists() and not list(tmp_path.rglob("x"))
