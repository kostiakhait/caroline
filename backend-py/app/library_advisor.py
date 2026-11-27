"""The library advisor (2026-11-25, explicit instruction: Caroline in the Ferret constructor took asphalt and a sky
from the web instead of the asset library; "a microagent that additionally searches the library and prompts
Caroline, as the tools prompt her").

For a real user message, the sw.library.need microagent lists the things the work will be made of (what the message names
first, then what such a scene has around it) as English queries; the CODE runs them against the library's ranked search and
glues a block of cards (id@version, type, what it is) to the message, so every engine sees what the library already holds.
A hint, not a restriction: she reads the cards and picks. Only in the constructor deployment (CAROLINE_FERRET_EDITOR=1).

No limits of its own (2026-11-26, the owner: no quantitative limits in the advisor or any tool): every thing the microagent
names is searched, cards are shown whole, and when a search matches more than the library's first page the block says how
many match in all and how to page on (library_search with offset). Fail-open: any failure is logged and the message goes on."""

from __future__ import annotations

import asyncio
import os
import time

from app.logging_setup import log_event

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
    out: list[str] = []
    for q in output.get("queries") or []:
        if isinstance(q, str) and q.strip() and q.strip() not in out:
            out.append(q.strip())
    return out


async def _search(query: str) -> tuple[list[dict], int] | None:
    """The library's first page for a query, and how many match in all; None when the library could not be asked."""
    import httpx

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            reply = await client.get(library_url() + "api/search", params={"q": query})
            reply.raise_for_status()
            cards = reply.json()
            total = int(reply.headers.get("x-total-count") or (len(cards) if isinstance(cards, list) else 0))
    except Exception as exc:  # noqa: BLE001
        log_event("engine", "library_advisor_search_failed", error=str(exc)[:200], error_type=type(exc).__name__, query=query[:80])
        return None
    if not isinstance(cards, list):
        return None
    cards = [c for c in cards if isinstance(c, dict) and c.get("id")]
    log_event("engine", "library_advisor_search_timing", ms=round((time.monotonic() - started) * 1000), query=query[:80], found=total)
    return cards, max(total, len(cards))


def _card_line(card: dict) -> str:
    versions = card.get("versions") or []
    ref = f'{card["id"]}@{versions[-1]}' if versions else str(card["id"])
    description = " ".join(str(card.get("description") or card.get("name") or "").split())
    return f'  - {ref} [{card.get("type") or "?"}, {card.get("rubric") or "-"}]: {description}'


def format_block(found: list[tuple[str, tuple[list[dict], int] | None]]) -> str:
    """The text block for the searched queries; "" when no search could be made at all."""
    if all(res is None for _q, res in found):
        return ""
    lines = [BLOCK_HEADER + ": these ready-made items of the asset library may be useful for this work: what it names, what could "
             "stand in for it or be built from, its materials, and what a scene like this usually has around it. The cards are ranked, "
             "the best match first, and the library may hold a thing under another name than the one you would search for (a "
             "mountain as cliffs, rocks and scree). Take from the library what fits (refer to it as id@version, see the "
             "ferret-scene-from-library skill), and of the items that fit take the most detailed one (a real model over a simple one, "
             "a full PBR set over a plain colour). Where more match than are shown, read on with library_search {q, offset}. Go to a "
             "website or build a thing yourself only for what these and other searches with other words do not give.]"]
    seen: set[str] = set()
    for query, res in found:
        if res is None:
            continue
        cards, total = res
        more = f" ({total} match in all; the next ones: library_search {{q: \"{query}\", offset: {len(cards)}}})" if total > len(cards) else ""
        lines.append(f'"{query}":{more}')
        fresh = [c for c in cards if c["id"] not in seen]
        seen.update(c["id"] for c in fresh)
        if fresh:
            lines.extend(_card_line(c) for c in fresh)
        elif cards:
            lines.append("  - the same items as listed above")
        else:
            lines.append("  - nothing in the library")
    return "\n".join(lines)


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
        return await _block(message, dialogue or [], tab_id)
    except Exception as exc:  # noqa: BLE001 -- fail-open
        log_event("engine", "library_advisor_failed", tab_id=tab_id, error=str(exc)[:200], error_type=type(exc).__name__)
        return ""
