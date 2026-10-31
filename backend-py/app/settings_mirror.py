"""A mirror of Caroline's settings in the owner's Notes, so a reinstall (or a new machine) picks them up
again instead of asking for everything one more time.

Per explicit instruction (2026-10-25): Caroline keeps every setting in Notes. The layout follows the
convention already used for mailboxes (plugins/email_credentials.py):

- plain settings  -> one note per setting file, "settings:<name>" in folder "Caroline:Settings";
- secrets         -> "vault:settings:<name>" in folder "Caroline:Vault" (the keys of subscription.json that
                     are API keys, and ratatosk-own-account.json as a whole).

A note is its title line, then the JSON of the file (the format _note_creds() reads for mailboxes).

NOT mirrored, on purpose: the SquirrelWisdom login itself (Notes cannot be read without it -- it is typed
once after a reinstall), the Claude and Codex sign-in tokens (~/.claude, ~/.codex -- decided 2026-10-25 not
to leave them on the server), messenger sessions, window geometry. Mailbox passwords already have their
own vault notes (email_credentials.py).

Two directions, both safe to run at any time:
- push_all():       a file changed since the last push -> its note is written. A file that is missing is
                    never turned into a deletion of its note.
- restore_missing(): a note holds something the local file lacks -> the local file is filled in. A value that
                    is already there is never overwritten, so a setting changed here beats an older note.

Nothing here logs a value; only names and counts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.akinakes_channel import TAB_PREFIX as AKINAKES_TAB_PREFIX
from app.logging_setup import log_event

SETTINGS_FOLDER = os.environ.get("CAROLINE_SETTINGS_FOLDER", "Caroline:Settings")
VAULT_FOLDER = os.environ.get("CAROLINE_SETTINGS_VAULT_FOLDER", "Caroline:Vault")
STATE_FILE = "settings-mirror-state.json"      # which note text was last written, per note title
PUSH_DEBOUNCE_S = 4.0
PERIODIC_PUSH_S = 600.0
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class Item:
    name: str                                   # note title is settings:<name> / vault:settings:<name>
    filename: str                               # relative to the workspace
    secret_keys: tuple[str, ...] = ()           # top-level keys that go to the vault note instead
    all_secret: bool = False                    # the whole file goes to the vault note


STATIC_ITEMS: tuple[Item, ...] = (
    Item("persona", "persona.json"),
    Item("owner-profile", "owner_profile.json"),
    Item("continuous-dialogue", "continuousDialogue.json"),
    Item("visual-mode", "visualMode.json"),
    Item("local-stt", "local-stt-settings.json"),
    # edition:cut claude-openai answer source
    Item("subscription", "subscription.json", secret_keys=("ownAnthropicApiKey", "ownOpenaiApiKey")),
    # edition:end
    Item("wizard", "wizard.json"),
    Item("ratatosk-own-account", "ratatosk-own-account.json", all_secret=True),
)


def public_title(name: str) -> str:
    return f"settings:{name}"


def vault_title(name: str) -> str:
    return f"vault:settings:{name}"


def _items(workspace_dir: str, extra_names: tuple[str, ...] = ()) -> list[Item]:
    """The fixed items, plus one per chat-mode-<tab>.json (a tab's engine choice), found on disk and, when
    restoring, in the notes. An akinakes chain's tab is left out both ways (2026-10-31, explicit instruction): an
    agent makes one per bus task, and in the organization's account they would pile up and reach its other
    machines (seen on the second real run: the owner's desktop tabs restored into the agent, its chains pushed)."""
    items = list(STATIC_ITEMS)
    seen = {i.name for i in items}
    names = [p.stem for p in sorted(Path(workspace_dir).glob("chat-mode-*.json"))] + list(extra_names)
    for name in names:
        if name.startswith("chat-mode-" + AKINAKES_TAB_PREFIX):
            continue
        if name.startswith("chat-mode-") and _SAFE_NAME.match(name) and name not in seen:
            seen.add(name)
            items.append(Item(name, f"{name}.json"))
    return items


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _split(item: Item, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(plain part, secret part) of a settings file."""
    if item.all_secret:
        return {}, dict(data)
    secret = {k: v for k, v in data.items() if k in item.secret_keys}
    plain = {k: v for k, v in data.items() if k not in item.secret_keys}
    return plain, secret


def _note_text(title: str, part: dict[str, Any]) -> str:
    return title + "\n" + json.dumps(part, indent=2, ensure_ascii=False, sort_keys=True)


def _parse_note(text: str) -> dict[str, Any] | None:
    body = text.split("\n", 1)[1] if "\n" in text else ""
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(body[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _title_of(text: str) -> str:
    return (text or "").split("\n", 1)[0].strip()


# --- the state file: the text each note was last written with ----------------

def _state_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / STATE_FILE


def _load_state(workspace_dir: str, account: str) -> dict[str, str]:
    data = _read_json(_state_path(workspace_dir)) or {}
    return dict(data.get("notes") or {}) if data.get("account") == account else {}


def _save_state(workspace_dir: str, account: str, notes: dict[str, str]) -> None:
    try:
        _write_json_atomic(_state_path(workspace_dir), {"account": account, "notes": notes})
    except OSError as exc:
        log_event("engine", "settings_mirror_state_save_failed", error=str(exc)[:200])


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Notes access (the same helpers the notes tools use) ----------------------

async def _with_session(fn: Any) -> Any:
    from app.plugins import notes_plugin

    return await notes_plugin._sessions.with_session(fn)


async def _index(folder: str) -> dict[str, dict[str, Any]]:
    """title -> index entry of the live notes in `folder`."""
    from app.plugins import notes_plugin

    entries = await _with_session(lambda s: notes_plugin.list_notes(s, folder=folder))
    return {_title_of(e.get("text", "")): e for e in entries}


def _account_email() -> str | None:
    from app.plugins.sw_api import load_credentials

    creds = load_credentials()
    return creds["email"] if creds else None


_lock = asyncio.Lock()


async def push_all(workspace_dir: str) -> dict[str, Any]:
    """Writes the notes of every settings file that changed since its last push."""
    from app.plugins import notes_plugin

    account = _account_email()
    if not account:
        return {"ok": False, "reason": "not logged in", "written": []}
    written: list[str] = []
    async with _lock:
        state = _load_state(workspace_dir, account)
        wanted: list[tuple[str, str, str]] = []            # (folder, title, text) that differ from the last push
        for item in _items(workspace_dir):
            data = _read_json(Path(workspace_dir) / item.filename)
            if data is None:
                continue
            plain, secret = _split(item, data)
            for folder, title, part in ((SETTINGS_FOLDER, public_title(item.name), plain), (VAULT_FOLDER, vault_title(item.name), secret)):
                if not part:
                    continue
                text = _note_text(title, part)
                if state.get(title) != _digest(text):
                    wanted.append((folder, title, text))
        if wanted:
            indexes = {folder: await _index(folder) for folder in {w[0] for w in wanted}}
            for folder, title, text in wanted:
                existing = indexes[folder].get(title)
                if existing:
                    await _with_session(lambda s, i=existing["id"], t=text, f=folder: notes_plugin.update_note(s, i, text=t, folder=f))
                else:
                    await _with_session(lambda s, t=text, f=folder: notes_plugin.create_note(s, t, folder=f))
                state[title] = _digest(text)
                written.append(title)
            _save_state(workspace_dir, account, state)
    if written:
        log_event("engine", "settings_mirror_pushed", count=len(written), notes=written)
    return {"ok": True, "written": written}


async def restore_missing(workspace_dir: str) -> dict[str, Any]:
    """Fills what the local settings files lack from the notes; never overwrites a value that is there."""
    from app.plugins import notes_plugin

    account = _account_email()
    if not account:
        return {"ok": False, "reason": "not logged in", "restored": []}
    restored: list[str] = []
    async with _lock:
        settings_index = await _index(SETTINGS_FOLDER)
        vault_index = await _index(VAULT_FOLDER)
        extra = tuple(t[len("settings:"):] for t in settings_index if t.startswith("settings:chat-mode-"))
        state = _load_state(workspace_dir, account)
        for item in _items(workspace_dir, extra):
            remote: dict[str, Any] = {}
            for index, title in ((settings_index, public_title(item.name)), (vault_index, vault_title(item.name))):
                entry = index.get(title)
                if not entry:
                    continue
                note = await _with_session(lambda s, i=entry["id"]: notes_plugin.get_note(s, i))
                part = _parse_note(note.get("text") or "")
                if part:
                    remote.update(part)
                    state[title] = _digest(_note_text(title, part))
            if not remote:
                continue
            path = Path(workspace_dir) / item.filename
            local = _read_json(path) or {}
            merged = {**remote, **local}                  # a value that is already here wins over the note
            if merged != local:
                _write_json_atomic(path, merged)
                restored.append(item.name)
        _save_state(workspace_dir, account, state)
    if restored:
        log_event("engine", "settings_mirror_restored", count=len(restored), items=restored)
    return {"ok": True, "restored": restored}


async def sync_now(workspace_dir: str) -> dict[str, Any]:
    """Restore, then push: what a fresh install needs right after the login, and what start-up does."""
    try:
        restored = await restore_missing(workspace_dir)
        pushed = await push_all(workspace_dir)
        return {"ok": bool(restored.get("ok") and pushed.get("ok")), "restored": restored.get("restored", []), "written": pushed.get("written", [])}
    except Exception as exc:  # noqa: BLE001 -- Notes down or the session refused: settings still work locally
        log_event("engine", "settings_mirror_sync_failed", error=str(exc)[:300])
        return {"ok": False, "reason": str(exc)[:200], "restored": [], "written": []}


_push_task: "asyncio.Task[None] | None" = None


def schedule_push(workspace_dir: str, delay_s: float = PUSH_DEBOUNCE_S) -> None:
    """Called after anything changed settings; several changes close together become one push."""
    global _push_task
    if _push_task is not None and not _push_task.done():
        return

    async def _later() -> None:
        await asyncio.sleep(delay_s)
        try:
            await push_all(workspace_dir)
        except Exception as exc:  # noqa: BLE001 -- the next change or the periodic push tries again
            log_event("engine", "settings_mirror_push_failed", error=str(exc)[:300])

    try:
        _push_task = asyncio.get_running_loop().create_task(_later())
    except RuntimeError:
        pass  # no running loop (a synchronous caller): the periodic push still catches it


_background: "set[asyncio.Task[Any]]" = set()   # strong references: a bare task may be collected mid-way


def schedule_sync(workspace_dir: str) -> None:
    """A sync in the background -- right after a login, when the settings of a previous install may be waiting."""
    try:
        task = asyncio.get_running_loop().create_task(sync_now(workspace_dir))
    except RuntimeError:
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


async def run_forever(workspace_dir: str) -> None:
    """Start-up task: one sync as soon as there is a login, then a push every PERIODIC_PUSH_S for the settings
    that changed without going through a control op."""
    synced = False
    while True:
        try:
            if _account_email():
                if not synced:
                    synced = bool((await sync_now(workspace_dir)).get("ok"))
                else:
                    await push_all(workspace_dir)
            else:
                synced = False                              # logged out: sync again after the next login
        except Exception as exc:  # noqa: BLE001
            log_event("engine", "settings_mirror_loop_error", error=str(exc)[:300])
        await asyncio.sleep(30.0 if not synced else PERIODIC_PUSH_S)
