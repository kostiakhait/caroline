"""The OpenAI engine: one `codex app-server` process per tab, adapted to the
AgentEngine contract.

Codex events (thread/turn/item notifications) are translated into the same
claude_agent_sdk message objects the Claude engine yields, so ChatSession,
wire.py, failure classification, status and history stay engine-agnostic.
Caroline's own tools (the in-process MCP servers built by plugins.loader) are
handed to Codex as `dynamicTools`; when the model calls one, Codex sends an
`item/tool/call` request that is answered by calling that tool over an
in-memory MCP client session -- the same server objects the Claude SDK talks
to, so a tool behaves identically under either engine.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, AsyncIterable, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage, ResultMessage, SystemMessage, TextBlock, ThinkingBlock,
    ToolResultBlock, ToolUseBlock, UserMessage,
)

from app.engines.codex_rpc import CodexRpcClient, CodexRpcError, build_env, codex_argv
from app.logging_setup import log_event

_END = object()
INIT_TIMEOUT_S = 60.0
COMPACTION_TIMEOUT_S = 300.0


@dataclass
class CodexOptions:
    codex_exe: str
    codex_home: str
    cwd: str
    # Caroline's own system prompt. Sent as developer instructions (appended),
    # NOT baseInstructions, which would replace Codex's built-in agent prompt.
    base_instructions: str | None = None
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    resume_thread_id: str | None = None
    # Raw `-c key=value` overrides (tests point Codex at a fake provider).
    config_overrides: list[str] = field(default_factory=list)
    extra_env: dict[str, str] = field(default_factory=dict)
    # Where this engine's conversation log is written (see CodexEngine._record).
    transcript_dir: str | None = None


class CodexEngineError(Exception):
    pass


def _in_process_client(server_instance: Any) -> Any:
    """An async context manager yielding an MCP client connected in-process to
    a server object. mcp 2.x ships `mcp.Client`; mcp 1.x has the memory-session
    helper instead -- the installed version decides."""
    try:
        from mcp import Client
        return Client(server_instance)
    except ImportError:
        from mcp.shared.memory import create_connected_server_and_client_session
        return create_connected_server_and_client_session(server_instance)


def _field(obj: Any, snake: str, camel: str, default: Any = None) -> Any:
    """mcp 2.x exposes snake_case fields, mcp 1.x camelCase."""
    value = getattr(obj, snake, None)
    return value if value is not None else getattr(obj, camel, default)


class ToolBridge:
    """Exposes the SDK-style in-process MCP servers as Codex dynamic tools.

    Each client session is an anyio-based async context manager, which
    requires its __aenter__/__aexit__ to run in the SAME asyncio task --
    CodexEngine.disconnect() normally runs in a different task than connect()
    did (ChatSession fire-and-forgets it via asyncio.create_task), so both are
    funneled through one dedicated task here instead of an AsyncExitStack that
    would violate that requirement."""

    def __init__(self, mcp_servers: dict[str, Any]) -> None:
        self._servers = mcp_servers
        self._sessions: dict[str, Any] = {}
        self._route: dict[str, tuple[str, str]] = {}  # dynamic tool name -> (server, tool)
        self.specs: list[dict[str, Any]] = []
        self._close_requested = asyncio.Event()
        self._closed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def open(self) -> None:
        ready = asyncio.Event()
        error: list[BaseException] = []
        self._task = asyncio.ensure_future(self._run(ready, error))
        await ready.wait()
        if error:
            raise error[0]

    async def _run(self, ready: asyncio.Event, error: list[BaseException]) -> None:
        try:
            async with AsyncExitStack() as stack:
                for server_name, cfg in self._servers.items():
                    instance = cfg.get("instance") if isinstance(cfg, dict) else None
                    if instance is None:
                        continue  # an external (stdio/http) server: not bridged
                    session = await stack.enter_async_context(_in_process_client(instance))
                    self._sessions[server_name] = session
                    for tool in (await session.list_tools()).tools:
                        name = f"{server_name}__{tool.name}"
                        self._route[name] = (server_name, tool.name)
                        self.specs.append({
                            "name": name,
                            "description": tool.description or "",
                            "inputSchema": _field(tool, "input_schema", "inputSchema") or {"type": "object", "properties": {}},
                        })
                ready.set()
                await self._close_requested.wait()
        except BaseException as exc:  # noqa: BLE001 -- surfaced to open()/logged, never swallowed
            error.append(exc)
            ready.set()
        finally:
            self._closed.set()

    def display_name(self, dynamic_name: str) -> str:
        server, tool = self._route.get(dynamic_name, ("", dynamic_name))
        return f"mcp__{server}__{tool}" if server else dynamic_name

    async def call(self, dynamic_name: str, arguments: Any) -> dict[str, Any]:
        route = self._route.get(dynamic_name)
        if route is None:
            return {"success": False, "contentItems": [{"type": "inputText", "text": f"Unknown tool: {dynamic_name}"}]}
        server_name, tool_name = route
        result = await self._sessions[server_name].call_tool(tool_name, arguments if isinstance(arguments, dict) else {})
        items: list[dict[str, Any]] = []
        for block in result.content:
            kind = getattr(block, "type", None)
            if kind == "text":
                items.append({"type": "inputText", "text": block.text})
            elif kind == "image":
                items.append({"type": "inputImage", "imageUrl": f"data:{_field(block, 'mime_type', 'mimeType')};base64,{block.data}"})
        if not items:
            items.append({"type": "inputText", "text": ""})
        return {"success": not _field(result, "is_error", "isError", False), "contentItems": items}

    async def close(self) -> None:
        if self._task is None:
            return
        self._close_requested.set()
        try:
            await asyncio.wait_for(self._closed.wait(), timeout=15)
        except asyncio.TimeoutError:
            log_event("engine", "codex_tool_bridge_close_timeout")


def _block_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content, "is_error": block.is_error}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "thinking": block.thinking}
    return {"type": "unknown"}


def _error_name(error: Any) -> str:
    info = (error or {}).get("codexErrorInfo") if isinstance(error, dict) else None
    if isinstance(info, str):
        return info
    if isinstance(info, dict) and info:
        return next(iter(info))
    return ""


def input_to_turn_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    """A Claude stream-json user message -> Codex turn/start input items."""
    content = (message.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    items: list[dict[str, Any]] = []
    for block in content or []:
        kind = block.get("type")
        if kind == "text":
            items.append({"type": "text", "text": block.get("text", "")})
        elif kind == "image":
            src = block.get("source") or {}
            if src.get("type") == "base64":
                items.append({"type": "image", "url": f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"})
            elif src.get("type") == "url":
                items.append({"type": "image", "url": src.get("url", "")})
        # document/other blocks: Codex cannot read them inline; every attachment
        # is also saved to disk and named in the text block (see _push_message).
    return items


class CodexEngine:
    kind = "openai"

    def __init__(self, options: CodexOptions) -> None:
        self._o = options
        self._out: asyncio.Queue[Any] = asyncio.Queue()
        self._rpc: CodexRpcClient | None = None
        self._bridge = ToolBridge(options.mcp_servers)
        self._pump: asyncio.Task[None] | None = None
        self._turn_idle = asyncio.Event()
        self._turn_idle.set()
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._turn_started_at = 0.0
        self._last_agent_text = ""
        self._last_usage: dict[str, int] = {}
        self._model = options.model or ""
        self._disconnecting = False
        self._tool_names: dict[str, str] = {}  # item id -> tool_use name shown to the UI
        self._last_turn_items: list[dict[str, Any]] = []
        self._context_retry_used = False
        self._compaction_turn_done: asyncio.Event | None = None  # set while WE await a compaction turn

    # ------------------------------------------------------------ lifecycle --

    async def connect(self, input_stream: AsyncIterable[dict[str, Any]]) -> None:
        await self._bridge.open()
        self._rpc = CodexRpcClient(
            codex_argv(self._o.codex_exe, self._o.config_overrides), build_env(self._o.codex_home, self._o.extra_env),
            on_notification=self._on_notification, on_server_request=self._on_server_request,
            on_closed=self._on_closed, label="codex",
        )
        self._rpc.start()
        await self._rpc.request("initialize", {
            "clientInfo": {"name": "caroline", "title": "Caroline", "version": "1"},
            "capabilities": {"experimentalApi": True},
        }, timeout=INIT_TIMEOUT_S)
        await self._rpc.notify("initialized")
        await self._open_thread()
        self._emit(SystemMessage(subtype="init", data={
            "session_id": self._thread_id, "model": self._model, "mcp_servers": [],
            "tools": [s["name"] for s in self._bridge.specs],
        }))
        self._pump = asyncio.create_task(self._pump_input(input_stream))

    async def _open_thread(self) -> None:
        params: dict[str, Any] = {
            "cwd": self._o.cwd, "approvalPolicy": "never", "sandbox": "danger-full-access",
            "dynamicTools": self._bridge.specs,
        }
        if self._o.base_instructions:
            params["developerInstructions"] = self._o.base_instructions
        if self._o.model:
            params["model"] = self._o.model
        assert self._rpc
        result = None
        if self._o.resume_thread_id:
            try:
                result = await self._rpc.request("thread/resume", {**params, "threadId": self._o.resume_thread_id}, timeout=INIT_TIMEOUT_S)
            except CodexRpcError as exc:
                log_event("engine", "codex_thread_resume_failed", thread_id=self._o.resume_thread_id, error=str(exc))
        if result is None:
            result = await self._rpc.request("thread/start", params, timeout=INIT_TIMEOUT_S)
        thread = result["thread"]
        self._thread_id = thread["id"]
        self._model = result.get("model") or thread.get("model") or self._model

    async def _pump_input(self, input_stream: AsyncIterable[dict[str, Any]]) -> None:
        try:
            async for message in input_stream:
                await self._turn_idle.wait()
                items = input_to_turn_items(message)
                if not items:
                    continue
                self._turn_idle.clear()
                self._last_agent_text = ""
                self._last_turn_items = items
                self._record("user", (message.get("message") or {}).get("content"))
                self._context_retry_used = False
                await self._start_turn(items)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event("engine", "codex_input_pump_failed", error=repr(exc))

    async def _start_turn(self, items: list[dict[str, Any]]) -> None:
        try:
            assert self._rpc
            await self._rpc.request("turn/start", {"threadId": self._thread_id, "input": items}, timeout=INIT_TIMEOUT_S)
        except Exception as exc:
            self._turn_idle.set()
            self._emit_failed_result(f"turn/start failed: {exc}", "")

    async def _compact_and_retry(self) -> None:
        """The conversation outgrew the model's context: have Codex compact it,
        then run the same input again -- once per user message, invisibly."""
        try:
            assert self._rpc
            self._compaction_turn_done = asyncio.Event()
            await self._rpc.request("thread/compact/start", {"threadId": self._thread_id}, timeout=INIT_TIMEOUT_S)
            await asyncio.wait_for(self._compaction_turn_done.wait(), COMPACTION_TIMEOUT_S)
        except Exception as exc:
            log_event("engine", "codex_context_compaction_failed", error=repr(exc))
            self._emit_failed_result("The conversation is too long for the model and could not be compacted.", "ContextWindowExceeded", final=True)
            return
        await self._start_turn(self._last_turn_items)

    async def receive_messages(self) -> AsyncIterator[Any]:
        while True:
            item = await self._out.get()
            if item is _END:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def interrupt(self) -> None:
        if self._rpc and self._thread_id and self._turn_id and not self._turn_idle.is_set():
            try:
                await self._rpc.request("turn/interrupt", {"threadId": self._thread_id, "turnId": self._turn_id}, timeout=15)
            except Exception as exc:
                log_event("engine", "codex_interrupt_failed", error=repr(exc))

    async def disconnect(self) -> None:
        self._disconnecting = True
        if self._pump:
            self._pump.cancel()
        if self._rpc:
            await self._rpc.kill_and_wait()
        await self._bridge.close()
        self._emit(_END)

    async def reconnect_mcp_server(self, name: str) -> None:
        raise CodexEngineError("Codex tools are hosted in-process; there is no server to reconnect")

    def process_pid(self) -> int | None:
        return self._rpc.pid if self._rpc else None

    def _emit(self, message: Any) -> None:
        """Queues a message for the consumer; assistant/user messages are also
        appended to this thread's transcript file (see _record)."""
        if isinstance(message, (AssistantMessage, UserMessage)):
            self._record("assistant" if isinstance(message, AssistantMessage) else "user", message.content)
        self._out.put_nowait(message)

    def _record(self, kind: str, content: Any) -> None:
        """Appends one entry in Claude Code's transcript shape to
        <transcript_dir>/<threadId>.jsonl, so history readers, the 24h dialogue
        and the phone mirror work on an OpenAI tab exactly as on a Claude one."""
        if not self._o.transcript_dir or not self._thread_id:
            return
        try:
            blocks = [b if isinstance(b, (dict, str)) else _block_dict(b) for b in (content if isinstance(content, list) else [content])]
            entry = {
                "type": kind, "uuid": str(uuid.uuid4()), "sessionId": self._thread_id,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "message": {"role": kind, "content": blocks},
            }
            os.makedirs(self._o.transcript_dir, exist_ok=True)
            with open(os.path.join(self._o.transcript_dir, f"{self._thread_id}.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            log_event("engine", "codex_transcript_write_failed", error=repr(exc))

    def _on_closed(self) -> None:
        if self._disconnecting:
            return
        tail = " | ".join(self._rpc.stderr_tail[-5:]) if self._rpc else ""
        self._emit(CodexEngineError(f"codex process ended unexpectedly: {tail}"))

    # ---------------------------------------------------------- server calls --

    async def _on_server_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "item/tool/call":
            return await self._bridge.call(params.get("tool", ""), params.get("arguments"))
        if method.endswith("/requestApproval"):
            return {"decision": "accept"}
        raise CodexRpcError(-32601, f"unsupported server request: {method}")

    # ------------------------------------------------------------ adapter ----

    def _assistant(self, blocks: list[Any]) -> AssistantMessage:
        return AssistantMessage(content=blocks, model=self._model or "openai", session_id=self._thread_id)

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "turn/started":
            turn = params.get("turn") or {}
            self._turn_id = turn.get("id")
            self._turn_started_at = time.monotonic()
        elif method == "item/started":
            self._on_item_started(params.get("item") or {})
        elif method == "item/completed":
            self._on_item_completed(params.get("item") or {})
        elif method == "thread/tokenUsage/updated":
            last = ((params.get("tokenUsage") or {}).get("last")) or {}
            cached = int(last.get("cachedInputTokens") or 0)
            self._last_usage = {
                "input_tokens": max(0, int(last.get("inputTokens") or 0) - cached),
                "cache_read_input_tokens": cached,
                "cache_creation_input_tokens": int(last.get("cacheWriteInputTokens") or 0),
                "output_tokens": int(last.get("outputTokens") or 0),
            }
        elif method == "turn/completed":
            self._on_turn_completed(params.get("turn") or {})

    def _tool_use_for(self, item: dict[str, Any]) -> ToolUseBlock | None:
        kind, item_id = item.get("type"), item.get("id", "")
        if kind == "dynamicToolCall":
            name, tool_input = self._bridge.display_name(item.get("tool", "")), item.get("arguments") or {}
        elif kind == "mcpToolCall":
            name, tool_input = f"mcp__{item.get('server', '')}__{item.get('tool', '')}", item.get("arguments") or {}
        elif kind == "commandExecution":
            name, tool_input = "Bash", {"command": item.get("command", "")}
        elif kind == "fileChange":
            name, tool_input = "Edit", {"changes": item.get("changes") or []}
        elif kind == "webSearch":
            name, tool_input = "WebSearch", {"query": item.get("query", "")}
        else:
            return None
        self._tool_names[item_id] = name
        return ToolUseBlock(id=item_id, name=name, input=tool_input if isinstance(tool_input, dict) else {"value": tool_input})

    def _on_item_started(self, item: dict[str, Any]) -> None:
        if item.get("type") == "contextCompaction":
            return
        block = self._tool_use_for(item)
        if block is not None:
            self._emit(self._assistant([block]))

    def _on_item_completed(self, item: dict[str, Any]) -> None:
        kind, item_id = item.get("type"), item.get("id", "")
        if kind == "contextCompaction":
            return
        if kind == "agentMessage":
            text = item.get("text") or ""
            if text:
                self._last_agent_text = text
                self._emit(self._assistant([TextBlock(text=text)]))
        elif kind == "reasoning":
            parts = item.get("summary") or item.get("content") or []
            text = "\n".join(p if isinstance(p, str) else str(p.get("text", "")) for p in parts).strip()
            if text:
                self._emit(self._assistant([ThinkingBlock(thinking=text, signature="")]))
        elif item_id in self._tool_names:
            ok, content = self._tool_outcome(item)
            self._emit(UserMessage(content=[ToolResultBlock(tool_use_id=item_id, content=content, is_error=not ok)]))

    @staticmethod
    def _tool_outcome(item: dict[str, Any]) -> tuple[bool, str]:
        kind = item.get("type")
        if kind == "dynamicToolCall":
            texts = [c.get("text", "") for c in (item.get("contentItems") or []) if c.get("type") == "inputText"]
            return bool(item.get("success", True)), "\n".join(texts)
        if kind == "commandExecution":
            code = item.get("exitCode")
            return code in (0, None), str(item.get("aggregatedOutput") or "")
        if kind == "mcpToolCall":
            err = item.get("error")
            return not err, str(err or item.get("result") or "")
        return item.get("status") != "failed", ""

    def _on_turn_completed(self, turn: dict[str, Any]) -> None:
        status, error = turn.get("status"), turn.get("error")
        if (status == "failed" or error) and _error_name(error).lower() == "contextwindowexceeded" and not self._context_retry_used:
            self._context_retry_used = True
            asyncio.ensure_future(self._compact_and_retry())
            return  # the turn is not over from the caller's side: it is being retried
        if self._compaction_turn_done is not None:
            done, self._compaction_turn_done = self._compaction_turn_done, None
            done.set()
            return  # the compaction's own turn, not a user turn
        self._turn_idle.set()
        if status == "failed" or error:
            message = (error or {}).get("message") if isinstance(error, dict) else str(error or "turn failed")
            self._emit_failed_result(message or "turn failed", _error_name(error))
            return
        self._emit(ResultMessage(
            subtype="success" if status == "completed" else "error_during_execution",
            duration_ms=int((time.monotonic() - self._turn_started_at) * 1000), duration_api_ms=0,
            is_error=False, num_turns=1, session_id=self._thread_id or "",
            usage=dict(self._last_usage) or None, result=self._last_agent_text or None,
            stop_reason="interrupted" if status == "interrupted" else "end_turn",
        ))

    def _emit_failed_result(self, message: str, error_name: str, final: bool = False) -> None:
        self._turn_idle.set()
        # The failure is also shown as an assistant line: the chat and ChatSession's
        # own classifiers (usage limit -> "limited") read assistant text, and a
        # bare error result would otherwise be invisible to the user.
        self._emit(AssistantMessage(
            content=[TextBlock(text=message)], model=self._model or "openai", session_id=self._thread_id,
            error="rate_limit" if error_name.lower() == "usagelimitexceeded" else None,
        ))
        self._emit(ResultMessage(
            subtype="error_during_execution",
            duration_ms=int((time.monotonic() - self._turn_started_at) * 1000), duration_api_ms=0,
            is_error=True, num_turns=1, session_id=self._thread_id or "",
            usage=dict(self._last_usage) or None, result=message, errors=[message],
            terminal_reason=error_name or None,
        ))


def user_text_message(text: str) -> dict[str, Any]:
    """Helper for tests: a Claude-shaped user message carrying plain text."""
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


__all__ = ["CodexEngine", "CodexOptions", "CodexEngineError", "ToolBridge", "input_to_turn_items", "user_text_message"]
