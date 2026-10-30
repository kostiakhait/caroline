"""The way an akinakes agent answers: publishes the reply of the task in progress to the Agent Bus
(the analogue of ratatosk_send_message). Routing comes from the original message the channel kept for the chainId,
so the model supplies only the chainId and the result. See app/akinakes_channel.py."""

from __future__ import annotations

from typing import Any

from app import akinakes_channel
from app.plugins.loader import Plugin, PluginTool


async def akinakes_publish_result(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        ack = await akinakes_channel.publish_result(str(args.get("chain_id") or ""), str(args.get("payload_type") or ""), dict(args.get("fields") or {}))
    except ValueError as exc:
        return {"is_error": True, "content": [{"type": "text", "text": str(exc)}]}
    return {"text": f"Published (messageId={ack.get('messageId')})."}


async def akinakes_report_blocked(args: dict[str, Any], _report_progress: Any) -> dict[str, Any]:
    try:
        ack = await akinakes_channel.publish_blocked(str(args.get("chain_id") or ""), str(args.get("reason") or "unspecified"))
    except ValueError as exc:
        return {"is_error": True, "content": [{"type": "text", "text": str(exc)}]}
    return {"text": f"Reported as blocked (messageId={ack.get('messageId')})."}


# Only in a process that is an akinakes agent (BUS_URL and AGENT_UID set): a desktop or IntelliCat Caroline never sees these tools.
PLUGIN = None if akinakes_channel.load_config() is None else Plugin(
    name="akinakes_bus",
    description="Deliver the result of an Agent Bus task",
    always_loaded=True,
    tools=[
        PluginTool(
            name="akinakes_publish_result",
            description="Publishes the result of the Agent Bus task with this chainId. Call it once, when the work is done.",
            input_schema={
                "type": "object",
                "properties": {
                    "chain_id": {"type": "string", "description": "The chainId from the task message, exactly."},
                    "payload_type": {"type": "string", "description": "The reply type named in the task message."},
                    "fields": {"type": "object", "description": "The result fields named in the task message."},
                },
                "required": ["chain_id", "payload_type", "fields"],
            },
            handler=akinakes_publish_result,
        ),
        PluginTool(
            name="akinakes_report_blocked",
            description="Reports that the Agent Bus task with this chainId cannot be done, with the reason.",
            input_schema={
                "type": "object",
                "properties": {"chain_id": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["chain_id", "reason"],
            },
            handler=akinakes_report_blocked,
        ),
    ],
)
