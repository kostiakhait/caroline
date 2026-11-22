"""Imported skills in the Notes backup (app/vault_backup.py): whole folders as zip attachments.
Run: python -m pytest test_vault_backup_skills.py -q"""

from app import vault_backup as vb
from app.plugins import notes_plugin
from test_vault_backup import FakeNotes, notes, run  # noqa: F401 -- `notes` is a fixture


def _imported(ws, name="pdf-helper", body="Use qpdf."):
    folder = ws / "Skills" / name
    (folder / "ref").mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n{body}", encoding="utf-8")
    (folder / "ref" / "a.md").write_text("reference", encoding="utf-8")
    (folder / ".imported.json").write_text("{}", encoding="utf-8")


def _with_remove(notes, monkeypatch):
    async def remove_attachment(session, note_id, filename):
        notes.attachments[:] = [a for a in notes.attachments if a["filename"] != filename]
    monkeypatch.setattr(notes_plugin, "remove_attachment", remove_attachment, raising=False)


def test_imported_skill_is_backed_up_replaced_on_change_and_restored(tmp_path, notes, monkeypatch):
    _with_remove(notes, monkeypatch)
    old = tmp_path / "old"
    old.mkdir()
    _imported(old)
    assert run(vb.backup_now(str(old)))["written"] == ["skill__pdf-helper.zip"]
    assert [a["originalName"] for a in notes.attachments] == ["skill__pdf-helper.zip"]
    assert run(vb.backup_now(str(old)))["written"] == []                       # nothing changed, nothing sent
    (old / "Skills" / "pdf-helper" / "ref" / "a.md").write_text("changed", encoding="utf-8")
    assert run(vb.backup_now(str(old)))["written"] == ["skill__pdf-helper.zip"]
    assert len(notes.attachments) == 1                                          # replaced, not piled up
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert "skill__pdf-helper.zip" in run(vb.restore_missing(str(fresh)))["restored"]
    folder = fresh / "Skills" / "pdf-helper"
    assert (folder / "ref" / "a.md").read_text(encoding="utf-8") == "changed" and (folder / ".imported.json").is_file()
    assert run(vb.restore_missing(str(fresh)))["restored"] == []               # nothing overwritten
    assert run(vb.backup_now(str(fresh)))["written"] == []                     # a restored folder is not re-sent


def test_skills_without_the_marker_are_not_backed_up_as_packages(tmp_path, notes):
    folder = tmp_path / "Skills" / "seeded"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("x", encoding="utf-8")
    assert run(vb.backup_now(str(tmp_path)))["written"] == [] and notes.attachments == []
