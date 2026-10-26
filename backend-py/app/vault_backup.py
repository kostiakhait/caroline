"""The memory backup to Notes, done by code (2026-10-26, explicit instruction: "I do not understand why a model
is called for this, it should be a purely mechanical operation -- it was meant as just copying the memory files
to the server so they are not lost if the machine fails").

It used to be an hourly reminder that woke a Claude process to follow skills-src/vault-backups/SKILL.md --
a whole turn with the full base prompt every hour, also at night and when nothing had changed. Now the code
copies the same things, with the same note names (so backups already in Notes keep working), and only what
changed:

- the scheduled reminders                      -> note "vault:backup"                  (folder Caroline:Vault)
- Skills/lessons-learned/<topic>/SKILL.md      -> note "vault:skill:<topic>"
- workspace/dehydrated/ and workspace/uploads/ -> attachments of note "vault:dehydrated-files",
  attached as "<dir>__<file name>"; a file larger than a note attachment may be is skipped.
The persona and the other settings are carried by settings_mirror.py.

Restore (right after a login on a fresh install): reminders are merged in by id, a lesson skill that is not on
disk is written, a missing file is downloaded. Nothing that exists locally is ever overwritten.

Nothing here logs content; only names and counts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.logging_setup import log_event

VAULT_FOLDER = os.environ.get("CAROLINE_SETTINGS_VAULT_FOLDER", "Caroline:Vault")
STATE_FILE = "vault-backup-state.json"            # digest of what each note was last written with, per account
BACKUP_NOTE = "vault:backup"
SKILL_PREFIX = "vault:skill:"
FILES_NOTE = "vault:dehydrated-files"
LESSONS_DIR = ("Skills", "lessons-learned")
FILE_DIRS = ("dehydrated", "uploads")
BACKUP_KIND = "vault-backup-hourly"               # the old model-driven reminder, removed from the schedule
MAX_FILES_PER_RUN = 20                            # attach / download at most this many files per run
MAX_ATTACHMENT_BYTES = 47 * 1024 * 1024           # notes_api.MAX_ATTACHMENT_BYTES


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _body(text: str) -> str:
    return text.split("\n", 1)[1] if "\n" in text else ""


def _title_of(text: str) -> str:
    return (text or "").split("\n", 1)[0].strip()


def _state(workspace_dir: str, account: str) -> dict[str, str]:
    data = _read_json(Path(workspace_dir) / STATE_FILE) or {}
    return dict(data.get("notes") or {}) if data.get("account") == account else {}


def _save_state(workspace_dir: str, account: str, notes: dict[str, str]) -> None:
    try:
        _write_json_atomic(Path(workspace_dir) / STATE_FILE, {"account": account, "notes": notes})
    except OSError as exc:
        log_event("engine", "vault_backup_state_save_failed", error=str(exc)[:200])


# --- what is backed up ---------------------------------------------------------

def _reminders(workspace_dir: str) -> list[dict[str, Any]]:
    data = _read_json(Path(workspace_dir) / "schedule.json")
    return [r for r in data if isinstance(r, dict) and r.get("kind") != BACKUP_KIND] if isinstance(data, list) else []


def _lesson_skills(workspace_dir: str) -> dict[str, str]:
    root = Path(workspace_dir, *LESSONS_DIR)
    skills: dict[str, str] = {}
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            try:
                if folder.is_dir() and (folder / "SKILL.md").is_file():
                    skills[folder.name] = (folder / "SKILL.md").read_text(encoding="utf-8")
            except OSError:
                continue
    return skills


def _local_files(workspace_dir: str) -> dict[str, Path]:
    """attachment name -> file, for every file that fits in a note attachment."""
    files: dict[str, Path] = {}
    for sub in FILE_DIRS:
        root = Path(workspace_dir) / sub
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            try:
                if path.is_file() and path.stat().st_size <= MAX_ATTACHMENT_BYTES:
                    files[f"{sub}__{path.name}"] = path
            except OSError:
                continue
    return files


def remove_old_reminder(workspace_dir: str) -> bool:
    """The hourly model-driven backup reminder is gone for good: drops it from the schedule if it is there."""
    path = Path(workspace_dir) / "schedule.json"
    data = _read_json(path)
    if not isinstance(data, list) or not any(isinstance(r, dict) and r.get("kind") == BACKUP_KIND for r in data):
        return False
    _write_json_atomic(path, [r for r in data if not (isinstance(r, dict) and r.get("kind") == BACKUP_KIND)])
    log_event("engine", "vault_backup_old_reminder_removed")
    return True


# --- Notes access ----------------------------------------------------------------

async def _with_session(fn: Any) -> Any:
    from app.plugins import notes_plugin

    return await notes_plugin._sessions.with_session(fn)


async def _index() -> dict[str, dict[str, Any]]:
    from app.plugins import notes_plugin

    entries = await _with_session(lambda s: notes_plugin.list_notes(s, folder=VAULT_FOLDER))
    return {_title_of(e.get("text", "")): e for e in entries}


def _account_email() -> str | None:
    from app.plugins.sw_api import load_credentials

    creds = load_credentials()
    return creds["email"] if creds else None


async def _write_note(index: dict[str, dict[str, Any]], title: str, text: str) -> None:
    from app.plugins import notes_plugin

    existing = index.get(title)
    if existing:
        await _with_session(lambda s: notes_plugin.update_note(s, existing["id"], text=text, folder=VAULT_FOLDER))
    else:
        created = await _with_session(lambda s: notes_plugin.create_note(s, text, folder=VAULT_FOLDER))
        index[title] = {"id": (created or {}).get("id")}


_lock = asyncio.Lock()


async def backup_now(workspace_dir: str) -> dict[str, Any]:
    """Copies what changed since the last run. Safe to call at any time and as often as wanted."""
    from app.plugins import notes_plugin

    account = _account_email()
    if not account:
        return {"ok": False, "reason": "not logged in", "written": [], "attached": 0}
    written: list[str] = []
    attached = 0
    async with _lock:
        state = _state(workspace_dir, account)
        wanted: list[tuple[str, str]] = []
        reminders = _reminders(workspace_dir)
        if reminders:
            wanted.append((BACKUP_NOTE, BACKUP_NOTE + "\n" + json.dumps({"reminders": reminders}, indent=2, ensure_ascii=False, sort_keys=True)))
        for name, content in _lesson_skills(workspace_dir).items():
            wanted.append((SKILL_PREFIX + name, SKILL_PREFIX + name + "\n" + content))
        wanted = [(t, x) for t, x in wanted if state.get(t) != _digest(x)]
        files = _local_files(workspace_dir)
        if wanted or files:
            index = await _index()
            for title, text in wanted:
                await _write_note(index, title, text)
                state[title] = _digest(text)
                written.append(title)
            if files:
                if FILES_NOTE not in index:
                    await _write_note(index, FILES_NOTE, FILES_NOTE)
                note_id = index[FILES_NOTE]["id"]
                have = {a.get("originalName") for a in await _with_session(lambda s: notes_plugin.list_attachments(s, note_id))}
                for name, path in files.items():
                    if name in have:
                        continue
                    if attached >= MAX_FILES_PER_RUN:
                        break
                    try:
                        await _with_session(lambda s, p=str(path), n=name: notes_plugin.attach_file(s, note_id, p, n))
                        attached += 1
                    except Exception as exc:  # noqa: BLE001 -- one bad file must not stop the rest
                        log_event("engine", "vault_backup_attach_failed", file=name, error=str(exc)[:200])
            _save_state(workspace_dir, account, state)
    if written or attached:
        log_event("engine", "vault_backup_done", notes=written, attached=attached)
    return {"ok": True, "written": written, "attached": attached}


async def restore_missing(workspace_dir: str) -> dict[str, Any]:
    """Fills in what this machine lacks from the notes; never overwrites anything that is here."""
    from app.plugins import notes_plugin

    account = _account_email()
    if not account:
        return {"ok": False, "reason": "not logged in", "restored": []}
    restored: list[str] = []
    async with _lock:
        index = await _index()
        state = _state(workspace_dir, account)
        entry = index.get(BACKUP_NOTE)
        if entry:
            note = await _with_session(lambda s: notes_plugin.get_note(s, entry["id"]))
            try:
                remote = json.loads(_body(note.get("text") or "")).get("reminders") or []
            except (ValueError, AttributeError):
                remote = []
            path = Path(workspace_dir) / "schedule.json"
            local = _read_json(path)
            local = local if isinstance(local, list) else []
            known = {r.get("id") for r in local if isinstance(r, dict)}
            added = [r for r in remote if isinstance(r, dict) and r.get("id") and r["id"] not in known]
            if added:
                _write_json_atomic(path, local + added)
                restored.append(f"reminders:{len(added)}")
        for title, entry in index.items():
            if not title.startswith(SKILL_PREFIX):
                continue
            topic = title[len(SKILL_PREFIX):]
            target = Path(workspace_dir, *LESSONS_DIR, topic, "SKILL.md")
            if not topic or "/" in topic or "\\" in topic or topic.startswith(".") or target.exists():
                continue
            note = await _with_session(lambda s, i=entry["id"]: notes_plugin.get_note(s, i))
            content = _body(note.get("text") or "")
            if content.strip():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                state[title] = _digest(title + "\n" + content)
                restored.append(title)
        files_entry = index.get(FILES_NOTE)
        if files_entry:
            fetched = 0
            for att in await _with_session(lambda s: notes_plugin.list_attachments(s, files_entry["id"])):
                sub, sep, name = str(att.get("originalName") or "").partition("__")
                if not sep or sub not in FILE_DIRS or not name or "/" in name or "\\" in name or name.startswith("."):
                    continue
                target = Path(workspace_dir) / sub / name
                if target.exists():
                    continue
                if fetched >= MAX_FILES_PER_RUN:
                    break
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    await _with_session(lambda s, f=att["filename"], t=str(target): notes_plugin.download_attachment(s, f, t))
                    fetched += 1
                    restored.append(f"{sub}__{name}")
                except Exception as exc:  # noqa: BLE001
                    log_event("engine", "vault_backup_download_failed", file=f"{sub}__{name}", error=str(exc)[:200])
        _save_state(workspace_dir, account, state)
    if restored:
        log_event("engine", "vault_backup_restored", count=len(restored))
    return {"ok": True, "restored": restored}


async def run_forever(workspace_dir: str) -> None:
    """Start-up task: drop the old reminder, restore what is missing as soon as there is a login, then a backup
    at the top of every hour (the old reminder's schedule) -- with no model involved."""
    try:
        remove_old_reminder(workspace_dir)
    except Exception as exc:  # noqa: BLE001
        log_event("engine", "vault_backup_old_reminder_failed", error=str(exc)[:200])
    restored = False
    while True:
        try:
            if _account_email():
                if not restored:
                    restored = bool((await restore_missing(workspace_dir)).get("ok"))
                await backup_now(workspace_dir)
            else:
                restored = False
        except Exception as exc:  # noqa: BLE001 -- Notes down: the next round tries again
            log_event("engine", "vault_backup_loop_error", error=str(exc)[:300])
        if _account_email() and restored:
            now = datetime.now()
            await asyncio.sleep((now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1) - now).total_seconds())
        else:
            await asyncio.sleep(30.0)
