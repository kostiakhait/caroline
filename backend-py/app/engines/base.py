"""The contract ChatSession needs from an agent engine.

An engine owns one live agent process for one tab. ChatSession feeds it user
messages through the async iterable given to connect(), and consumes the
engine's output from receive_messages() as claude_agent_sdk message objects
(SystemMessage/AssistantMessage/UserMessage/ResultMessage) -- an engine that
is not the Claude SDK adapts its own events to those shapes, so everything
downstream (wire.py, failure classification, status, history) is shared.
"""

from __future__ import annotations

from typing import Any, AsyncIterable, AsyncIterator, Literal, Protocol

EngineKind = Literal["claude", "openai"]


class AgentEngine(Protocol):
    kind: EngineKind

    async def connect(self, input_stream: AsyncIterable[dict[str, Any]]) -> None:
        """Starts the agent process and begins consuming input_stream."""

    def receive_messages(self) -> AsyncIterator[Any]:
        """Yields SDK-shaped messages until the process ends."""

    async def interrupt(self) -> None:
        """Politely stops the in-flight turn (the process stays alive)."""

    async def disconnect(self) -> None:
        """Ends the process."""

    async def reconnect_mcp_server(self, name: str) -> None:
        """Retries one failed tool server. Engines without such servers raise."""

    def process_pid(self) -> int | None:
        """The agent process's pid if it can be found without the engine's own
        bookkeeping (used as a last resort by force-kill), else None."""
