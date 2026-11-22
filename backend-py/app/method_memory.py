"""Method memory (2026-11-02, explicit instruction): after a real turn with tool work, the sw.method.extract
microagent decides whether the way the task was done is worth remembering, and the CODE saves it -- the way
save_info saves -- so that "earlier I solved this kind of task like this" does not depend on the model
remembering to write it down. The other half is the memory advisor (app/memory_advisor.py): on every new
request it also searches for how such a task was done before.

Called from the end-of-turn hook (chat_session_mixins/delivery_report.py, _dr_finish), the one place both the
Claude/Codex engines and the sw path reach with the turn's request, tool trace and delivered text. Fail-open:
nothing here can fail a turn."""

from __future__ import annotations

import time

from app.logging_setup import log_event

# Fewer real tool calls than this is a one-step job: no method to remember, and no model call is spent on it.
MIN_WORK_CALLS = 2

# Remembering and recalling are not the work itself.
_MEMORY_TOOL_MARKERS = ("save_info", "request_info", "save_document", "owner_profile_", "correct_info", "forget_info")


def work_lines(trace: list[str]) -> list[str]:
    """The trace lines that are the task's own work (not memory look-ups or saves)."""
    return [line for line in trace if not any(marker in line.split("(", 1)[0] for marker in _MEMORY_TOOL_MARKERS)]


async def remember_method(request: str, trace: list[str], delivered: str, tab_id: str | None) -> bool:
    """True when a method was saved. Never raises."""
    try:
        work = work_lines(trace)
        if len((request or "").strip()) == 0 or len(work) < MIN_WORK_CALLS:
            return False
        from app.microagents.sw_method_extract import METHOD_EXTRACT
        from app.plugins.memory_plugin import save_fact_from_code
        from app.plugins.voice_api import _run_local_microagent

        started = time.monotonic()
        result = await _run_local_microagent(
            METHOD_EXTRACT, "app.microagents.sw_method_extract", {"request": request, "trace": work, "delivered": delivered},
        )
        output = (result or {}).get("output") if (result or {}).get("status") == "ok" else None
        if not isinstance(output, dict) or output.get("worth_saving") is not True:
            log_event("engine", "method_memory_skipped", tab_id=tab_id, ok=isinstance(output, dict), calls=len(work),
                      ms=round((time.monotonic() - started) * 1000))
            return False
        method = str(output.get("method") or "").strip()
        saved = await save_fact_from_code(method, tab_id)
        log_event("engine", "method_memory_saved", tab_id=tab_id, saved=saved, method=method[:300],
                  ms=round((time.monotonic() - started) * 1000))
        return saved
    except Exception as exc:  # noqa: BLE001 -- background bookkeeping must never surface as a turn error
        log_event("engine", "method_memory_failed", tab_id=tab_id, error=str(exc)[:200], error_type=type(exc).__name__)
        return False
