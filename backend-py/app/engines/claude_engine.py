"""The Claude engine: a thin delegating wrapper over ClaudeSDKClient, so the
rest of the backend talks to the AgentEngine contract rather than to the SDK
class directly. Behaviour is exactly the SDK client's."""

from __future__ import annotations

from typing import Any, AsyncIterable, AsyncIterator

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient


class ClaudeEngine:
    kind = "claude"

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self._client = ClaudeSDKClient(options=options)

    async def connect(self, input_stream: AsyncIterable[dict[str, Any]]) -> None:
        await self._client.connect(input_stream)

    def receive_messages(self) -> AsyncIterator[Any]:
        return self._client.receive_messages()

    async def interrupt(self) -> None:
        await self._client.interrupt()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def reconnect_mcp_server(self, name: str) -> None:
        await self._client.reconnect_mcp_server(name)

    def process_pid(self) -> int | None:
        transport = getattr(self._client, "_transport", None)
        process = getattr(transport, "_process", None)
        return getattr(process, "pid", None)
