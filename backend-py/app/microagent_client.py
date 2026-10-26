"""Local Microagent.Runtime for Caroline's own backend -- lets an agent's
build_prompt/verify CODE live and run entirely in this process instead of
needing to be deployed into Camerlengo's own server codebase (see
app/microagents/permission_identify.py's own module docstring for the
incident that prompted this, 2026-10-17).

Microagent.py is vendored from reforce (apps/microagents/Microagent.py, the
SAME file that already runs server-side -- see that module's own docstring:
"kept as an identical copy on the server (reforce) and on the client
(Caroline)") into the same shipped-or-dev-tree camerlengo/ directory
small_model_engine.py already resolves AI.py from (see that module's own
_resolve_camerlengo_dir). This module runs its own, independent resolution
rather than depending on small_model_engine.py's import having already run,
so it works standalone and never silently depends on another module's
import order.

The actual model call goes through Camerlengo's microagent:call command
(reforce's API/Api2AICommands.py, added 2026-10-17) -- the one piece that
must stay server-side: real provider keys and billing. Never a hard
dependency: if Microagent.py can't be resolved (a dev tree without the
right branch checked out, a build that somehow skipped vendoring it),
build_local_runtime()/new_budget() return None and every caller falls back
to the existing remote command, exactly like small_model_engine.py's own
camerlengo_ai=None fallback.

Microagent.Runtime.run() is SYNCHRONOUS (every agent runs in its own
thread internally, per Microagent.py's own module docstring) -- callers in
this async codebase must wrap it in asyncio.to_thread(...). llm_call below
is correspondingly a plain sync function doing a blocking HTTP call (it
already executes inside Runtime's own worker thread, never the asyncio
event loop), using httpx.Client rather than app/plugins/sw_api.py's
async-only _post_json.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from app.http_tls import ssl_context
from app.logging_setup import log_event
from app.sw_service_keys_local import CAROLINE_SW_KEY

API_URL = "https://www.squirrelwisdom.com/"

# Mirrors small_model_engine.py's own shipped-vs-dev-tree resolution exactly
# (see that module's _resolve_camerlengo_dir/_SHIPPED_CAMERLENGO_DIR/
# _DEV_TREE_CAMERLENGO_DIR) -- same vendored directory, just checked for
# Microagent.py instead of AI.py.
_SHIPPED_CAMERLENGO_DIR = Path(__file__).resolve().parent.parent / "camerlengo"
_DEV_TREE_CAMERLENGO_DIR = Path(__file__).resolve().parents[3] / "reforce"


def _resolve_camerlengo_dir() -> Path | None:
    override = os.environ.get("CAROLINE_CAMERLENGO_PATH")
    if override and (Path(override) / "Microagent.py").is_file():
        return Path(override)
    if (_SHIPPED_CAMERLENGO_DIR / "Microagent.py").is_file():
        return _SHIPPED_CAMERLENGO_DIR
    if (_DEV_TREE_CAMERLENGO_DIR / "Microagent.py").is_file():
        return _DEV_TREE_CAMERLENGO_DIR
    return None


Microagent: Any = None
_camerlengo_dir = _resolve_camerlengo_dir()
if _camerlengo_dir is None:
    log_event("engine", "microagent_client_camerlengo_not_found",
              shipped=str(_SHIPPED_CAMERLENGO_DIR), dev_tree=str(_DEV_TREE_CAMERLENGO_DIR))
else:
    if str(_camerlengo_dir) not in sys.path:
        sys.path.insert(0, str(_camerlengo_dir))
    try:
        import Microagent  # type: ignore[no-redef]  # noqa: E402
    except Exception as exc:  # noqa: BLE001 -- never a hard dependency, see module docstring
        log_event("engine", "microagent_client_import_failed", path=str(_camerlengo_dir), error=str(exc))
        Microagent = None

# Same plain numbers apps/microagents/settings.py already uses server-side
# (MicroagentServer.make_runtime/new_budget/ladder_from) -- not secrets, just
# the ladder/timeout/budget shape, kept identical so a locally-run agent
# behaves the same as it would have running on the server.
_CALL_TIMEOUT_S = 60.0
_AGENT_DEADLINE_S = 180.0
_MAX_ACTIVE_AGENTS = 8
_BUDGET_CALLS = 64
_BUDGET_DEADLINE_S = 300.0
_REASONING_RUNG1, _REASONING_RUNG2, _REASONING_RUNG3 = "low", "low", None


def _post_json_sync(body: dict[str, Any], timeout: float = 30.0, retries: int = 3) -> Any:
    """Synchronous mirror of app/plugins/sw_api.py's own _post_json -- see
    that function's own docstring for the retry/timeout reasoning this
    copies verbatim. A separate, sync copy because this runs inside
    Microagent.Runtime's own worker thread (see module docstring), never on
    the asyncio event loop _post_json itself assumes."""
    with httpx.Client(verify=ssl_context(), timeout=timeout) as client:
        last_err: Exception | None = None
        res = None
        for attempt in range(retries):
            try:
                res = client.post(API_URL, json=body)
                break
            except httpx.TransportError as exc:
                last_err = exc
                if attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
        else:
            raise last_err  # type: ignore[misc]
        if res.status_code >= 400:
            raise RuntimeError(f'SquirrelWisdom API HTTP {res.status_code} for command "{body.get("command")}"')
        return res.json()


# --- timing (2026-10-26: the memory advisor waited 13-25 s per message and nothing said where the time went) ----------
# Every agent runs in its own thread named "microagent:<agent>" (Microagent.py), and that is the thread _llm_call executes in.
_active_runs = 0
_inflight_calls = 0
_calls_by_agent: dict[str, int] = {}
_active_lock = threading.Lock()


def _note_llm_call(model_class: str, reasoning: str | None, prompt: str, started: float, ok: bool,
                   usage: Any = None, error: str | None = None, inflight: int | None = None) -> None:
    """One network hop of the ladder (a rung): how long the server took and what it spent. Never raises."""
    try:
        agent = threading.current_thread().name.removeprefix("microagent:")
        with _active_lock:
            _calls_by_agent[agent] = _calls_by_agent.get(agent, 0) + 1
            n = _calls_by_agent[agent]
        usage = usage if isinstance(usage, dict) else {}
        log_event("engine", "microagent_llm_call", agent=agent, model_class=model_class, reasoning=reasoning,
                  rung_call=n, inflight_calls=inflight, prompt_chars=len(prompt or ""), ms=round((time.monotonic() - started) * 1000), ok=ok, error=error,
                  prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                  reasoning_tokens=usage.get("reasoning_tokens") or (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
    except Exception:  # noqa: BLE001
        pass


def run_timed(runtime: Any, agent_name: str, agent_input: dict[str, Any], context: Any, submitted: float) -> Any:
    """runtime.run() for the worker thread, measured: how long the run waited for a thread (`queued_ms`: the default pool is
    shared with every other to_thread call), how many runs were going at the same moment, how long it ran, how many model
    calls it made (two runs of the same agent at once share the count). The result is returned unchanged."""
    global _active_runs
    started = time.monotonic()
    with _active_lock:
        _active_runs += 1
        concurrent = _active_runs
        _calls_by_agent[agent_name] = 0
    status = None
    try:
        result = runtime.run(agent_name, agent_input, context)
        status = result.get("status") if isinstance(result, dict) else None
        return result
    finally:
        with _active_lock:
            _active_runs -= 1
            calls = _calls_by_agent.get(agent_name, 0)
        try:
            log_event("engine", "microagent_run", agent=agent_name, queued_ms=round((started - submitted) * 1000),
                      run_ms=round((time.monotonic() - started) * 1000), model_calls=calls,
                      concurrent_runs=concurrent, threads=threading.active_count(), status=status)
        except Exception:  # noqa: BLE001
            pass


def _llm_call(model_class: str, prompt: str, timeout_s: float, reasoning: str | None = None,
              images: list | None = None) -> dict:
    """Microagent.Runtime's own llm_call contract -- POSTs Camerlengo's
    microagent:call command (reforce's API/Api2AICommands.py) and returns
    its {text, usage} verbatim. The real model call and billing stay
    server-side; this is the one network hop every rung of the ladder
    makes."""
    body = {
        "command": "microagent:call", "key": CAROLINE_SW_KEY,
        "modelClass": model_class, "prompt": prompt, "timeoutS": timeout_s,
    }
    if reasoning:
        body["reasoning"] = reasoning
    if images:
        body["images"] = images
    global _inflight_calls
    started = time.monotonic()
    with _active_lock:
        _inflight_calls += 1
        inflight = _inflight_calls
    try:
        envelope = _post_json_sync(body, timeout=timeout_s + 15.0, retries=1)
    except Exception as exc:  # noqa: BLE001 -- measured, then raised as before
        _note_llm_call(model_class, reasoning, prompt, started, ok=False, error=f"{type(exc).__name__}: {str(exc)[:120]}", inflight=inflight)
        raise
    finally:
        with _active_lock:
            _inflight_calls -= 1
    _note_llm_call(model_class, reasoning, prompt, started, ok=envelope.get(".status") == "ok", usage=envelope.get("usage"),
                   error=None if envelope.get(".status") == "ok" else envelope.get(".reason"), inflight=inflight)
    if envelope.get(".status") != "ok":
        raise RuntimeError(envelope.get(".reason") or "microagent:call failed")
    # cmdV2MicroagentCall's payload (text/usage) is merged at the TOP level
    # of the response by makeResponse(), not nested -- see that function's
    # own implementation (API/Api2Dispatcher.py).
    return {"text": envelope.get("text", ""), "usage": envelope.get("usage")}


def build_local_runtime(max_active_agents: int = _MAX_ACTIVE_AGENTS):
    """A Microagent.Runtime backed by Camerlengo's microagent:call
    primitive, or None if Microagent.py couldn't be resolved -- every
    caller must fall back to the existing remote command in that case (see
    module docstring)."""
    if Microagent is None:
        return None
    ladder = (
        Microagent.Rung(Microagent.MODEL_NANO, False, _REASONING_RUNG1),
        Microagent.Rung(Microagent.MODEL_NANO, True, _REASONING_RUNG2),
        Microagent.Rung(Microagent.MODEL_MINI, True, _REASONING_RUNG3),
    )
    return Microagent.Runtime(_llm_call, ladder=ladder, call_timeout_s=_CALL_TIMEOUT_S,
                               agent_deadline_s=_AGENT_DEADLINE_S, max_active_agents=max_active_agents)


def new_budget():
    if Microagent is None:
        return None
    return Microagent.Budget(calls=_BUDGET_CALLS, deadline_s=_BUDGET_DEADLINE_S)
