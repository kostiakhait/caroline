"""The library advisor (2026-11-25, explicit instruction: Caroline in the Ferret constructor took asphalt and a sky
from the web instead of the asset library; "a microagent that additionally searches the library and prompts
Caroline, as the tools prompt her").

For a real user message, the sw.library.need microagent lists the things the work needs as English queries; the CODE
runs them against the library's REST search and glues a block of candidate cards (id@version, type, what it is) to the
message, so every engine sees what the library already holds and does not have to remember to look. A hint, not a
restriction: she reads the cards and picks (or searches further). Only in the constructor deployment
(CAROLINE_FERRET_EDITOR=1). Fail-open: any failure is logged and the message goes on unchanged."""

from __future__ import annotations

import asyncio
import os
import time

from app.logging_setup import log_event

TOTAL_TIMEOUT_S = 25.0
SEARCH_TIMEOUT_S = 10.0
PER_QUERY = 4
MAX_BLOCK_CHARS = 4500
DESCRIPTION_CHARS = 170
BLOCK_HEADER = "[Library look-up done for this message"


def enabled() -> bool:
    return os.environ.get("CAROLINE_FERRET_EDITOR") == "1"


def library_url() -> str:
    return (os.environ.get("FERRET_LIBRARY_URL") or "https://builder.ferret3d.com/ferret/library/").rstrip("/") + "/"


async def _decide(message: str, dialogue: list[str], tab_id: str | None) -> list[str]:
    from app.microagents.sw_library_need import LIBRARY_NEED
    from app.plugins.voice_api import _run_local_microagent

    try:
        result = await _run_local_microagent(LIBRARY_NEED, "app.microagents.sw_library_need", {"message": message, "dialogue": dialogue})
    except Exception as exc:  # noqa: BLE001 -- fail-open
        log_event("engine", "library_advisor_failed", tab_id=tab_id, error=str(exc)[:200], error_type=type(exc).__name__)
        return []
    output = (result or {}).get("output") if (result or {}).get("status") == "ok" else None
    if not isinstance(output, dict) or not output.get("needs_library"):
        return []
    return [q.strip() for q in output.get("queries") or [] if isinstance(q, str) and q.strip()]


async def _search(query: str) -> list[dict] | None:
    import httpx

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S) as client:
            reply = await client.get(library_url() + "api/search", params={"q": query, "limit": PER_QUERY})
            reply.raise_for_status()
            cards = reply.json()
    except Exception as exc:  # noqa: BLE001
        log_event("engine", "library_advisor_search_failed", error=str(exc)[:200], error_type=type(exc).__name__, query=query[:80])
        return None
    log_event("engine", "library_advisor_search_timing", ms=round((time.monotonic() - started) * 1000), query=query[:80],
              found=len(cards) if isinstance(cards, list) else -1)
    if not isinstance(cards, list):
        return None
    return [c for c in cards if isinstance(c, dict) and c.get("id")][:PER_QUERY]


def _card_line(card: dict) -> str:
    versions = card.get("versions") or []
    ref = f'{card["id"]}@{versions[-1]}' if versions else str(card["id"])
    description = " ".join(str(card.get("description") or "").split())
    if len(description) > DESCRIPTION_CHARS:
        description = description[:DESCRIPTION_CHARS].rstrip() + "..."
    return f'  - {ref} [{card.get("type") or "?"}, {card.get("rubric") or "-"}]: {description}'


def format_block(found: list[tuple[str, list[dict] | None]]) -> str:
    """The text block for the searched queries; "" when no search could be made at all."""
    if all(cards is None for _q, cards in found):
        return ""
    lines = [BLOCK_HEADER + ": the asset library was searched for the things this work needs. Take what fits (refer to it "
             "as id@version, see the ferret-scene-from-library skill) before going to any website; where nothing fits, "
             "search the library again with other words, and make or find the thing only then.]"]
    seen: set[str] = set()
    for query, cards in found:
        if cards is None:
            continue
        lines.append(f'"{query}":')
        fresh = [c for c in cards if c["id"] not in seen]
        seen.update(c["id"] for c in fresh)
        if fresh:
            lines.extend(_card_line(c) for c in fresh)
        else:
            lines.append("  - nothing (or only what is listed above)")
    return "\n".join(lines)[:MAX_BLOCK_CHARS]


async def _block(message: str, dialogue: list[str], tab_id: str | None) -> str:
    queries = await _decide(message, dialogue, tab_id)
    if not queries:
        return ""
    results = await asyncio.gather(*(_search(q) for q in queries))
    return format_block(list(zip(queries, results)))


async def library_block(message: str, dialogue: list[str] | None = None, tab_id: str | None = None) -> str:
    """The text block to glue to a real user message, or "" when nothing is needed, found or available."""
    from app.login_api import is_logged_in

    message = (message or "").strip()
    if not message or not enabled() or not is_logged_in():
        return ""
    try:
        return await asyncio.wait_for(_block(message, dialogue or [], tab_id), TOTAL_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 -- fail-open
        log_event("engine", "library_advisor_failed", tab_id=tab_id, error=str(exc)[:200], error_type=type(exc).__name__)
        return ""
