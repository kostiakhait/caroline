"""The skill tools of every engine: list_skills, use_skill, read_skill_file (app/skills.py has the catalog and
why). Always loaded, not behind the tool groups: a skill is opened before the work it describes, often before
any group is looked at."""

from __future__ import annotations

from typing import Any

from app import skills
from app.plugins.loader import Plugin, PluginTool
from app.workspace_dir import WORKSPACE_DIR


def _not_found(name: str) -> dict[str, Any]:
    return {"text": f'No skill named "{name}". The skills now:\n' + skills.catalog_text(skills.catalog(WORKSPACE_DIR)),
            "is_error": True}


async def list_skills(_args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    return {"text": skills.catalog_text(skills.catalog(WORKSPACE_DIR))}


async def use_skill(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    name = str(args.get("name") or "")
    skill = skills.find(WORKSPACE_DIR, name)
    if skill is None:
        return _not_found(name)
    return {"text": skills.use_text(skill)}


async def read_skill_file(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    name = str(args.get("name") or "")
    skill = skills.find(WORKSPACE_DIR, name)
    if skill is None:
        return _not_found(name)
    try:
        return {"text": skills.read_file_text(skill, str(args.get("path") or ""))}
    except (ValueError, OSError) as exc:
        return {"text": str(exc), "is_error": True}


PLUGIN = Plugin(
    name="skills",
    description="Your skills: written instructions for particular kinds of work",
    always_loaded=True,
    tools=[
        PluginTool(
            name="list_skills",
            description="Lists your skills, one line each: name and when to use it.",
            input_schema={},
            handler=list_skills,
        ),
        PluginTool(
            name="use_skill",
            description="Opens one skill by name: its instructions, and the list of its other files. Call it before "
                        "doing work a skill's description matches, and follow what it says.",
            input_schema={"name": str},
            handler=use_skill,
        ),
        PluginTool(
            name="read_skill_file",
            description="Reads one file of a skill (a path from the file list use_skill gave, relative to the skill's folder).",
            input_schema={"name": str, "path": str},
            handler=read_skill_file,
        ),
    ],
)
