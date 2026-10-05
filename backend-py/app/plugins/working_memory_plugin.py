"""working_memory -- the tool surface over app/working_memory.py's deliberate,
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

from app.plugins.loader import Plugin, PluginTool
from app.working_memory import CATEGORIES, forget_fact, list_facts, remember_fact, touch_fact
from app.workspace_dir import WORKSPACE_DIR


def _fact_dict(fact: Any) -> dict[str, Any]:
    return {"key": fact.key, "value": fact.value, "useCount": fact.use_count, "lastUsedAt": fact.last_used_at}


async def working_memory_remember(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    fact = remember_fact(WORKSPACE_DIR, args["category"], args["key"], args["value"])
    return {"text": json.dumps(_fact_dict(fact), ensure_ascii=False)}


async def working_memory_touch(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    fact = touch_fact(WORKSPACE_DIR, args["category"], args["key"])
    if fact is None:
        return {"text": f'No "{args["key"]}" in category "{args["category"]}" to touch -- it may have already been evicted or never existed.'}
    return {"text": json.dumps(_fact_dict(fact), ensure_ascii=False)}


async def working_memory_forget(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    removed = forget_fact(WORKSPACE_DIR, args["category"], args["key"])
    return {"text": "Removed." if removed else f'No "{args["key"]}" in category "{args["category"]}" -- nothing to remove.'}


async def working_memory_list(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    facts = list_facts(WORKSPACE_DIR, args.get("category"))
    return {"text": json.dumps([_fact_dict(f) for f in facts], ensure_ascii=False)}


_USAGE_INSTRUCTIONS = (
    "Six fixed categories, pass exactly one: " + ", ".join(CATEGORIES) + " (\"facts\" is the catch-all for "
    "anything that doesn't fit the other five). This is a SMALL, auto-evicting scratch space, not durable "
    "storage -- durable facts still belong in Notes (\"Caroline:Profile\"/\"Caroline:Topics\"/\"Caroline:Vault\" "
    "as already established), this is specifically for things worth having instantly on hand for a while "
    "without a tool call or a Notes round-trip. Eviction is real and silent: each category holds at most 8 "
    "entries, and the whole injected block is capped in total size -- the least-used, least-recently-used "
    "entries disappear automatically to make room. When you actually reuse something already stored here "
    "(not just see it sitting in the prompt), call working_memory_touch on it rather than re-calling "
    "working_memory_remember with the same value -- this is what keeps genuinely useful entries from being "
    "evicted in favor of ones that were only ever written once and never touched again. Remove a fact "
    "explicitly with working_memory_forget once it's known to be stale (e.g. a credential that no longer "
    "works) rather than leaving it to silently expire."
)


PLUGIN = Plugin(
    name="working_memory",
    usage_instructions=_USAGE_INSTRUCTIONS,
    tools=[
        PluginTool(
            "working_memory_remember",
            "Saves (or updates, if the key already exists in that category) a small fact worth keeping handy "
            "for a while -- auto-injected into your own system prompt until it's evicted or explicitly "
            "forgotten. See this tool's own usage instructions for the fixed category list and eviction rules.",
            {"category": str, "key": str, "value": str}, working_memory_remember,
        ),
        PluginTool(
            "working_memory_touch",
            "Marks an existing working-memory fact as genuinely reused again, WITHOUT restating its value -- "
            "call this (not working_memory_remember) when you actually use something already stored here.",
            {"category": str, "key": str}, working_memory_touch,
        ),
        PluginTool(
            "working_memory_forget",
            "Explicitly removes a working-memory fact (e.g. a credential that no longer works).",
            {"category": str, "key": str}, working_memory_forget,
        ),
        PluginTool(
            "working_memory_list",
            "Lists everything currently in working memory, optionally filtered to one category -- the same "
            "content that's already auto-injected into your system prompt, useful to check without relying on "
            "that block alone.",
            {"category": str | None}, working_memory_list,
        ),
    ],
)
