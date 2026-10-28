"""Skills for every engine (app/skills.py, app/plugins/skills_plugin.py).
Run: python -m pytest test_skills.py -q"""

import asyncio

import pytest

from app import skills
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
    _skill(ws / "Skills" / "python-environment", "python-environment", "Which Python to use.",
           extra={"reference/paths.md": "the paths", ".hidden": "no"})
    _skill(ws / "Skills" / "lessons-learned" / "mail-passwords", "mail-passwords", "Save a mailbox password.")
    monkeypatch.delenv("CAROLINE_SKILL_DIRS", raising=False)
    monkeypatch.setattr(skills, "_sources", {})
    monkeypatch.setattr(skills_plugin, "WORKSPACE_DIR", str(ws))
    return ws


def _run(coro):
    return asyncio.run(coro)


def test_frontmatter_plain_quoted_and_folded():
    meta, body = skills.split_frontmatter('---\nname: "a"\ndescription: >\n  first line\n  second line\nx: y\n---\nBody\n')
    assert meta == {"name": "a", "description": "first line second line", "x": "y"}
    assert body == "Body"
    assert skills.split_frontmatter("no frontmatter") == ({}, "no frontmatter")


def test_catalog_has_seeded_and_lessons(workspace):
    names = [s.name for s in skills.catalog(str(workspace))]
    assert names == ["mail-passwords", "python-environment"]
    text = skills.catalog_clause(str(workspace))
    assert "- python-environment: Which Python to use." in text and "use_skill" in text


def test_no_skills_no_clause(tmp_path, monkeypatch):
    monkeypatch.delenv("CAROLINE_SKILL_DIRS", raising=False)
    monkeypatch.setattr(skills, "_sources", {})
    assert skills.catalog_clause(str(tmp_path)) == ""


def test_host_sources_env_and_registered(workspace, tmp_path, monkeypatch):
    env_dir = tmp_path / "env"
    _skill(env_dir / "one", "from-env", "Given by the environment.")
    app_skill = tmp_path / "apps" / "notepad" / "skills" / "editing"
    _skill(app_skill, "notepad-editing", "Edit a text in the Notepad.")
    monkeypatch.setenv("CAROLINE_SKILL_DIRS", str(env_dir))
    skills.register_source("apps", lambda: [app_skill])
    found = {s.name: s.source for s in skills.catalog(str(workspace))}
    assert found["from-env"] == "CAROLINE_SKILL_DIRS"
    assert found["notepad-editing"] == "apps"
    skills.unregister_source("apps")
    assert "notepad-editing" not in {s.name for s in skills.catalog(str(workspace))}


def test_first_of_a_name_wins_and_broken_source_is_skipped(workspace, tmp_path):
    other = tmp_path / "other"
    _skill(other / "python-environment", "python-environment", "A second one.")
    skills.register_source("dup", lambda: [other])
    skills.register_source("broken", lambda: 1 / 0)
    same = [s for s in skills.catalog(str(workspace)) if s.name == "python-environment"]
    assert len(same) == 1 and same[0].description == "Which Python to use."


def test_use_skill_gives_body_and_files(workspace):
    result = _run(skills_plugin.use_skill({"name": "Python-Environment"}, None))
    text = result["text"]
    assert "Do it right." in text and "description:" not in text
    assert "- reference/paths.md" in text and ".hidden" not in text


def test_unknown_skill_lists_the_catalog(workspace):
    result = _run(skills_plugin.use_skill({"name": "nope"}, None))
    assert result["is_error"] and "python-environment" in result["text"]


def test_read_skill_file_stays_inside_the_skill(workspace):
    ok = _run(skills_plugin.read_skill_file({"name": "python-environment", "path": "reference/paths.md"}, None))
    assert ok["text"] == "the paths"
    out = _run(skills_plugin.read_skill_file({"name": "python-environment", "path": "../lessons-learned/mail-passwords/SKILL.md"}, None))
    assert out["is_error"] and "outside" in out["text"]
    missing = _run(skills_plugin.read_skill_file({"name": "python-environment", "path": "nothing.md"}, None))
    assert missing["is_error"]


def test_list_skills_tool(workspace):
    assert "mail-passwords" in _run(skills_plugin.list_skills({}, None))["text"]


def test_plugin_is_discovered_and_always_loaded():
    from app.plugins.loader import discover_plugins
    plugin = next(p for p in discover_plugins() if p.name == "skills")
    assert plugin.always_loaded
    assert {t.name for t in plugin.tools} == {"list_skills", "use_skill", "read_skill_file"}
