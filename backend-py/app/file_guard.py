"""Hard limits for the tools that move files across the machine's edge: which
local file a messaging or mail tool may send out, and where a downloaded
attachment may be written.

Plain code, no model, on purpose. These tools are reached from turns a third
party starts -- anyone can message Caroline's own Messenger/WhatsApp/Signal/
Telegram account, or mail the owner -- and a model can be talked into things.
Whatever it is talked into, two rules hold here:

- what leaves never includes secrets: the session state of every linked
  account (workspace/messengers/), the SquirrelWisdom login (~/.mcp-notes),
  the installed app (its service keys, its browser profile), SSH and cloud
  keys, the browsers' own profiles, Windows' credential stores;
- what arrives only lands in the places a person keeps files (the workspace,
  Downloads, Desktop, Documents, Pictures, Videos, Music), never in a place
  that runs it later (a Startup folder, a PowerShell profile) and never over
  an existing file.

Both return None when the path is fine, else one sentence for the model.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.workspace_dir import WORKSPACE_DIR

_HOME = Path.home()
_LOCAL = Path(os.environ.get("LOCALAPPDATA") or _HOME / "AppData" / "Local")
_ROAMING = Path(os.environ.get("APPDATA") or _HOME / "AppData" / "Roaming")


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(path))))


def _under(path_n: str, root: str | Path) -> bool:
    root_n = _norm(root).rstrip("\\/")
    return path_n == root_n or path_n.startswith(root_n + os.sep)


def _secret_roots() -> list[Path]:
    return [
        Path(WORKSPACE_DIR) / "messengers",
        _HOME / ".mcp-notes",
        _HOME / ".ssh", _HOME / ".gnupg", _HOME / ".aws", _HOME / ".azure", _HOME / ".kube", _HOME / ".docker",
        _HOME / ".claude", _HOME / ".config",
        _LOCAL / "Google" / "Chrome" / "User Data", _LOCAL / "Microsoft" / "Edge" / "User Data",
        _LOCAL / "BraveSoftware", _ROAMING / "Opera Software", _ROAMING / "Mozilla",
        _ROAMING / "Microsoft" / "Credentials", _LOCAL / "Microsoft" / "Credentials",
        _ROAMING / "Microsoft" / "Protect", _LOCAL / "Microsoft" / "Vault",
    ]


# The installed app lives next to the workspace (%LOCALAPPDATA%\Caroline\app-*,
# runtime\, state.json): all of it is off limits except the workspace itself,
# where the files Caroline makes and downloads are.
_APP_ROOT = _LOCAL / "Caroline"

_SECRET_NAME = re.compile(
    r"^(?:id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|.+\.(?:pem|key|pfx|p12|kdbx|ppk|session)|\.env(?:\..+)?"
    r"|\.?git-credentials|_?\.?netrc|credentials(?:\.json)?|appstate.*\.json|sw_service_keys.*\.py)$",
    re.IGNORECASE,
)


def _is_secret(path_n: str) -> bool:
    if any(_under(path_n, root) for root in _secret_roots()):
        return True
    if _under(path_n, _APP_ROOT) and not _under(path_n, WORKSPACE_DIR):
        return True
    return bool(_SECRET_NAME.match(os.path.basename(path_n)))


def outbound_file_refusal(path: str) -> str | None:
    """For a file about to be sent off the machine (a messenger file, a mail attachment)."""
    if _is_secret(_norm(path)):
        return (f'Refused to send "{path}": it is, or lies in a folder that holds, account credentials, keys or '
                "session data, which never leave this computer through a message.")
    return None


def _download_roots() -> list[Path]:
    roots = [Path(WORKSPACE_DIR)] + [_HOME / name for name in ("Downloads", "Desktop", "Documents", "Pictures", "Videos", "Music")]
    for onedrive in _HOME.glob("OneDrive*"):
        roots += [onedrive / name for name in ("Downloads", "Desktop", "Documents", "Pictures", "Videos", "Music")]
    return roots


def download_target_refusal(path: str) -> str | None:
    """For a downloaded attachment the model asked to save at `path`."""
    path_n = _norm(path)
    if not any(_under(path_n, root) for root in _download_roots()) or _is_secret(path_n):
        return (f'Refused to save to "{path}": a downloaded attachment may only be saved under the workspace or the '
                "user's Downloads, Desktop, Documents, Pictures, Videos or Music folders (or left at its default place).")
    parts = {part.lower() for part in Path(path_n).parts}
    if parts & {"windowspowershell", "powershell", "startup"}:
        return f'Refused to save to "{path}": files there run on their own (a shell profile or startup folder).'
    if os.path.exists(path_n):
        return f'Refused to save to "{path}": a file is already there; choose a new name rather than replacing it.'
    return None
