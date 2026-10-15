"""Shared "find by name, fuzzy" helper for every messenger plugin's chat/
channel/group list.

Per explicit instruction (2026-10-15): confirmed live -- every messenger
plugin's own *_list_chats/*_list_channels/*_list_groups/*_list_conversations
tool just hands the model a raw, un-matched list to eyeball itself (none of
them do any matching in code), and the ONE place that DID match in code
(app/plugins/companion_contacts_store.py's own search_contacts()) did
literal, hardcoded substring matching -- "Mikhail Murashov" never found a
real contact named "Mikhail Murashev" (one letter off). "Делай гибкий поиск
по мессенджерам" (для всех мессенджеров, одним и тем же путём) -- this
module is that one shared path: every messenger's own *_find_* tool is a
thin wrapper that fetches its chats/channels/groups the same way its
existing *_list_* tool already does, then calls find_by_name here, which
hands the matching decision to the search.fuzzy_match microagent
(voice_api.fuzzy_match) -- never a hardcoded string/substring comparison.
"""

from __future__ import annotations

from typing import Any

from app.plugins.voice_api import fuzzy_match


async def find_by_name(query: str, rows: list[dict[str, Any]], name_field: str = "name") -> list[dict[str, Any]]:
    """Returns the rows whose `name_field` (or `id`, if that's missing) the
    fuzzy_match microagent judges the query to mean -- in best-match-first
    order, possibly empty (genuinely nothing fit, not an error). Fail-open
    on any error/timeout (fuzzy_match returns None): returns every row
    unfiltered, same as today's un-matched *_list_* tools already do --
    an infrastructure hiccup must not silently report "nothing found" for
    something that might have been right there in the list."""
    if not rows:
        return []
    candidates = [
        {"label": str(i), "text": str(row.get(name_field) or row.get("id") or i)}
        for i, row in enumerate(rows)
    ]
    matched_labels = await fuzzy_match(query, candidates)
    if matched_labels is None:
        return rows
    return [rows[int(label)] for label in matched_labels if label.isdigit() and int(label) < len(rows)]
