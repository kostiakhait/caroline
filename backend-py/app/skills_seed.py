"""Ports backend/src/workspace.ts's seedSkills() -- the one piece of
ensureWorkspace() that's actually load-bearing for backend-py right now
(2026-09-09 cutover): Claude Code's `skills: "all"` option (set on every
ClaudeAgentOptions in chat_session.py) only surfaces skills that are
physically present under workspaceDir/Skills/. Previously this ran every
time the Node backend started; now that BackendProcess.cs launches Python
instead, nothing seeds Skills/ at all unless this runs too -- a fresh
install (or one where Skills/ was never seeded) would silently lose every
skill (python-environment, showing-files-in-chat, squirrelwisdom-login,
vault-backups, embedded-browser-troubleshooting, ratatosk-messenger,
analyzing-video) with no error, just the model never finding them.

NOT ported (known, tracked gap, lower urgency -- durable CLI-level state,
not per-process): the ONE user-scope MCP server registration
(`caroline-browser`, the standalone browser tool referenced throughout
policies.py's embedded_browser_instruction) that workspace.ts's
ensureWorkspace()/defaultServers() also used to perform via `claude mcp
add --scope user`. That registration persists in the `claude` CLI's own
config once done and needs no re-registration on every start -- every
existing install (including this dev machine) already has it from past
Node-backend runs, so this gap only affects a genuinely NEW install that
never ran the Node backend even once. The rest of ensureWorkspace()
(SHARED_UTILITY_SERVERS -- mouse/keyboard/notes/etc. launched once as HTTP
servers to work around Node's 1:1 stdio-per-process constraint across
multiple tabs) doesn't apply to backend-py at all: its plugins are
in-process SDK-hosted MCP tools, not spawned subprocesses, so that whole
problem class doesn't exist here.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.logging_setup import log_event

# Mirrors local_tts_launcher.py's own shipped-vs-dev-tree resolution: the
# Makefile copies backend/skills-src/ into backend-py/skills-src/ at
# packaging time (single source of truth for skill CONTENT stays under
# backend/skills-src/ -- these are plain markdown files, nothing
# Node-specific about them, no reason to fork them in the repo); a dev
# tree running straight from the repo without a full `make build` yet
# falls back to the sibling backend/skills-src/ directly.
_SHIPPED_SKILLS_SRC = Path(__file__).resolve().parent.parent / "skills-src"
_DEV_TREE_SKILLS_SRC = Path(__file__).resolve().parent.parent.parent / "backend" / "skills-src"


def _resolve_skills_src() -> Path | None:
    for candidate in (_SHIPPED_SKILLS_SRC, _DEV_TREE_SKILLS_SRC):
        if candidate.is_dir():
            return candidate
    return None


def seed_skills(workspace_dir: str) -> None:
    """Copies every skill folder from skills-src/ into workspace_dir/Skills/,
    overwriting each one every start -- these are code-managed defaults, so
    a code update to an existing skill's content should take effect on the
    next start, the same way any other code change does. Only touches
    directories that exist in skills-src/ by name: a custom skill the user
    or Caroline added directly under Skills/ under a different name is
    never touched, so the catalog stays genuinely appendable."""
    skills_src = _resolve_skills_src()
    if skills_src is None:
        log_event("engine", "seed_skills_source_not_found", checked=[str(_SHIPPED_SKILLS_SRC), str(_DEV_TREE_SKILLS_SRC)])
        return
    skills_dir = Path(workspace_dir) / "Skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    seeded = []
    for entry in skills_src.iterdir():
        if not entry.is_dir():
            continue
        try:
            shutil.copytree(entry, skills_dir / entry.name, dirs_exist_ok=True)
            seeded.append(entry.name)
        except Exception as exc:
            log_event("engine", "seed_skill_failed", skill=entry.name, error=str(exc))
    log_event("engine", "skills_seeded", count=len(seeded), skills=seeded, source=str(skills_src))
