"""OpenAI *ChatGPT/device* sign-in and account state, driven through
short-lived Codex app-server processes (the same bundled exe the tabs'
engines use, against the shared ~/.codex -- see openai_mode.codex_home_dir).

This is the OAuth-shaped half of the OpenAI source, the counterpart of
Claude's `claude auth login`/`claude auth status` (subscription_mode.py).
The OTHER half -- a pasted API key -- is intentionally NOT here: it lives in
openai_mode.py as its own independent, Caroline-only setting (mirroring
subscription_mode's own-anthropic-key), never touches this module or
~/.codex/auth.json. See openai_mode.py's module docstring for why.

Two sign-in methods: "chatgpt" (browser sign-in with a ChatGPT subscription)
and "device" (the same with a code to type elsewhere, for when the browser
flow cannot complete). A browser sign-in needs its process alive until the
user finishes, so one is kept in `_login` and polled through status(); it is
torn down on completion, cancel or timeout.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from app.engines.codex_rpc import CodexRpcClient, CodexRpcError, build_env, codex_argv
from app.logging_setup import log_event
from app.openai_mode import codex_exe_path, codex_home_dir, openai_unavailable_reason

LOGIN_TIMEOUT_S = 600.0
_OP_TIMEOUT_S = 30.0

_login: dict[str, Any] | None = None  # {rpc, login_id, state, error, started, url, user_code, method}
_last_finished: dict[str, Any] | None = None  # public state of the last sign-in that ended, until the next starts


async def _open_rpc(workspace_dir: str, on_notification=None) -> CodexRpcClient:
    exe = codex_exe_path()
    if exe is None:
        raise RuntimeError("Codex is not installed")
    home = codex_home_dir(workspace_dir)
    os.makedirs(home, exist_ok=True)

    async def _no_requests(method: str, params: dict[str, Any]) -> Any:
        raise CodexRpcError(-32601, f"unsupported server request: {method}")

    rpc = CodexRpcClient(
        codex_argv(exe), build_env(home), on_notification=on_notification or (lambda m, p: None),
        on_server_request=_no_requests, on_closed=lambda: None, label="codex-account",
    )
    rpc.start()
    try:
        await rpc.request("initialize", {
            "clientInfo": {"name": "caroline", "title": "Caroline", "version": "1"},
            "capabilities": {"experimentalApi": True},
        }, timeout=_OP_TIMEOUT_S)
        await rpc.notify("initialized")
    except BaseException:
        await rpc.kill_and_wait()
        raise
    return rpc


async def status(workspace_dir: str) -> dict[str, Any]:
    """{installed, loggedIn, method, email, plan, models[], login{...}}."""
    result: dict[str, Any] = {"installed": codex_exe_path() is not None, "loggedIn": False, "login": _login_public()}
    reason = openai_unavailable_reason(workspace_dir)
    if not result["installed"]:
        result["unavailableReason"] = reason
        return result
    rpc = await _open_rpc(workspace_dir)
    try:
        acct = await rpc.request("account/read", {"refreshToken": False}, timeout=_OP_TIMEOUT_S)
        account = (acct or {}).get("account")
        if account:
            result["loggedIn"] = True
            result["method"] = account.get("type")
            result["email"] = account.get("email")
            result["plan"] = account.get("planType")
        try:
            models = (await rpc.request("model/list", {}, timeout=_OP_TIMEOUT_S) or {}).get("data") or []
            result["models"] = [
                {"id": m.get("id"), "displayName": m.get("displayName") or m.get("id"), "isDefault": bool(m.get("isDefault"))}
                for m in models if not m.get("hidden")
            ]
        except Exception as exc:
            log_event("engine", "openai_model_list_failed", error=str(exc))
    finally:
        await rpc.kill_and_wait()
    return result


def _login_public() -> dict[str, Any] | None:
    source = _login if _login is not None else _last_finished
    if source is None:
        return None
    return {k: source.get(k) for k in ("method", "state", "error", "url", "userCode")}


async def _end_login() -> None:
    global _login, _last_finished
    login, _login = _login, None
    if login is None:
        return
    _last_finished = dict(login) if login.get("state") != "pending" else None
    if login.get("rpc"):
        await login["rpc"].kill_and_wait()


async def cancel_login() -> None:
    await _end_login()


async def start_login(workspace_dir: str, method: str) -> dict[str, Any]:
    """Begins a ChatGPT/device sign-in and returns its public state: a url
    (and, for "device", a userCode); finishes later -- poll status()."""
    global _login, _last_finished
    if method not in ("chatgpt", "device"):
        raise ValueError(f"unknown sign-in method: {method}")
    await _end_login()
    _last_finished = None

    login: dict[str, Any] = {"method": method, "state": "pending", "error": None, "url": None, "userCode": None, "started": time.monotonic()}

    def _on_notification(name: str, params: dict[str, Any]) -> None:
        if name == "account/login/completed" and _login is login:
            login["state"] = "done" if params.get("success") else "error"
            login["error"] = None if params.get("success") else (params.get("error") or "Sign-in was not completed")
            asyncio.get_running_loop().call_later(2.0, lambda: asyncio.ensure_future(_finish_if_current(login)))

    rpc = await _open_rpc(workspace_dir, on_notification=_on_notification)
    login["rpc"] = rpc
    _login = login
    try:
        params = {"type": "chatgptDeviceCode"} if method == "device" else {"type": "chatgpt"}
        resp = await rpc.request("account/login/start", params, timeout=_OP_TIMEOUT_S) or {}
    except BaseException:
        await _end_login()
        raise
    login["login_id"] = resp.get("loginId")
    login["url"] = resp.get("authUrl") or resp.get("verificationUrl")
    login["userCode"] = resp.get("userCode")
    asyncio.get_running_loop().call_later(LOGIN_TIMEOUT_S, lambda: asyncio.ensure_future(_expire(login)))
    return _login_public() or {}


async def _finish_if_current(login: dict[str, Any]) -> None:
    if _login is login:
        await _end_login()


async def _expire(login: dict[str, Any]) -> None:
    if _login is login and login["state"] == "pending":
        login["state"] = "error"
        login["error"] = "Sign-in timed out"
        await _end_login()


async def logout(workspace_dir: str) -> None:
    await _end_login()
    rpc = await _open_rpc(workspace_dir)
    try:
        await rpc.request("account/logout", {}, timeout=_OP_TIMEOUT_S)
    finally:
        await rpc.kill_and_wait()
