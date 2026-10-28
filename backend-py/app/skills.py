"""Skills for every engine (2026-10-28, explicit instruction: "нормальная поддержка скиллов").

A skill is a folder with a SKILL.md: a frontmatter (`name`, `description`) and a body of instructions, plus any
other files the body refers to. Until now only Claude Code read them (`skills: "all"`), so the SW engine never saw
one -- neither the seeded ones (skills_seed.py) nor the lessons she writes herself under Skills/lessons-learned/
(policies.learn_from_mistakes_instruction), which told her to look them up with a list_skills that did not exist.

The same disclosure Claude Code uses:

    the catalog          -> one line per skill (name: description), in the base request (catalog_clause)
    use_skill(name)      -> the SKILL.md body and the list of the skill's other files
    read_skill_file(...) -> one of those files

Where skills come from, read fresh on every call (a new lesson or a new source is seen on the next turn):

    1. <workspace>/Skills/: every folder holding a SKILL.md, at any depth (lessons-learned/<topic>/ included)
    2. CAROLINE_SKILL_DIRS: folders (os.pathsep-separated) a host gives her
    3. register_source(): a function a host registers, returning such folders (e.g. the skills of the apps
       installed on the device)

A folder given by 2 or 3 is either one skill (it holds a SKILL.md) or a folder of skills. The first skill of a name
wins; a later one of the same name is left out and logged."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from app.logging_setup import log_event

SKILL_FILE = "SKILL.md"
MAX_DESCRIPTION_CHARS = 300
MAX_FILE_CHARS = 60_000

_sources: dict[str, Callable[[], Iterable[str | Path]]] = {}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    folder: Path
    source: str


def register_source(name: str, folders: Callable[[], Iterable[str | Path]]) -> None:
    """A host's own skills: `folders()` is called on every catalog read. Registering a name again replaces it."""
    _sources[name] = folders


def unregister_source(name: str) -> None:
    _sources.pop(name, None)


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """The `key: value` lines between the leading `---` lines, and the body after them. Values may be quoted;
    a folded value (`>` or `|`) takes the indented lines below it, joined by spaces."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta: dict[str, str] = {}
    key = None
    for line in lines[1:end]:
        if key and line[:1] in (" ", "\t") and line.strip():
            meta[key] = (meta[key] + " " + line.strip()).strip()
            continue
        key = None
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        if v in (">", "|", ">-", "|-"):
            v, key = "", k
        elif len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        meta[k] = v
    return meta, "\n".join(lines[end + 1:]).lstrip("\n")


def _read_skill(folder: Path, source: str) -> Skill | None:
    try:
        meta, _ = split_frontmatter((folder / SKILL_FILE).read_text(encoding="utf-8"))
    except OSError as exc:
        log_event("engine", "skill_unreadable", folder=str(folder), error=str(exc))
        return None
    name = (meta.get("name") or folder.name).strip()
    description = " ".join((meta.get("description") or "").split())
    return Skill(name=name, description=description, folder=folder, source=source)


def _skill_folders(root: Path) -> list[Path]:
    """`root` itself when it is a skill, else every folder under it that holds a SKILL.md (a skill's own
    subfolders are its files, not skills of their own)."""
    if (root / SKILL_FILE).is_file():
        return [root]
    found: list[Path] = []
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return found
    for child in children:
        found.extend(_skill_folders(child))
    return found


def _roots(workspace_dir: str) -> list[tuple[Path, str]]:
    roots: list[tuple[Path, str]] = [(Path(workspace_dir) / "Skills", "workspace")]
    for entry in (os.environ.get("CAROLINE_SKILL_DIRS") or "").split(os.pathsep):
        if entry.strip():
            roots.append((Path(entry.strip()), "CAROLINE_SKILL_DIRS"))
    for source, folders in list(_sources.items()):
        try:
            roots.extend((Path(f), source) for f in folders())
        except Exception as exc:  # noqa: BLE001 -- a broken host source must not cost the other skills
            log_event("engine", "skill_source_failed", source=source, error=str(exc))
    return roots


def catalog(workspace_dir: str) -> list[Skill]:
    skills: list[Skill] = []
    seen: dict[str, Skill] = {}
    for root, source in _roots(workspace_dir):
        if not root.is_dir():
            continue
        for folder in _skill_folders(root):
            skill = _read_skill(folder, source)
            if skill is None:
                continue
            key = skill.name.lower()
            if key in seen:
                log_event("engine", "skill_name_taken", name=skill.name, kept=str(seen[key].folder), left_out=str(folder))
                continue
            seen[key] = skill
            skills.append(skill)
    return skills


def find(workspace_dir: str, name: str) -> Skill | None:
    wanted = (name or "").strip().lower()
    return next((s for s in catalog(workspace_dir) if s.name.lower() == wanted), None)


def _short(text: str) -> str:
    return text if len(text) <= MAX_DESCRIPTION_CHARS else text[:MAX_DESCRIPTION_CHARS - 1] + "…"


def catalog_text(skills: list[Skill]) -> str:
    return "\n".join(f"- {s.name}: {_short(s.description) or '(no description)'}" for s in skills) or "(no skills)"


def catalog_clause(workspace_dir: str) -> str:
    """The catalog for the base request; empty when there are no skills."""
    skills = catalog(workspace_dir)
    if not skills:
        return ""
    return (
        "Your skills -- written instructions for particular kinds of work, one line each (name: when to use it). "
        "When the task matches a skill's description, call use_skill(name) FIRST and follow what it says; it may "
        "point to more files of the skill, read them with read_skill_file(name, path). Skills you write yourself "
        "(Skills/lessons-learned/) are listed here too.\n" + catalog_text(skills)
    )


def skill_files(skill: Skill) -> list[str]:
    """The skill's files other than SKILL.md, as paths relative to its folder."""
    files: list[str] = []
    for path in sorted(skill.folder.rglob("*")):
        if path.is_file() and path.name != SKILL_FILE and not any(p.startswith(".") for p in path.relative_to(skill.folder).parts):
            files.append(path.relative_to(skill.folder).as_posix())
    return files


def use_text(skill: Skill) -> str:
    _, body = split_frontmatter((skill.folder / SKILL_FILE).read_text(encoding="utf-8"))
    files = skill_files(skill)
    listed = ("\n\nFiles of this skill (read_skill_file):\n" + "\n".join(f"- {f}" for f in files)) if files else ""
    return f"Skill: {skill.name}\nFolder: {skill.folder}\n\n{body.strip()}{listed}"


def read_file_text(skill: Skill, relative: str) -> str:
    """A file of the skill; never a path outside its folder."""
    folder = skill.folder.resolve()
    target = (folder / (relative or "")).resolve()
    if target != folder and folder not in target.parents:
        raise ValueError(f'"{relative}" is outside the skill "{skill.name}"')
    if not target.is_file():
        raise FileNotFoundError(f'the skill "{skill.name}" has no file "{relative}"')
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + f"\n\n[... cut at {MAX_FILE_CHARS} characters of {len(text)}]"
    return text
