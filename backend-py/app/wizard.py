"""The start-up wizard's backend (page: app/assets/wizard.html, served by main.py at /wizard).

Three steps, in this order:
1. an account -- an e-mail and a password, with a way to register. The page does not say this is the
   SquirrelWisdom account; it is, and it is the one thing Caroline cannot work without (not skippable).
2. the user's own Claude and OpenAI (Codex) subscriptions -- connected, or declared absent.
3. the mailboxes and messenger accounts of Caroline herself and of her owner -- only what the notes (and
   what the wizard has recorded earlier) do not already hold.

What the user answers is kept in <workspace>/wizard.json, which the settings mirror (settings_mirror.py)
writes to Notes -- so a reinstall that logs in gets the answers back and the wizard has nothing left to ask.
The sign-in tokens of Claude and Codex are NOT kept there (they stay in ~/.claude and ~/.codex).

Nothing here logs a password; the secrets go to the same stores the rest of Caroline already uses."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from app import settings_mirror
from app.logging_setup import log_event
from app.login_api import is_logged_in, logged_in_email, register_and_save_login, verify_and_save_login

ROLES = ("caroline", "owner")
MESSENGERS = ("telegram", "whatsapp", "signal")
# What step 3 wants, each satisfied by an entry or declined by the user with "none".
NEEDS = ("caroline_mail", "owner_mail", "caroline_messengers", "owner_messengers")

_claude_login_task: "asyncio.Task[Any] | None" = None


def _path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "wizard.json"


def load(workspace_dir: str) -> dict[str, Any]:
    try:
        data = json.loads(_path(workspace_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(workspace_dir: str, data: dict[str, Any]) -> None:
    path = _path(workspace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    settings_mirror.schedule_push(workspace_dir)


def _email_ok(address: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address or ""))


async def _vault_mailboxes() -> list[str]:
    """Addresses that have a vault:email:<address> note (a mailbox the notes already hold)."""
    try:
        from app.plugins import email_credentials

        index = await settings_mirror._index(email_credentials.VAULT_FOLDER)
    except Exception as exc:  # noqa: BLE001 -- the wizard still works from what it has recorded itself
        log_event("engine", "wizard_vault_list_failed", error=str(exc)[:200])
        return []
    out = []
    for title in index:
        m = re.match(r"^vault:email:([^\s]+@[^\s]+)", title)
        if m:
            out.append(m.group(1).lower())
    return sorted(set(out))


async def _claude_signed_in() -> bool:
    try:
        from app.subscription_mode import get_claude_auth_status

        # The status comes from the shared cache (a wedged CLI is killed after 45 s there); the wizard does not
        # wait that long -- shield keeps a timeout here from cancelling the refresh others are waiting on.
        r = await asyncio.wait_for(asyncio.shield(get_claude_auth_status()), timeout=8.0)
        return bool(json.loads(r.get("stdout") or "{}").get("loggedIn"))
    except Exception:  # noqa: BLE001 -- unknown counts as not signed in
        return False


def _openai_signed_in(workspace_dir: str) -> bool:
    try:
        from app.openai_mode import openai_available

        return bool(openai_available(workspace_dir))
    except Exception:  # noqa: BLE001
        return False


def _needs(data: dict[str, Any], vault_addresses: list[str]) -> dict[str, bool]:
    """For each of NEEDS: True when it is still missing."""
    roles: dict[str, str] = data.get("emailRoles") or {}
    declined = set(data.get("declined") or [])
    contacts: dict[str, dict[str, str]] = data.get("contacts") or {}
    missing = {}
    for role in ROLES:
        has_mail = any(r == role for r in roles.values())
        missing[f"{role}_mail"] = not has_mail and f"{role}_mail" not in declined
        has_messenger = any(str(v).strip() for k, v in (contacts.get(role) or {}).items() if k in MESSENGERS)
        missing[f"{role}_messengers"] = not has_messenger and f"{role}_messengers" not in declined
    return missing


async def get_state(workspace_dir: str) -> dict[str, Any]:
    data = load(workspace_dir)
    logged_in = is_logged_in()
    claude = await _claude_signed_in() if logged_in else False
    openai = _openai_signed_in(workspace_dir)
    vault = await _vault_mailboxes() if logged_in else []
    roles = data.get("emailRoles") or {}
    unassigned = [a for a in vault if a not in roles]
    needs = _needs(data, vault) if logged_in else {n: True for n in NEEDS}
    subscriptions_done = bool(data.get("subscriptionsAnswered")) or claude or openai
    steps = [
        {"id": "account", "status": "done" if logged_in else "todo"},
        {"id": "subscriptions", "status": "done" if subscriptions_done else "todo"},
        {"id": "accounts", "status": "done" if not any(needs.values()) else "todo"},
    ]
    return {
        "loggedIn": logged_in,
        "email": logged_in_email(),
        "completed": bool(data.get("completed")),
        "dismissed": bool(data.get("dismissed")),
        "needed": (not logged_in) or not (data.get("completed") or data.get("dismissed")),
        "steps": steps,
        "claude": {"signedIn": claude, "pending": _claude_login_task is not None and not _claude_login_task.done()},
        "openai": {"signedIn": openai},
        "needs": needs,
        "mailboxes": {"found": vault, "roles": roles, "unassigned": unassigned, "createdHere": data.get("createdHere") or []},
        "contacts": data.get("contacts") or {},
    }


async def login(workspace_dir: str, email: str, password: str, register: bool) -> dict[str, Any]:
    """Signs in or registers, then waits for the settings sync, so the state that comes back already has what a
    previous install left in Notes."""
    email = (email or "").strip()
    if not _email_ok(email) or not password:
        return {"ok": False, "error": "invalid_input"}
    result = await (register_and_save_login(email, password) if register else verify_and_save_login(email, password))
    if not result.ok:
        return {"ok": False, "error": str(result.error or "failed")[:300]}
    sync = await settings_mirror.sync_now(workspace_dir)
    return {"ok": True, "restored": sync.get("restored", []), "state": await get_state(workspace_dir)}


async def start_claude_login(workspace_dir: str) -> dict[str, Any]:
    """`claude auth login` opens the system browser itself; the state's claude.signedIn flips when it is done."""
    global _claude_login_task
    from app.cli_control import spawn_auth_login
    from app.subscription_mode import invalidate_claude_auth_status

    if _claude_login_task is not None and not _claude_login_task.done():
        return {"ok": True, "already": True}

    async def _run() -> None:
        async def _discard(_stream: str, _text: str) -> None:
            return None

        try:
            await spawn_auth_login(workspace_dir, _discard)
        except Exception as exc:  # noqa: BLE001
            log_event("engine", "wizard_claude_login_failed", error=str(exc)[:200])
        finally:
            invalidate_claude_auth_status()

    _claude_login_task = asyncio.get_running_loop().create_task(_run())
    return {"ok": True}


def answer(workspace_dir: str, field: str, value: Any) -> dict[str, Any]:
    """One answer of the user. Only the fields the wizard owns are accepted."""
    data = load(workspace_dir)
    if field == "subscriptionsAnswered":
        data["subscriptionsAnswered"] = bool(value)
    elif field in ("completed", "dismissed"):
        data[field] = bool(value)
    elif field == "emailRole":
        address, role = str((value or {}).get("address", "")).strip().lower(), (value or {}).get("role")
        if not _email_ok(address) or role not in ROLES:
            return {"ok": False, "error": "invalid_input"}
        data.setdefault("emailRoles", {})[address] = role
    elif field == "contact":
        role, kind, handle = (value or {}).get("role"), (value or {}).get("kind"), str((value or {}).get("handle", "")).strip()[:200]
        if role not in ROLES or kind not in MESSENGERS:
            return {"ok": False, "error": "invalid_input"}
        data.setdefault("contacts", {}).setdefault(role, {})[kind] = handle
    elif field == "removeContact":
        role, kind = (value or {}).get("role"), (value or {}).get("kind")
        if role not in ROLES or kind not in MESSENGERS:
            return {"ok": False, "error": "invalid_input"}
        (data.get("contacts") or {}).get(role, {}).pop(kind, None)
    elif field == "decline":
        need = str(value or "")
        if need not in NEEDS:
            return {"ok": False, "error": "invalid_input"}
        data["declined"] = sorted(set(data.get("declined") or []) | {need})
    else:
        return {"ok": False, "error": "unknown_field"}
    save(workspace_dir, data)
    return {"ok": True}


async def save_mailbox(workspace_dir: str, body: dict[str, Any]) -> dict[str, Any]:
    """Checks a mailbox by listing its folders (the same call the e-mail tools make); a mailbox that answers is
    kept (see below) and recorded as Caroline's or the owner's."""
    from app.plugins import email_plugin

    address = str(body.get("address") or "").strip().lower()
    role = body.get("role")
    creds = {
        "address": address,
        "password": str(body.get("password") or ""),
        "imapHost": str(body.get("imapHost") or "").strip(),
        "imapPort": int(body.get("imapPort") or 993),
        "smtpHost": str(body.get("smtpHost") or "").strip(),
        "smtpPort": int(body.get("smtpPort") or 465),
    }
    if not _email_ok(address) or role not in ROLES or not creds["password"] or not creds["imapHost"]:
        return {"ok": False, "error": "invalid_input"}
    known = address in await _vault_mailboxes()                   # a mailbox the notes already held is not "added here"
    try:
        reply = await email_plugin.email_list_folders(dict(creds), None)
    except Exception as exc:  # noqa: BLE001 -- the reason goes back to the page, the password never does
        log_event("engine", "wizard_mailbox_check_failed", address=address, error=str(exc)[:200])
        return {"ok": False, "error": "mailbox_check_failed", "detail": str(exc)[:300]}
    if reply.get("is_error"):
        return {"ok": False, "error": "mailbox_check_failed", "detail": str(reply.get("text") or "")[:300]}
    # email_list_folders keeps the credentials itself (local copy, vault note, memory) once the call worked.
    answer(workspace_dir, "emailRole", {"address": address, "role": role})
    if not known:
        data = load(workspace_dir)
        data["createdHere"] = sorted(set(data.get("createdHere") or []) | {address})
        save(workspace_dir, data)
    return {"ok": True}


async def remove_mailbox(workspace_dir: str, address: str) -> dict[str, Any]:
    """Takes a mailbox out of the wizard's list. One the wizard itself added also loses what was kept for it (so a
    mailbox added by mistake leaves no trace but a memory line); one that was already in the notes is only unassigned,
    its credentials stay."""
    from app.plugins import email_credentials

    address = (address or "").strip().lower()
    data = load(workspace_dir)
    roles = dict(data.get("emailRoles") or {})
    if address not in roles:
        return {"ok": False, "error": "unknown_mailbox"}
    roles.pop(address)
    created_here = list(data.get("createdHere") or [])
    forget = address in created_here
    data["emailRoles"] = roles
    data["createdHere"] = [a for a in created_here if a != address]
    save(workspace_dir, data)
    gone = await email_credentials.forget(address) if forget else {"local": False, "note": False}
    return {"ok": True, "credentialsDeleted": bool(gone["local"] or gone["note"])}
