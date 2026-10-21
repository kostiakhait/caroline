"""secret_store -- secrets encrypted at rest, for anything more sensitive
than the plain JSON files the rest of this codebase already tolerates
(ratatosk-own-account.json etc -- all plaintext, see that module's own
doc comment; there was no encryption-at-rest helper anywhere in
backend-py before this). First user: the messenger integrations
(Slack/Telegram/Discord/WhatsApp/Signal credentials and session state),
per docs/MESSENGER_INTEGRATIONS_PLAN.md (2026-10-06).

Windows: DPAPI (CryptProtectData/CryptUnprotectData), encrypting to the
CURRENT WINDOWS USER ACCOUNT by design -- no extra password, no
cross-machine/cross-user portability, same single-machine/single-account
scope every other per-install state file in this codebase already has.

Linux (2026-10-21, Linux parity pass -- see docs/LINUX_PORT_PLAN.md and
the messenger-integrations testing plan): DPAPI has no cross-platform
equivalent, and a desktop keyring (libsecret/GNOME Keyring) needs a
logged-in desktop session to talk to -- not available on a disposable/
headless box, and not something this module should depend on existing.
Instead: a Fernet key generated once per secret file, written alongside
it with `0600` permissions (owner read/write only) -- the direct
non-Windows analog of DPAPI's own scope: no extra password, tied to
whoever can read files on this one machine, nothing fancier. `cryptography`
is already a pinned Linux dependency (requirements-linux.txt, pulled in
by `claude-agent-sdk`), so this adds no new dependency.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform == "win32":
    import win32crypt
else:
    from cryptography.fernet import Fernet


class SecretStoreUnavailable(Exception):
    """Raised when an encrypted file exists but can't be decrypted --
    wrong Windows user account / corrupted DPAPI blob on Windows, or a
    missing/corrupted key file on Linux. A caller silently treating this
    as "not configured" would send the user through setup again without
    ever surfacing why their existing secret stopped working."""


def _linux_key_path(path: Path) -> Path:
    return path.parent / f"{path.name}.key"


def _linux_key(path: Path) -> bytes:
    """One Fernet key per secret file, created on first use. Reused as-is
    on every later call for the same `path`."""
    key_path = _linux_key_path(path)
    if key_path.exists():
        return key_path.read_bytes()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    key_path.write_bytes(key)
    os.chmod(key_path, 0o600)
    return key


def encrypt_to_file(path: str | Path, data: bytes, description: str = "") -> None:
    """Overwrites `path` with the encrypted blob. Creates parent
    directories as needed -- callers don't need their own mkdir."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        blob = win32crypt.CryptProtectData(data, description, None, None, None, 0)
    else:
        blob = Fernet(_linux_key(path)).encrypt(data)
    path.write_bytes(blob)
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def decrypt_from_file(path: str | Path) -> bytes | None:
    """None if `path` doesn't exist yet -- a normal "not configured/linked
    yet" state, not a failure. Raises SecretStoreUnavailable if the file
    exists but can't be decrypted."""
    path = Path(path)
    if not path.exists():
        return None
    blob = path.read_bytes()
    if sys.platform == "win32":
        try:
            _description, data = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
        except Exception as exc:
            raise SecretStoreUnavailable(f"could not decrypt {path} (wrong Windows account or corrupted file)") from exc
        return data
    key_path = _linux_key_path(path)
    if not key_path.exists():
        raise SecretStoreUnavailable(f"secret key missing for {path} (expected {key_path})")
    try:
        return Fernet(key_path.read_bytes()).decrypt(blob)
    except Exception as exc:
        raise SecretStoreUnavailable(f"could not decrypt {path} (wrong/corrupted key)") from exc


def encrypt_text_to_file(path: str | Path, text: str, description: str = "") -> None:
    encrypt_to_file(path, text.encode("utf-8"), description)


def decrypt_text_from_file(path: str | Path) -> str | None:
    data = decrypt_from_file(path)
    return data.decode("utf-8") if data is not None else None


def messenger_secrets_dir(workspace_dir: str, service: str) -> Path:
    """workspace_dir/messengers/<service>/ -- one directory per messenger
    integration (a token file, a session file, or a whole auth-state
    folder for Baileys/signal-cli), per docs/MESSENGER_INTEGRATIONS_PLAN.md.
    Created on first use."""
    d = Path(workspace_dir) / "messengers" / service
    d.mkdir(parents=True, exist_ok=True)
    return d
