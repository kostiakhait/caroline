"""What the akinakes chat coordinator (chatSecretary) does with a human chat message besides answering it: see who is in the
room, hand the message to agents, publish a bus task, wait for the agents' replies. The model decides which, and
when; these tools only execute. Only in a role started with AKINAKES_CHAT_ORCHESTRATOR=1. See app/akinakes_channel.py."""

from __future__ import annotations

import json
from typing import Any

from app import akinakes_channel
from app.plugins.loader import Plugin, PluginTool


def _failure(exc: Exception) -> dict[str, Any]:
    return {"is_error": True, "content": [{"type": "text", "text": str(exc)}]}


def _json(data: Any) -> dict[str, Any]:
    return {"text": json.dumps(data, ensure_ascii=False, indent=2, default=str)}


async def room_agents(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        return _json(await akinakes_channel.room_agents(akinakes_channel.session_chain_id(str(args.get("chain_id") or ""))))
    except Exception as exc:  # noqa: BLE001 -- the model gets the reason and decides
        return _failure(exc)


async def hand_to_agents(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        uids = [str(u) for u in args.get("to_agents") or []]
        sent = await akinakes_channel.hand_to_agents(akinakes_channel.session_chain_id(str(args.get("chain_id") or "")), uids, args.get("text") or None)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return {"text": f"Handed to: {', '.join(sent)}. They answer in the room themselves, or pass."}


async def bus_agents(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        return _json(await akinakes_channel.bus_agents(akinakes_channel.session_chain_id(str(args.get("chain_id") or ""))))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


async def publish_bus_task(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        fields = args.get("fields") or {}
        if not isinstance(fields, dict):
            raise ValueError("fields must be an object.")
        ack = await akinakes_channel.publish_bus_task(akinakes_channel.session_chain_id(str(args.get("chain_id") or "")), str(args.get("to_agent") or ""),
                                                      str(args.get("task_type") or ""), fields)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return _json({"published": True, "ack": ack, "note": "The result comes back to you as a new message; tell the room then."})


async def wait_for_agent_replies(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        result = await akinakes_channel.wait_for_agent_replies(akinakes_channel.session_chain_id(str(args.get("chain_id") or "")), float(args.get("seconds") or 30))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return _json(result)


_CHAIN = {"chain_id": {"type": "string", "description": "Optional: the chat message's chainId (taken from the conversation when left out)."}}

_config = akinakes_channel.load_config()
PLUGIN = None if _config is None or not _config.orchestrator or not _config.chat_url else Plugin(
    name="akinakes_secretary",
    description="Hand a chat message to agents in its room, publish a bus task, wait for replies",
    always_loaded=True,
    tools=[
        PluginTool(name="room_agents", description="The room of the chat message with this chainId (name, type, id, parent) and the agents "
                                                   "in it, with their role, what they do and the bus tasks they take.",
                   input_schema={"type": "object", "properties": _CHAIN, "required": []}, handler=room_agents),
        PluginTool(name="hand_to_agents",
                   description="Forward the chat message to the named agents (their uids from room_agents). Each answers in the room itself or passes. "
                               "Optional text replaces the message text for them (for example when you have made the request clearer).",
                   input_schema={"type": "object", "properties": {**_CHAIN, "to_agents": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                                                                  "text": {"type": "string"}}, "required": ["to_agents"]},
                   handler=hand_to_agents),
        PluginTool(name="bus_agents",
                   description="Every agent that takes tasks from the Agent Bus, in this room or not: what it does and each task it takes, "
                               "with the task's description and payload fields.",
                   input_schema={"type": "object", "properties": _CHAIN, "required": []}, handler=bus_agents),
        PluginTool(name="publish_bus_task",
                   description="Publish one of the tasks bus_agents lists; its result returns to you later. With to_agent it goes to that "
                               "agent; without it, to every agent on the bus, and the one whose work it is takes it (a request for a "
                               "project or a bureau goes this way). fields are that task's payload fields, in your own words where a "
                               "field is free text.",
                   input_schema={"type": "object", "properties": {**_CHAIN, "to_agent": {"type": "string", "description": "Optional: the agent that takes the task, its uid from bus_agents. It is who receives the task, not an identity this call acts as. Leave it out to send the task to everyone."},
                                                                  "task_type": {"type": "string", "description": "One of the task types bus_agents lists."},
                                                                  "fields": {"type": "object"}},
                                  "required": ["task_type"]},
                   handler=publish_bus_task),
        PluginTool(name="wait_for_agent_replies",
                   description="Wait (up to 90 s) until every agent you handed the message to has posted in the room or passed, then return their replies, "
                               "who passed and who stayed silent. Use it when you need to know whether anyone answered.",
                   input_schema={"type": "object", "properties": {**_CHAIN, "seconds": {"type": "number"}}, "required": []},
                   handler=wait_for_agent_replies),
    ],
)
