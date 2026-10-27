"""instruction_memory -- the tool surface over app/instruction_memory.py: the owner's standing
instructions, always in the system prompt, with priority + LFU eviction (see that module)."""

from __future__ import annotations

import json
from typing import Any

from app.instruction_memory import (
    DEFAULT_PRIORITY, MAX_PRIORITY, add_instruction, forget_instruction, load_instructions, mark_applied,
)
from app.plugins.loader import Plugin, PluginTool
from app.workspace_dir import WORKSPACE_DIR


def _dict(item: Any) -> dict[str, Any]:
    return {"id": item.id, "text": item.text, "priority": item.priority, "uses": item.use_count}


async def instruction_add(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    try:
        item, evicted, repeated = add_instruction(WORKSPACE_DIR, args.get("text") or "", args.get("priority"))
    except ValueError as exc:
        return {"text": str(exc)}
    result: dict[str, Any] = {"stored": _dict(item), "alreadyKnown": repeated}
    dropped_self = any(e.id == item.id for e in evicted)
    others = [e for e in evicted if e.id != item.id]
    if dropped_self:
        result["warning"] = (
            "The list is full of higher-priority instructions and this one ranked lowest, so it was NOT kept. "
            "Tell the owner, or raise its priority if it really outranks the others."
        )
    if others:
        result["pushedOut"] = [_dict(e) for e in others]
        result["note"] = "These lower-ranked instructions were pushed out to make room; tell the owner if one of them mattered."
    return {"text": json.dumps(result, ensure_ascii=False)}


async def instruction_list(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    items = load_instructions(WORKSPACE_DIR)
    if not items:
        return {"text": "No standing instructions stored."}
    return {"text": json.dumps([_dict(i) for i in sorted(items, key=lambda i: -i.priority)], ensure_ascii=False)}


async def instruction_forget(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    removed = forget_instruction(WORKSPACE_DIR, str(args.get("id") or ""))
    return {"text": "Removed." if removed else f"No instruction with id {args.get('id')} -- nothing to remove."}


async def instruction_applied(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    item = mark_applied(WORKSPACE_DIR, str(args.get("id") or ""))
    return {"text": json.dumps(_dict(item), ensure_ascii=False) if item else f"No instruction with id {args.get('id')}."}


_USAGE_INSTRUCTIONS = (
    "Your owner's standing instructions are kept with instruction_add and are shown to you in every system "
    "prompt. When the owner gives a rule that is meant to last -- \"всегда ...\", \"никогда ...\", \"впредь ...\", "
    "\"не делай ...\", \"always\", \"never\", \"from now on\", or a correction of how you behave that will "
    "apply again -- call instruction_add right away, in the owner's own words, short and complete enough to "
    "stand alone. Do NOT store one-off requests about the current task. Priority: "
    f"{MAX_PRIORITY} = a hard rule the owner stressed or repeated, 4 = important, {DEFAULT_PRIORITY} = a normal "
    "rule (default), 2-1 = a mild preference. When the owner repeats an instruction, call instruction_add with "
    "the same text again: that raises how much it is worth keeping. The list is small on purpose: when it is "
    "full the lowest-priority, least-used entries are dropped, and instruction_add tells you if that happened -- "
    "pass that on to the owner. Call instruction_applied(id) when an instruction actually changed what you "
    "did in a turn (it counts toward keeping that instruction). instruction_forget(id) removes one the "
    "owner withdraws."
)


PLUGIN = Plugin(
    name="instruction_memory",
    usage_instructions=_USAGE_INSTRUCTIONS,
    tools=[
        PluginTool(
            "instruction_add",
            "Keeps a standing instruction from your owner (a rule that applies to every future turn). "
            "\"text\" is the rule; \"priority\" 1-5 (5 = hard rule, default 3). Stating the same text again "
            "raises its use count and priority instead of duplicating it. May push out the lowest-ranked "
            "instruction when the list is full.",
            {"text": str, "priority": int | None}, instruction_add,
        ),
        PluginTool("instruction_list", "Lists the stored standing instructions with ids, priorities and use counts.", {}, instruction_list),
        PluginTool("instruction_forget", "Removes a standing instruction by id (the owner withdrew it).", {"id": str}, instruction_forget),
        PluginTool(
            "instruction_applied",
            "Records that an instruction really shaped what you did this turn, so it is not the one pushed out "
            "next time.",
            {"id": str}, instruction_applied,
        ),
    ],
)
