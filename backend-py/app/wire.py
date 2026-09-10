"""Converts claude_agent_sdk's typed Python dataclasses back into the exact
wire JSON shape chat.js already knows how to render (msg.type ===
"assistant" -> msg.message.content[] blocks, etc.) -- the Python SDK
flattens fields onto its own dataclasses rather than preserving the raw
Claude Code CLI JSON shape, so this is the one place that gap gets bridged.
Covers what chat.js actually inspects (system/init, assistant text/
tool_use/thinking, result) -- not necessarily every field of every message
type, extend as real usage surfaces gaps.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if isinstance(block, ToolResultBlock):
        return {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content, "is_error": block.is_error}
    # Unknown block shape -- pass through whatever it already looks like
    # rather than dropping it silently.
    return {"type": getattr(block, "type", "unknown"), **vars(block)}


def message_to_wire(msg: Any) -> dict[str, Any] | None:
    """Returns the {type, ...} dict to send as `sdk_message`'s `message`
    field, or None for a message kind chat.js has no handling for (dropped
    silently -- StreamEvent/RateLimitEvent/ConversationResetMessage today)."""
    if isinstance(msg, SystemMessage):
        return {"type": "system", "subtype": msg.subtype, **msg.data}
    if isinstance(msg, AssistantMessage):
        return {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [_block_to_dict(b) for b in msg.content],
                "model": msg.model,
                "stop_reason": msg.stop_reason,
            },
            "session_id": msg.session_id,
            "parent_tool_use_id": msg.parent_tool_use_id,
        }
    if isinstance(msg, UserMessage):
        return {
            "type": "user",
            "message": {"role": "user", "content": [_block_to_dict(b) if not isinstance(b, str) else b for b in (msg.content if isinstance(msg.content, list) else [msg.content])]},
            "parent_tool_use_id": msg.parent_tool_use_id,
        }
    if isinstance(msg, ResultMessage):
        return {
            "type": "result",
            "subtype": msg.subtype,
            "duration_ms": msg.duration_ms,
            "is_error": msg.is_error,
            "num_turns": msg.num_turns,
            "session_id": msg.session_id,
            "total_cost_usd": msg.total_cost_usd,
            "result": msg.result,
        }
    if isinstance(msg, StreamEvent):
        return None  # not consumed by chat.js today
    return None
