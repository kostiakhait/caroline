"""secret_store -- DPAPI-encrypted secrets at rest, for anything more
sensitive than the plain JSON files the rest of this codebase already
tolerates (ratatosk-own-account.json etc -- all plaintext, see that
module's own doc comment; there was no encryption-at-rest helper
anywhere in backend-py before this). First user: the messenger
integrations (Slack/Telegram/Discord/WhatsApp/Signal credentials and
session state), per docs/MESSENGER_INTEGRATIONS_PLAN.md (2026-10-06).

Windows-only, same as win32-dependent automation plugins already are
(see requirements-linux.txt's own comment on why pywin32 is absent
from the Linux manifest -- it's bundled in the shipped Windows runtime,
confirmed live). DPAPI's CryptProtectData/CryptUnprotectData encrypt
to the CURRENT WINDOWS USER ACCOUNT by design -- no extra password, no
cross-machine/cross-user portability, same single-machine/single-account
scope every other per-install state file in this codebase already has.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.platform == "win32":
    import win32crypt


class SecretStoreUnavailable(Exception):
    """Raised on a non-Windows platform -- DPAPI has no equivalent there.
    A messenger plugin that needs this should fail to register the same
    way any other Windows-only plugin already does on Linux (see
    docs/LINUX_PORT_PLAN.md), not crash the whole backend."""


def _require_windows() -> None:
    if sys.platform != "win32":
        raise SecretStoreUnavailable("secret_store is Windows-only (DPAPI has no cross-platform equivalent)")


def encrypt_to_file(path: str | Path, data: bytes, description: str = "") -> None:
    """Overwrites `path` with the DPAPI-encrypted blob. Creates parent
    directories as needed -- callers don't need their own mkdir."""
    _require_windows()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = win32crypt.CryptProtectData(data, description, None, None, None, 0)
    path.write_bytes(blob)


def decrypt_from_file(path: str | Path) -> bytes | None:
    """None if `path` doesn't exist yet -- a normal "not configured/linked
    yet" state, not a failure. Raises if the file exists but can't be
    decrypted (wrong user account, corrupted file) -- a caller silently
    treating that as "not configured" would send the user through setup
    again without ever surfacing why their existing secret stopped working."""
    _require_windows()
    path = Path(path)
    if not path.exists():
        return None
    blob = path.read_bytes()
    _description, data = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
    return data


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
