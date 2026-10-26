"""The memory advisor (2026-10-26, explicit instruction after a reinstalled Caroline did not know her owner and
did not look in her memory: "there must be an advisor microagent that demands the search in memory when the
request implies it" -- one general mechanism, no special cases).

For every real user message, before an engine sees it, the sw.memory.need microagent decides whether the
answer may depend on something Caroline already knows. If so, the CODE runs the memory search (request_info's
own query path) and the result goes to the engine as a block glued to the message -- so the weakest model of
the SW cascade, Claude and Codex all get the same help, and none of them has to remember to look.

A hint, not a restriction (like the tool advisor): the block says what was searched and found. Fail-open:
any failure is logged and the message goes on unchanged -- except that an advisor that cannot decide counts
as "memory is needed" (a needless look-up costs little, a missed one makes her answer as if she did not know
the user)."""

from __future__ import annotations

import asyncio
import time

from app.logging_setup import log_event

# The whole advisor must never hold a message for long.
TOTAL_TIMEOUT_S = 25.0
SEARCH_TIMEOUT_S = 20.0
MAX_BLOCK_CHARS = 6000

BLOCK_HEADER = "[Memory look-up done for this message"


async def _decide(message: str, dialogue: list[str], tab_id: str | None) -> tuple[bool, list[str], str]:
    """(memory needed, queries, why). The advisor failing means: needed, the message itself as the query."""
    from app.microagents.sw_memory_need import MEMORY_NEED
    from app.plugins.voice_api import _run_local_microagent

    try:
        result = await _run_local_microagent(MEMORY_NEED, "app.microagents.sw_memory_need", {"message": message, "dialogue": dialogue})
    except Exception as exc:  # noqa: BLE001 -- fail-open
        log_event("engine", "memory_advisor_failed", tab_id=tab_id, error=str(exc)[:200], error_type=type(exc).__name__)
        result = None
    output = (result or {}).get("output") if (result or {}).get("status") == "ok" else None
    if not isinstance(output, dict) or not isinstance(output.get("needs_memory"), bool):
        log_event("engine", "memory_advisor_undecided", tab_id=tab_id, error=(result or {}).get("error"))
        return True, [message[:200]], "the advisor could not decide"
    queries = [q.strip() for q in output.get("queries") or [] if isinstance(q, str) and q.strip()]
    if output["needs_memory"] and not queries:
        queries = [message[:200]]
    return output["needs_memory"], queries, str(output.get("why") or "")


async def _search(query: str) -> str | None:
    from app.plugins.memory_plugin import _run_memory_query

    try:
        reply = await asyncio.wait_for(_run_memory_query(query), SEARCH_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 -- timeouts included
        log_event("engine", "memory_advisor_search_failed", error=str(exc)[:200], error_type=type(exc).__name__)
        return None
    if reply.get("is_error"):
        log_event("engine", "memory_advisor_search_refused", error=str(reply.get("text"))[:200])
        return None
    return str(reply.get("text") or "")


async def memory_block(message: str, dialogue: list[str] | None = None, tab_id: str | None = None) -> str:
    """The text block to glue to a real user message, or "" when memory is not needed or unavailable."""
    from app.login_api import is_logged_in

    message = (message or "").strip()
    if not message or not is_logged_in():
        return ""
    started = time.monotonic()
    try:
        return await asyncio.wait_for(_block(message, dialogue or [], tab_id), TOTAL_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 -- fail-open, message goes on unchanged
        log_event("engine", "memory_advisor_failed", tab_id=tab_id, error=str(exc)[:200], error_type=type(exc).__name__,
                  ms=int((time.monotonic() - started) * 1000))
        return ""


async def _block(message: str, dialogue: list[str], tab_id: str | None) -> str:
    started = time.monotonic()
    needed, queries, why = await _decide(message, dialogue, tab_id)
    if not needed:
        log_event("engine", "memory_advisor_not_needed", tab_id=tab_id, why=why, ms=int((time.monotonic() - started) * 1000))
        return ""
    replies = await asyncio.gather(*(_search(q) for q in queries))
    found = [(q, r) for q, r in zip(queries, replies) if r]
    log_event("engine", "memory_advisor_searched", tab_id=tab_id, why=why, queries=queries,
              answered=len(found), ms=int((time.monotonic() - started) * 1000))
    if not found:
        return ""
    parts = [f"{BLOCK_HEADER}: your memory was searched because {why or 'the message may rely on what you already know'}. "
             "This is a hint, not a restriction -- call request_info yourself for anything more.]"]
    budget = MAX_BLOCK_CHARS
    for query, reply in found:
        text = reply[:budget]
        budget -= len(text)
        parts.append(f'[Memory, query "{query}": {text}]')
        if budget <= 0:
            break
    return "\n".join(parts)
