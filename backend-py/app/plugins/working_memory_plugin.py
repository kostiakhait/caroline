"""DEPRECATED and read-only for the model since the microagent memory
(save_info / request_info) -- see app/deprecated_memory.py. What follows
describes the store as it was built; its reading tool still works.

working_memory -- the tool surface over app/working_memory.py's deliberate,
source-agnostic short-term fact cache. See that module's own doc comment for
the full design: six fixed categories, LFU+LRU-hybrid eviction, auto-injected
into every system prompt (chat_session.py/small_model_engine.py, right next to
owner_profile's own clause).

There is no automatic/transparent caching here by design -- Caroline decides
herself what's worth keeping handy and calls remember_fact. This is NOT the
same thing as notes_plugin.py's own read-through cache (that one is
transparent, Notes-specific, and exists purely to avoid redundant network
round-trips); this one is a deliberate, durable-feeling-but-actually-
ephemeral scratch space that works whether or not a SquirrelWisdom account
even exists.
"""

from __future__ import annotations

import json
from typing import Any

from app.deprecated_memory import READ_PREFIX, USAGE_NOTE, WRITE_PREFIX, read_only_refusal
from app.plugins.loader import Plugin, PluginTool
from app.working_memory import list_facts
from app.workspace_dir import WORKSPACE_DIR


def _fact_dict(fact: Any) -> dict[str, Any]:
    return {"key": fact.key, "value": fact.value, "useCount": fact.use_count, "lastUsedAt": fact.last_used_at}


async def working_memory_remember(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return read_only_refusal("working_memory_remember")


async def working_memory_touch(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return read_only_refusal("working_memory_touch")


async def working_memory_forget(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    return read_only_refusal("working_memory_forget")


async def working_memory_list(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    facts = list_facts(WORKSPACE_DIR, args.get("category"))
    return {"text": json.dumps([_fact_dict(f) for f in facts], ensure_ascii=False)}


PLUGIN = Plugin(
    name="working_memory",
    usage_instructions=USAGE_NOTE,
    tools=[
        PluginTool(
            "working_memory_remember",
            WRITE_PREFIX + "Saves (or updates, if the key already exists in that category) a small fact worth keeping handy "
            "for a while -- auto-injected into your own system prompt until it's evicted or explicitly "
            "forgotten. See this tool's own usage instructions for the fixed category list and eviction rules.",
            {"category": str, "key": str, "value": str}, working_memory_remember,
        ),
        PluginTool(
            "working_memory_touch",
            WRITE_PREFIX + "Marks an existing working-memory fact as genuinely reused again, WITHOUT restating its value -- "
            "call this (not working_memory_remember) when you actually use something already stored here.",
            {"category": str, "key": str}, working_memory_touch,
        ),
        PluginTool(
            "working_memory_forget",
            WRITE_PREFIX + "Explicitly removes a working-memory fact (e.g. a credential that no longer works).",
            {"category": str, "key": str}, working_memory_forget,
        ),
        PluginTool(
            "working_memory_list",
            READ_PREFIX + "Lists everything currently in working memory, optionally filtered to one category -- the same "
            "content that's already auto-injected into your system prompt, useful to check without relying on "
            "that block alone.",
            {"category": str | None}, working_memory_list,
        ),
    ],
)
