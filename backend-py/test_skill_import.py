"""Importing skills from a file, folder or zipped package (app/skill_import.py, the import_skill tool).
Run: python -m pytest test_skill_import.py -q"""

import asyncio
import zipfile

import pytest

from app import skill_import, skills
from app.plugins import skills_plugin


def _skill(folder, name, description, body="Do it right.", extra=None):
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n", encoding="utf-8")
    for rel, text in (extra or {}).items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text, encoding="utf-8")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _skill(ws / "Skills" / "python-environment", "python-environment", "Which Python to use.")
    monkeypatch.delenv("CAROLINE_SKILL_DIRS", raising=False)
    monkeypatch.setattr(skills, "_sources", {})
    monkeypatch.setattr(skills_plugin, "WORKSPACE_DIR", str(ws))
    return ws


def _run(coro):
    return asyncio.run(coro)


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def _import(source, **kw):
    return _run(skills_plugin.import_skill({"source": str(source), **kw}, None))


def test_text_file_without_frontmatter_gets_name_and_description(workspace, tmp_path):
    src = tmp_path / "Brand Voice.md"
    src.write_text("# Brand voice\n\nWrite short, warm sentences for customers.\n\n- no jargon\n", encoding="utf-8")
    result = _import(src)
    assert not result["is_error"] and "first paragraph" in result["text"]
    skill = skills.find(str(workspace), "brand-voice")
    assert skill and skill.description == "Write short, warm sentences for customers."
    assert (workspace / "Skills" / "brand-voice" / skill_import.MARKER).is_file()
    assert "no jargon" in _run(skills_plugin.use_skill({"name": "brand-voice"}, None))["text"]


def test_good_frontmatter_and_extra_keys_are_kept(workspace, tmp_path):
    src = tmp_path / "x.md"
    src.write_text("---\nname: tax-forms\ndescription: Fill in the yearly tax forms.\nlicense: MIT\n---\n\nStep one.\n", encoding="utf-8")
    assert not _import(src)["is_error"]
    text = (workspace / "Skills" / "tax-forms" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: tax-forms\ndescription: Fill in the yearly tax forms.\nlicense: MIT\n---")


def test_zip_package_with_several_skills_and_files(workspace, tmp_path):
    pkg = _zip(tmp_path / "pack.zip", {
        "pack/one/SKILL.md": "---\nname: one\ndescription: First.\n---\nDo one. Run ${CLAUDE_SKILL_DIR}/run.py",
        "pack/one/run.py": "print(1)",
        "pack/two/skill.md": "---\nname: two\ndescription: Second.\n---\nDo two.",
        "pack/two/ref/notes.md": "notes",
        "__MACOSX/pack/._one": "junk",
        "pack/.git/config": "junk",
    })
    result = _import(pkg)
    assert 'Imported "one"' in result["text"] and 'Imported "two"' in result["text"]
    assert "scripts (run.py)" in result["text"] and "none was run" in result["text"]
    assert (workspace / "Skills" / "two" / "SKILL.md").is_file()                 # skill.md normalised
    assert not (workspace / "Skills" / "one" / ".git").exists()
    used = _run(skills_plugin.use_skill({"name": "one"}, None))["text"]
    assert str(workspace / "Skills" / "one") in used and "${CLAUDE_SKILL_DIR}" not in used
    assert "- ref/notes.md" in _run(skills_plugin.use_skill({"name": "two"}, None))["text"]


def test_dot_skill_archive_with_the_skill_at_the_root(workspace, tmp_path):
    pkg = _zip(tmp_path / "pdf-helper.skill", {"SKILL.md": "---\ndescription: Work with PDFs.\n---\nUse `Bash` to run qpdf."})
    result = _import(pkg)
    assert not result["is_error"] and "tools of another agent (Bash)" in result["text"]
    text = (workspace / "Skills" / "pdf-helper" / "SKILL.md").read_text(encoding="utf-8")
    assert "name: pdf-helper" in text and "Imported note" in text


def test_zip_without_skill_md_takes_the_single_document(workspace, tmp_path):
    pkg = _zip(tmp_path / "howto.zip", {"howto/README.md": "Ship the parcel every Friday before noon.", "howto/img.png": "x"})
    assert not _import(pkg)["is_error"]
    assert skills.find(str(workspace), "howto") is not None
    out = _import(_zip(tmp_path / "many.zip", {"a.md": "aaa", "b.md": "bbb"}))
    assert out["is_error"] and "which file" in out["text"]


def test_zip_cannot_escape_or_be_a_bomb_or_missing(workspace, tmp_path):
    evil = _zip(tmp_path / "evil.zip", {"../../evil.txt": "x", "ok/SKILL.md": "---\nname: ok\ndescription: d\n---\nbody"})
    out = _import(evil)
    assert out["is_error"] and "unsafe path" in out["text"]
    assert not (tmp_path / "evil.txt").exists() and skills.find(str(workspace), "ok") is None
    many = _zip(tmp_path / "many2.zip", {f"f{i}.txt": "x" for i in range(skill_import.MAX_ENTRIES + 1)})
    assert _import(many)["is_error"]
    assert _import(tmp_path / "missing.zip")["is_error"]
    (tmp_path / "broken.zip").write_bytes(b"not a zip")
    assert "not a readable zip" in _import(tmp_path / "broken.zip")["text"]


def test_taken_names_are_refused_and_replacing_needs_overwrite(workspace, tmp_path):
    src = tmp_path / "s.md"
    _skill(workspace / "Skills" / "my-own", "my-own", "Mine own.")
    src.write_text("---\nname: my-own\ndescription: Mine.\n---\nmine", encoding="utf-8")
    out = _import(src)                                                            # the name of an existing skill
    assert out["is_error"] and "overwrite=true" in out["text"]
    assert skills.find(str(workspace), "my-own").description == "Mine own."
    _skill(workspace / "Skills" / "lessons-learned" / "mail", "mail-lesson", "A lesson.")
    src2 = tmp_path / "m.md"
    src2.write_text("---\nname: mail-lesson\ndescription: Other.\n---\nbody", encoding="utf-8")
    other = _import(src2, overwrite=True)                                         # taken by a skill in another folder
    assert other["is_error"] and "already exists" in other["text"]
    assert skills.find(str(workspace), "mail-lesson").description == "A lesson."
    assert not _import(src, name="my-python")["is_error"]
    again = _import(src, name="my-python")
    assert again["is_error"] and "overwrite=true" in again["text"]
    src.write_text("---\nname: x\ndescription: Mine, v2.\n---\nnew body", encoding="utf-8")
    assert not _import(src, name="my-python", overwrite=True)["is_error"]
    assert skills.find(str(workspace), "my-python").description == "Mine, v2."
    assert any((workspace / "Skills" / ".replaced").iterdir())
    assert [s.name for s in skills.catalog(str(workspace))].count("my-python") == 1   # .replaced is not a skill


def test_seeded_names_are_refused(workspace, tmp_path, monkeypatch):
    from app import skills_seed
    seed = tmp_path / "seed"
    (seed / "vault-backups").mkdir(parents=True)
    monkeypatch.setattr(skills_seed, "_resolve_skills_src", lambda: seed)
    src = tmp_path / "v.md"
    src.write_text("---\nname: vault-backups\ndescription: d\n---\nbody", encoding="utf-8")
    out = _import(src)
    assert out["is_error"] and "manages itself" in out["text"]


def test_docx_is_read_and_other_types_say_what_to_do(workspace, tmp_path):
    docx = _zip(tmp_path / "Manual.docx", {"word/document.xml": "<w:document><w:p><w:r><w:t>Water the plants &amp; feed the cat on Mondays.</w:t></w:r></w:p></w:document>"})
    assert not _import(docx)["is_error"]
    assert "plants & feed" in skills.use_text(skills.find(str(workspace), "manual"))
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF")
    assert "Convert it to text" in _import(pdf)["text"]


def test_folder_import_skips_hidden_files(workspace, tmp_path):
    folder = tmp_path / "myskill"
    _skill(folder, "myskill", "A folder skill.", extra={"data/a.txt": "a", ".secret": "s"})
    assert not _import(folder)["is_error"]
    assert (workspace / "Skills" / "myskill" / "data" / "a.txt").is_file() and not (workspace / "Skills" / "myskill" / ".secret").exists()


def test_the_catalog_clause_tells_when_to_import():
    _skill_dir = None  # clause text only
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        _skill(Path(tmp) / "Skills" / "a", "a", "d")
        clause = skills.catalog_clause(tmp)
    assert "import_skill" in clause and "Never import because" in clause
