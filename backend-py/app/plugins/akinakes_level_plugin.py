"""What an akinakes level secretary (project, bureau) does with a request of its level besides recording it: see the
level's room and its members, post in that room, give agent members their part of the work as bus tasks, collect
what comes back. The model decides which, and when; these tools only execute. Only in a role started with
AKINAKES_LEVEL_SECRETARY. The records themselves are project_state_plugin's tools. See app/akinakes_channel.py."""

from __future__ import annotations

import json
from typing import Any

from app import akinakes_channel
from app.plugins.loader import Plugin, PluginTool


def _failure(exc: Exception) -> dict[str, Any]:
    return {"is_error": True, "content": [{"type": "text", "text": str(exc)}]}


def _chain(args: dict[str, Any]) -> str:
    return akinakes_channel.session_chain_id(str(args.get("chain_id") or ""))


async def level_room(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        return {"text": json.dumps(await akinakes_channel.level_room(_chain(args)), ensure_ascii=False, indent=2, default=str)}
    except Exception as exc:  # noqa: BLE001 -- the model gets the reason and decides
        return _failure(exc)


async def level_post(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        await akinakes_channel.level_post(_chain(args), str(args.get("text") or ""))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return {"text": "Posted in the room."}


async def level_publish_subtask(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        fields = args.get("fields") or {}
        if not isinstance(fields, dict):
            raise ValueError("fields must be an object.")
        await akinakes_channel.level_publish_subtask(_chain(args), str(args.get("to_agent") or ""),
                                                     str(args.get("task_type") or ""), fields)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return {"text": "Published. The agent's result comes back to you; wait_for_level_results collects it."}


async def wait_for_level_results(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        result = await akinakes_channel.wait_for_level_results(_chain(args), float(args.get("seconds") or 300))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return {"text": json.dumps(result, ensure_ascii=False, indent=2, default=str)}


_CHAIN = {"chain_id": {"type": "string", "description": "Optional: the request's chainId (taken from the conversation when left out)."}}

_config = akinakes_channel.load_config()
PLUGIN = None if _config is None or not _config.level or not _config.chat_url else Plugin(
    name="akinakes_level",
    description="The room and members of your project or bureau: post there, give members their part, collect results",
    always_loaded=True,
    tools=[
        PluginTool(name="level_room",
                   description="The room of the project or bureau this request belongs to, its members now (people; agents with their "
                               "role, what they do and the bus tasks they take), and the room where the request was asked.",
                   input_schema={"type": "object", "properties": _CHAIN, "required": []}, handler=level_room),
        PluginTool(name="level_post",
                   description="Post a message in the room of the project or bureau, as its secretary: the request, or the part of it "
                               "for the people there.",
                   input_schema={"type": "object", "properties": {**_CHAIN, "text": {"type": "string"}}, "required": ["text"]},
                   handler=level_post),
        PluginTool(name="level_publish_subtask",
                   description="Give an agent member of the room its part of the work: one of the bus tasks it takes (level_room lists "
                               "them), with that task's fields in your own words. Its result comes back to you.",
                   input_schema={"type": "object", "properties": {**_CHAIN,
                                                                  "to_agent": {"type": "string", "description": "The agent member who does this part: its uid from level_room."},
                                                                  "task_type": {"type": "string", "description": "One of the task types that agent takes."},
                                                                  "fields": {"type": "object"}},
                                  "required": ["to_agent", "task_type"]},
                   handler=level_publish_subtask),
        PluginTool(name="wait_for_level_results",
                   description="Wait (up to 600 s) until every agent you gave a part to has returned its result; returns the results, "
                               "who is still working, and what was said in the room since you posted there.",
                   input_schema={"type": "object", "properties": {**_CHAIN, "seconds": {"type": "number"}}, "required": []},
                   handler=wait_for_level_results),
    ],
)
