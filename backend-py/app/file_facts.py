"""What is known about a local path a tool call is about to send off the machine or
write a received file to -- plain facts for the turn.file.transfer microagent
(app/microagents/file_transfer.py), which makes the decision. Nothing here allows
or refuses anything.

Per explicit instruction (2026-10-22): no static blockers ("not this folder",
"never that file") -- microagents decide, code supplies what it knows. What code
does know for sure is where things live on this machine: the session state of
linked accounts, the SquirrelWisdom login, the installed app, the browsers'
profiles, the user's own folders. That knowledge goes to the agent as sentences.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.workspace_dir import WORKSPACE_DIR

_HOME = Path.home()
_LOCAL = Path(os.environ.get("LOCALAPPDATA") or _HOME / "AppData" / "Local")
_ROAMING = Path(os.environ.get("APPDATA") or _HOME / "AppData" / "Roaming")


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(path))))


def _under(path_n: str, root: str | Path) -> bool:
    root_n = _norm(root).rstrip("\\/")
    return path_n == root_n or path_n.startswith(root_n + os.sep)


def _places() -> list[tuple[Path, str]]:
    return [
        (Path(WORKSPACE_DIR) / "messengers", "the folder where Caroline's linked messenger accounts keep their login sessions and tokens"),
        (_HOME / ".mcp-notes", "the folder holding the SquirrelWisdom login (email and password)"),
        (_HOME / ".ssh", "the user's SSH keys folder"),
        (_HOME / ".claude", "the folder of Claude Code's own settings and login"),
        (_HOME / ".config", "a folder of program settings and logins"),
        (_LOCAL / "Google" / "Chrome" / "User Data", "Chrome's profile (its saved logins and cookies)"),
        (_LOCAL / "Microsoft" / "Edge" / "User Data", "Edge's profile (its saved logins and cookies)"),
        (_ROAMING / "Opera Software", "Opera's profile (its saved logins and cookies)"),
        (_ROAMING / "Mozilla", "Firefox's profile (its saved logins and cookies)"),
        (_ROAMING / "Microsoft" / "Credentials", "Windows' own credential store"),
        (_ROAMING / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup", "the Startup folder: what is in it runs at every sign-in"),
    ]


_KEY_NAME = re.compile(
    r"^(?:id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|.+\.(?:pem|key|pfx|p12|kdbx|ppk|session)|\.env(?:\..+)?"
    r"|\.?git-credentials|_?\.?netrc|credentials(?:\.json)?|appstate.*\.json|sw_service_keys.*\.py)$",
    re.IGNORECASE,
)


def path_facts(path: str) -> list[str]:
    """Plain sentences about `path`: which known place it is in, whether its name is
    that of a key or credentials file, whether a file is already there."""
    path_n = _norm(path)
    facts = [f"Resolved path: {os.path.realpath(os.path.expanduser(path))}"]
    for root, what in _places():
        if _under(path_n, root):
            facts.append(f"It is inside {what}.")
    if _under(path_n, _LOCAL / "Caroline") and not _under(path_n, WORKSPACE_DIR):
        facts.append("It is inside Caroline's installed program (its code, runtime and service keys), not her workspace.")
    elif _under(path_n, WORKSPACE_DIR) and not _under(path_n, Path(WORKSPACE_DIR) / "messengers"):
        facts.append("It is inside Caroline's workspace, where the files she makes and downloads are kept.")
    for name in ("Downloads", "Desktop", "Documents", "Pictures", "Videos", "Music"):
        if _under(path_n, _HOME / name):
            facts.append(f"It is inside the user's {name} folder.")
    if {"windowspowershell", "powershell"} & {part.lower() for part in Path(path_n).parts}:
        facts.append("It is inside a PowerShell profile folder: scripts there run whenever PowerShell starts.")
    if _KEY_NAME.match(os.path.basename(path_n)):
        facts.append("Its file name is that of a key, credentials or session file.")
    facts.append("A file already exists at this path." if os.path.isfile(path_n) else "No file exists at this path yet.")
    return facts


# Argument names that carry a local path, by what happens to the file.
_SEND_PATH_ARGS = ("path", "filePath", "file_path")
_SAVE_PATH_ARGS = ("savePath", "save_path", "saveTo", "outputPath")


def file_transfers_in(tool_name: str, args: dict[str, Any]) -> list[tuple[str, str]]:
    """(direction, path) for every local file this tool call sends out ("send") or
    writes a received file to ("save"). Sending is a tool whose name says it sends;
    email attachments are listed under "attachments"."""
    name = tool_name.lower()
    out: list[tuple[str, str]] = []
    if "send" in name:
        out += [("send", str(args[key])) for key in _SEND_PATH_ARGS if args.get(key)]
        for attachment in args.get("attachments") or []:
            if isinstance(attachment, dict) and attachment.get("path"):
                out.append(("send", str(attachment["path"])))
    if "download" in name or "save" in name:
        out += [("save", str(args[key])) for key in _SAVE_PATH_ARGS if args.get(key)]
    return out
