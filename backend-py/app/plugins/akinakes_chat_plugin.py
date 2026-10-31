"""How an akinakes role that talks in chat rooms (editor, research) answers a chat.message: in the message's room,
through Chat Service (as agent_lib's agents post), or by passing to chatSecretary when the message is not for it. The
room comes from the message the channel kept for the chainId, so the model supplies only the chainId and the text. See
app/akinakes_channel.py."""

from __future__ import annotations

import json
from typing import Any

from app import akinakes_channel
from app.plugins.loader import Plugin, PluginTool

HISTORY_TEXT_CHARS = 20000


def _failure(exc: Exception) -> dict[str, Any]:
    return {"is_error": True, "content": [{"type": "text", "text": str(exc)}]}


async def chat_post_message(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        await akinakes_channel.post_chat(str(args.get("chain_id") or ""), str(args.get("text") or ""))
    except Exception as exc:  # noqa: BLE001 -- the model gets the reason and decides
        return _failure(exc)
    return {"text": "Posted in the room."}


async def chat_history(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        messages = await akinakes_channel.chat_history(str(args.get("chain_id") or ""))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    lines = [f"[{m.get('created_at', '')}] {m.get('sender_uid', '?')}: {_text(m)}" for m in messages]
    text = "\n".join(lines) or "(no messages yet)"
    # The newest messages matter most: an overlong history keeps its end.
    return {"text": text if len(text) <= HISTORY_TEXT_CHARS else "... (older messages left out)\n" + text[-HISTORY_TEXT_CHARS:]}


def _text(message: dict[str, Any]) -> str:
    """A message's text; Chat Service may keep the payload as a JSON string (agent_lib/chat_history.py)."""
    payload = message.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return payload
    return str(payload.get("text", "")) if isinstance(payload, dict) else ""


async def akinakes_pass(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        await akinakes_channel.publish_pass(str(args.get("chain_id") or ""))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)
    return {"text": "Passed."}


_CHAIN = {"chain_id": {"type": "string", "description": "The chainId from the chat message, exactly."}}

# Only in an akinakes agent that has Chat Service (BUS_URL, AGENT_UID and CHAT_SERVICE_URL set).
_config = akinakes_channel.load_config()
PLUGIN = None if _config is None or not _config.chat_url else Plugin(
    name="akinakes_chat",
    description="Answer a chat message in its room, or pass it",
    always_loaded=True,
    tools=[
        PluginTool(name="chat_post_message", description="Posts your answer to the chat message with this chainId in its room.",
                   input_schema={"type": "object", "properties": {**_CHAIN, "text": {"type": "string"}}, "required": ["chain_id", "text"]},
                   handler=chat_post_message),
        PluginTool(name="chat_history", description="The conversation so far in the room of the chat message with this chainId.",
                   input_schema={"type": "object", "properties": _CHAIN, "required": ["chain_id"]}, handler=chat_history),
        PluginTool(name="akinakes_pass", description="The chat message with this chainId is not for you: pass it, and do nothing else.",
                   input_schema={"type": "object", "properties": _CHAIN, "required": ["chain_id"]}, handler=akinakes_pass),
    ],
)
