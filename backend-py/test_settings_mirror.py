"""The settings mirror (app/settings_mirror.py): settings go to Notes, secrets to the vault, and a fresh
install gets them back. Notes is replaced by an in-memory store here.
Run: python -m pytest test_settings_mirror.py -q"""

import asyncio
import json

import pytest

from app import settings_mirror as sm
from app.plugins import notes_plugin


class FakeNotes:
    def __init__(self):
        self.notes: dict[str, dict] = {}
        self.writes: list[str] = []
        self._n = 0

    async def with_session(self, fn):
        return await fn("S")

    async def list_notes(self, session, folder=None, include_deleted=False):
        return [{"id": i, **n} for i, n in self.notes.items() if (folder is None or n["folder"] == folder) and not n["deleted"]]

    async def create_note(self, session, text, folder=None, is_folder_marker=False):
        self._n += 1
        nid = f"n{self._n}"
        self.notes[nid] = {"text": text, "folder": folder or "", "deleted": False, "updatedAt": self._n}
        self.writes.append(text.split("\n", 1)[0])
        return {"id": nid, **self.notes[nid]}

    async def update_note(self, session, note_id, text=None, folder=None):
        self.notes[note_id]["text"] = text
        self.writes.append(text.split("\n", 1)[0])
        return {"id": note_id, **self.notes[note_id]}

    async def get_note(self, session, note_id):
        return {"id": note_id, **self.notes[note_id]}


@pytest.fixture
def notes(monkeypatch):
    fake = FakeNotes()
    monkeypatch.setattr(notes_plugin, "_sessions", fake)
    for name in ("list_notes", "create_note", "update_note", "get_note"):
        monkeypatch.setattr(notes_plugin, name, getattr(fake, name))
    monkeypatch.setattr(sm, "_account_email", lambda: "owner@example.com")
    return fake


def write(ws, name, data):
    (ws / name).write_text(json.dumps(data), encoding="utf-8")


def read(ws, name):
    return json.loads((ws / name).read_text(encoding="utf-8"))


def run(coro):
    return asyncio.run(coro)


def test_push_splits_secrets_from_plain_settings(tmp_path, notes):
    write(tmp_path, "subscription.json", {"claudeModel": "opus", "ownOpenaiApiKey": "sk-secret-1", "ownAnthropicApiKey": "sk-ant-2"})
    write(tmp_path, "persona.json", {"profileKey": "caroline"})
    write(tmp_path, "ratatosk-own-account.json", {"email": "c@x", "password": "pw-3"})
    result = run(sm.push_all(str(tmp_path)))
    assert result["ok"]
    by_title = {n["text"].split("\n", 1)[0]: n for n in notes.notes.values()}
    assert set(by_title) == {"settings:persona", "settings:subscription", "vault:settings:subscription", "vault:settings:ratatosk-own-account"}
    plain = by_title["settings:subscription"]
    assert plain["folder"] == "Caroline:Settings" and "sk-secret-1" not in plain["text"] and "sk-ant-2" not in plain["text"] and "opus" in plain["text"]
    vault = by_title["vault:settings:subscription"]
    assert vault["folder"] == "Caroline:Vault" and "sk-secret-1" in vault["text"] and "opus" not in vault["text"]
    assert "pw-3" in by_title["vault:settings:ratatosk-own-account"]["text"]
    assert "settings:ratatosk-own-account" not in by_title


def test_push_twice_writes_nothing_the_second_time_and_updates_on_change(tmp_path, notes):
    write(tmp_path, "continuousDialogue.json", {"enabled": True})
    run(sm.push_all(str(tmp_path)))
    assert notes.writes == ["settings:continuous-dialogue"]
    run(sm.push_all(str(tmp_path)))
    assert notes.writes == ["settings:continuous-dialogue"]            # unchanged: no write
    write(tmp_path, "continuousDialogue.json", {"enabled": False})
    run(sm.push_all(str(tmp_path)))
    assert notes.writes == ["settings:continuous-dialogue"] * 2
    assert len(notes.notes) == 1                                          # updated in place, not duplicated


def test_missing_file_never_deletes_its_note(tmp_path, notes):
    write(tmp_path, "persona.json", {"profileKey": "peter"})
    run(sm.push_all(str(tmp_path)))
    (tmp_path / "persona.json").unlink()
    run(sm.push_all(str(tmp_path)))
    assert len(notes.notes) == 1 and not next(iter(notes.notes.values()))["deleted"]


def test_fresh_install_gets_everything_back(tmp_path, notes):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir(), new.mkdir()
    write(old, "subscription.json", {"claudeModel": "sonnet", "ownOpenaiApiKey": "sk-secret-1"})
    write(old, "persona.json", {"profileKey": "peter", "peter": {"name": "Pete"}})
    write(old, "chat-mode-1.json", {"mode": "sw"})
    write(old, "ratatosk-own-account.json", {"email": "c@x", "password": "pw-3"})
    run(sm.push_all(str(old)))
    result = run(sm.restore_missing(str(new)))
    assert sorted(result["restored"]) == ["chat-mode-1", "persona", "ratatosk-own-account", "subscription"]
    assert read(new, "subscription.json") == {"claudeModel": "sonnet", "ownOpenaiApiKey": "sk-secret-1"}
    assert read(new, "persona.json") == {"profileKey": "peter", "peter": {"name": "Pete"}}
    assert read(new, "chat-mode-1.json") == {"mode": "sw"}
    assert read(new, "ratatosk-own-account.json")["password"] == "pw-3"


def test_restore_never_overwrites_a_local_value_but_fills_gaps(tmp_path, notes):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir(), new.mkdir()
    write(old, "subscription.json", {"claudeModel": "opus", "openaiModel": "gpt-x", "ownOpenaiApiKey": "sk-secret-1"})
    run(sm.push_all(str(old)))
    write(new, "subscription.json", {"claudeModel": "haiku"})            # changed here, newer than the note
    run(sm.restore_missing(str(new)))
    assert read(new, "subscription.json") == {"claudeModel": "haiku", "openaiModel": "gpt-x", "ownOpenaiApiKey": "sk-secret-1"}


def test_not_logged_in_does_nothing(tmp_path, notes, monkeypatch):
    monkeypatch.setattr(sm, "_account_email", lambda: None)
    write(tmp_path, "persona.json", {"profileKey": "caroline"})
    assert run(sm.push_all(str(tmp_path)))["ok"] is False
    assert run(sm.restore_missing(str(tmp_path)))["ok"] is False
    assert notes.notes == {}


def test_state_belongs_to_one_account(tmp_path, notes, monkeypatch):
    write(tmp_path, "persona.json", {"profileKey": "caroline"})
    run(sm.push_all(str(tmp_path)))
    monkeypatch.setattr(sm, "_account_email", lambda: "other@example.com")
    notes.notes.clear()                                                   # the other account has no notes yet
    run(sm.push_all(str(tmp_path)))
    assert len(notes.notes) == 1                                          # written again for the new account


def test_a_notes_failure_in_sync_now_is_reported_not_raised(tmp_path, monkeypatch, notes):
    async def boom(*_a, **_k):
        raise RuntimeError("notes down")
    monkeypatch.setattr(sm, "push_all", boom)
    result = run(sm.sync_now(str(tmp_path)))
    assert result["ok"] is False and "notes down" in result["reason"]
