"""Tripwire for the 2026-10-28 fixes after a live incident on tab 2 (a password request that failed with an
English "can't answer" while the SW server was returning 502, with no narrator and a notes search that missed
an existing note): an empty notes folder means every folder, gateway errors (429/501/502/503) are retried by
every SW client, the fixed "unavailable" text exists per language, and an sw turn resets the narrator's
suppress flag.

Run directly: python test_sw_outage_fixes.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from app import microagent_client as mc
from app import small_model_engine as sme
from app.plugins import memory_plugin, notes_plugin, sw_api
from app.chat_session_mixins.small_model_turn import SmallModelTurnMixin

_FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    if not condition:
        _FAILURES.append(name)
        print(f"FAIL: {name}")
    else:
        print(f"ok:   {name}")


# --- notes: an empty folder is "no restriction" ------------------------------------------------------------------
INDEX = {
    "a": {"text": "vault:email:test@navlink.net\n{}", "folder": "Somewhere/Else", "updatedAt": 2},
    "b": {"text": "vault:email:khait@navlink.net\n{}", "folder": "Caroline:Vault", "updatedAt": 1},
    "c": {"text": "root note test@navlink.net", "folder": "", "updatedAt": 3},
}


async def _fake_read_index(_session: str) -> dict:
    return INDEX


notes_plugin._read_index = _fake_read_index  # type: ignore[assignment]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


check("blank folder filters to None", notes_plugin._folder_filter("") is None and notes_plugin._folder_filter("  ") is None)
check("folder filter keeps a real path", notes_plugin._folder_filter("Caroline:Vault") == "Caroline:Vault")
found = run(notes_plugin.search_notes("s", "test@navlink.net", folder=""))
check("search with folder '' finds notes in every folder", {e["id"] for e in found} == {"a", "c"})
check("search with a folder stays scoped", [e["id"] for e in run(notes_plugin.search_notes("s", "navlink.net", folder="Caroline:Vault"))] == ["b"])
check("list with folder '' lists every folder", len(run(notes_plugin.list_notes("s", folder=""))) == 3)


# --- gateway errors are retried ----------------------------------------------------------------------------------
class _Resp:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload if payload is not None else {".status": "ok"}
        self.text = "<html>502</html>"

    def json(self) -> dict:
        return self._payload


def _fake_clients(statuses: list[int]):
    calls = {"n": 0}

    def reply() -> _Resp:
        status = statuses[min(calls["n"], len(statuses) - 1)]
        calls["n"] += 1
        return _Resp(status)

    class AsyncClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return reply()

    class Client:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return reply()

    return calls, AsyncClient, Client


sw_api.RETRY_PAUSES_S = (0.0, 0.0, 0.0, 0.0)
memory_plugin.RETRY_PAUSES_S = (0.0, 0.0, 0.0, 0.0)
_real_async, _real_sync = httpx.AsyncClient, httpx.Client

calls, A, C = _fake_clients([502, 502, 200])
httpx.AsyncClient = A  # type: ignore[misc]
check("sw_api retries 502 then succeeds", run(sw_api._post_json({"command": "x"}))[".status"] == "ok" and calls["n"] == 3)

calls, A, C = _fake_clients([501] * 10)
httpx.AsyncClient = A  # type: ignore[misc]
try:
    run(sw_api._post_json({"command": "x"}))
    check("sw_api gives up after the last pause", False)
except sw_api.SwApiError:
    check("sw_api gives up after the last pause (1 + 4 retries)", calls["n"] == 5)

calls, A, C = _fake_clients([502] * 10)
httpx.AsyncClient = A  # type: ignore[misc]
try:
    run(sw_api._post_json({"command": "x"}, retries=1))
except sw_api.SwApiError:
    pass
check("retries=1 (the narrator) makes a single attempt", calls["n"] == 1)

calls, A, C = _fake_clients([500, 200])
httpx.AsyncClient = A  # type: ignore[misc]
try:
    run(sw_api._post_json({"command": "x"}))
except sw_api.SwApiError:
    pass
check("a plain 500 is not retried", calls["n"] == 1)

calls, A, C = _fake_clients([503, 200])
httpx.AsyncClient = A  # type: ignore[misc]
check("memory _post retries 503 then succeeds", run(memory_plugin._post({"command": "memory:x"}))[".status"] == "ok" and calls["n"] == 2)

calls, A, C = _fake_clients([502, 502, 200])
httpx.Client = C  # type: ignore[misc]
check("microagent sync client retries 502 then succeeds", mc._post_json_sync({"command": "x"})[".status"] == "ok" and calls["n"] == 3)

httpx.AsyncClient, httpx.Client = _real_async, _real_sync


# --- the fixed "unavailable" text --------------------------------------------------------------------------------
check("Russian fixed text exists and is Russian", "недоступен" in (sme.localized_unavailable_answer("Russian") or ""))
check("language lookup ignores case and spaces", sme.localized_unavailable_answer(" russian ") == sme.localized_unavailable_answer("Russian"))
check("an unknown language has no fixed text", sme.localized_unavailable_answer("Klingon") is None and sme.localized_unavailable_answer(None) is None)


# --- an sw turn sets the narrator's suppress flag itself ---------------------------------------------------------
class _Turn(SmallModelTurnMixin):
    pass


def _start(is_real_user: bool, suppress: bool, stuck: bool) -> bool:
    t = _Turn()
    t.current_turn_suppress_narration = stuck
    t.turn_narration_remarks = ["old"]
    try:
        t._start_small_model_turn("hi", [], is_real_user, False, suppress)
    except AttributeError:
        pass  # the rest of the start-up needs a whole session; the flag is set before it
    return t.current_turn_suppress_narration


check("a real user's sw turn clears a stuck suppress flag", _start(True, False, True) is False)
check("a real user's turn is never suppressed", _start(True, True, False) is False)
check("a proactive sw turn keeps the suppress it was asked for", _start(False, True, False) is True)
check("a proactive sw turn can opt back in", _start(False, False, True) is False)

if _FAILURES:
    print(f"\n{len(_FAILURES)} failed: {_FAILURES}")
    sys.exit(1)
print("\nall passed")
