"""Ports backend/src/control.ts -- thin wrappers around the bundled `claude`
CLI binary for Settings' auth (status/login/logout) and MCP (list/add/
remove) actions. A different, secondary code path from chat_session.py's
own query() transport (which the SDK resolves internally) -- these are
one-shot diagnostic/admin subprocess calls, not the main chat connection.
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Awaitable, Callable

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class CliNotFoundError(Exception):
    pass


def _find_bundled_cli() -> str | None:
    try:
        import claude_agent_sdk
    except Exception:
        return None
    cli_name = "claude.exe" if platform.system() == "Windows" else "claude"
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / cli_name
    return str(bundled) if bundled.exists() else None


def resolve_claude_exe() -> str:
    bundled = _find_bundled_cli()
    if bundled:
        return bundled
    which_hit = shutil.which("claude.exe") or shutil.which("claude")
    if which_hit:
        return which_hit
    home_local = Path.home() / ".local" / "bin" / "claude.exe"
    if home_local.exists():
        return str(home_local)
    raise CliNotFoundError("Claude Code CLI (claude.exe) not found -- bundled path, PATH, and ~/.local/bin all missed.")


async def _run(args: list[str], cwd: str) -> dict[str, object]:
    proc = await asyncio.create_subprocess_exec(
        resolve_claude_exe(), *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    stdout, stderr = await proc.communicate()
    return {
        "code": proc.returncode if proc.returncode is not None else -1,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


async def auth_status(cwd: str) -> dict[str, object]:
    return await _run(["auth", "status"], cwd)


async def auth_logout(cwd: str) -> dict[str, object]:
    return await _run(["auth", "logout"], cwd)


async def spawn_auth_login(cwd: str, on_chunk: Callable[[str, str], Awaitable[None]]) -> bool:
    """Starts the bundled CLI's own browser-based OAuth flow (`claude auth
    login --claudeai`). The CLI opens the system browser itself; stdout/
    stderr are streamed line-by-line via on_chunk(stream_name, text) so the
    caller can forward them as control_stream events (the fallback URL if
    the browser doesn't open automatically shows up this way), then
    resolves to whether the process exited 0."""

    async def pump(stream: asyncio.StreamReader | None, name: str) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            await on_chunk(name, chunk.decode("utf-8", errors="replace"))

    proc = await asyncio.create_subprocess_exec(
        resolve_claude_exe(), "auth", "login", "--claudeai", cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    await asyncio.gather(pump(proc.stdout, "stdout"), pump(proc.stderr, "stderr"))
    code = await proc.wait()
    return code == 0


async def mcp_list(cwd: str) -> dict[str, object]:
    return await _run(["mcp", "list"], cwd)


async def mcp_add(cwd: str, name: str, command: str, args: list[str] | None = None, scope: str = "local") -> dict[str, object]:
    return await _run(["mcp", "add", "--scope", scope, name, "--", command, *(args or [])], cwd)


async def mcp_remove(cwd: str, name: str) -> dict[str, object]:
    return await _run(["mcp", "remove", name], cwd)
