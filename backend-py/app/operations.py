"""Uniform start/status/(partial)/stop contract for EVERY plugin tool call --
explicit architectural requirement (2026-09-09): no operation ever blocks
the caller until completion. Generalizes patterns already proven piecemeal
in the current system (caroline-email's send/delete/etc already return
immediately and report outcome via a proactive nudge; viewer.ts's
open_in_viewer does the same; reforce/Camerlengo's ProcessorDispatch.py
implements the identical job:registerProcessor/pollForWork/submitResult
shape for heavy GPU work) into one uniform mechanism every plugin tool gets
automatically, not something each plugin author reimplements.

Confirmed design (2026-09-09): ONE contract for every tool, not two tiers --
a tool expected to finish fast doesn't skip the protocol, it just satisfies
it near-instantly. See dispatch()'s FAST_PATH_TIMEOUT_S: every call is
raced against a short window; if the handler finishes within it, the
"start" response already carries status="done" and the real result (no
polling needed for the common case); if not, "start" returns immediately
with status="running" and an operation_id, and the SAME two generic tools
below (check_operation_status/stop_operation -- registered once at the
engine level, not per-plugin) let the caller poll for status/partial data
or cancel it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from claude_agent_sdk import McpServerConfig, SdkMcpTool, create_sdk_mcp_server, tool as sdk_tool

from app.logging_setup import log_event

# How long dispatch() waits before giving up on returning a "done" result
# synchronously and instead returning a bare operation_id for polling.
# Short enough that a fast tool (get_current_time, a quick HTTP call) never
# pays any polling ceremony; long enough not to be a meaningless race for
# anything that's actually fast. Confirmed live (2026-09-09): a native-exe
# subprocess call (mouse.exe --action Position) measured 78-95ms end to end
# on its own, but 200ms wasn't a reliable margin above that once Python-side
# dispatch/logging overhead and asyncio's own per-process subprocess-spawn
# setup cost are added in -- widened to keep native-exe tools (the majority
# of Phase 2's ported plugins) landing on the fast path consistently rather
# than flapping between "done" and "running" run to run.
FAST_PATH_TIMEOUT_S = 0.5

ReportProgress = Callable[[Any], None]
ToolHandler = Callable[[dict[str, Any], ReportProgress], Awaitable[dict[str, Any]]]


@dataclass
class Operation:
    id: str
    tool_name: str
    status: str = "running"  # "running" | "done" | "error" | "cancelled"
    partial: Any | None = None
    result: Any | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    task: "asyncio.Task[Any] | None" = None


class OperationRegistry:
    """One per backend process (not per tab/session) -- operation ids are
    globally unique and short-lived (cleared once observed as done/error/
    cancelled via a status check, so this never grows unbounded)."""

    def __init__(self) -> None:
        self._ops: dict[str, Operation] = {}

    def create(self, tool_name: str) -> Operation:
        op = Operation(id=uuid.uuid4().hex[:12], tool_name=tool_name)
        self._ops[op.id] = op
        return op

    def get(self, operation_id: str) -> Operation | None:
        return self._ops.get(operation_id)

    def forget(self, operation_id: str) -> None:
        self._ops.pop(operation_id, None)


REGISTRY = OperationRegistry()


async def dispatch(plugin_name: str, tool_name: str, handler: ToolHandler, args: dict[str, Any]) -> dict[str, Any]:
    """Runs one tool call through the uniform start/status/stop contract.
    Always logs call/result/error/duration at the engine level (see
    plugins/loader.py's wrap_tool, which calls this) -- individual plugins
    never implement their own polling/cancellation plumbing."""
    op = REGISTRY.create(tool_name)

    def report_progress(data: Any) -> None:
        op.partial = data

    async def run() -> Any:
        try:
            result = await handler(args, report_progress)
            op.status = "done"
            op.result = result
            return result
        except asyncio.CancelledError:
            op.status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 -- must surface, not swallow
            op.status = "error"
            op.error = str(exc)
            raise

    op.task = asyncio.create_task(run())
    log_event(f"plugin:{plugin_name}", "operation_started", tool=tool_name, operation_id=op.id)

    try:
        result = await asyncio.wait_for(asyncio.shield(op.task), timeout=FAST_PATH_TIMEOUT_S)
        duration_ms = round((time.monotonic() - op.started_at) * 1000, 1)
        log_event(f"plugin:{plugin_name}", "operation_done_fast", tool=tool_name, operation_id=op.id, duration_ms=duration_ms)
        REGISTRY.forget(op.id)
        return {"operation_id": op.id, "status": "done", "result": result}
    except asyncio.TimeoutError:
        log_event(f"plugin:{plugin_name}", "operation_running", tool=tool_name, operation_id=op.id)
        return {"operation_id": op.id, "status": "running"}
    except Exception as exc:  # noqa: BLE001 -- op.status/op.error already set by run()
        duration_ms = round((time.monotonic() - op.started_at) * 1000, 1)
        log_event(f"plugin:{plugin_name}", "operation_error", tool=tool_name, operation_id=op.id, duration_ms=duration_ms, error=str(exc))
        REGISTRY.forget(op.id)
        return {"operation_id": op.id, "status": "error", "error": str(exc)}


def _operation_to_dict(op: Operation) -> dict[str, Any]:
    body: dict[str, Any] = {"operation_id": op.id, "status": op.status}
    if op.partial is not None:
        body["partial"] = op.partial
    if op.status == "done":
        body["result"] = op.result
    if op.status == "error":
        body["error"] = op.error
    return body


@sdk_tool(
    "check_operation_status",
    "Checks the status of a previously started long-running tool operation (one whose "
    "start returned status=\"running\" instead of \"done\"). Returns the current status "
    "(running/done/error/cancelled), any partial/intermediate data reported so far, and "
    "the final result once done.",
    {"operation_id": str},
)
async def check_operation_status(args: dict[str, Any]) -> dict[str, Any]:
    op = REGISTRY.get(args["operation_id"])
    if op is None:
        log_event("engine", "check_operation_status_unknown", operation_id=args["operation_id"])
        return {"content": [{"type": "text", "text": "Unknown or already-completed operation_id."}], "is_error": True}
    body = _operation_to_dict(op)
    if op.status in ("done", "error", "cancelled"):
        log_event("engine", "check_operation_status_final", operation_id=op.id, tool=op.tool_name, status=op.status)
        REGISTRY.forget(op.id)
    return {"content": [{"type": "text", "text": str(body)}]}


@sdk_tool(
    "stop_operation",
    "Cancels a previously started long-running tool operation by its operation_id.",
    {"operation_id": str},
)
async def stop_operation(args: dict[str, Any]) -> dict[str, Any]:
    op = REGISTRY.get(args["operation_id"])
    if op is None or op.task is None:
        log_event("engine", "stop_operation_unknown", operation_id=args["operation_id"])
        return {"content": [{"type": "text", "text": "Unknown or already-completed operation_id."}], "is_error": True}
    op.task.cancel()
    log_event("engine", "operation_cancelled", operation_id=op.id, tool=op.tool_name)
    return {"content": [{"type": "text", "text": f"Cancelled {args['operation_id']}."}]}


def _build_get_tool_instructions_tool(tool_instructions: dict[str, str]) -> SdkMcpTool[Any]:
    """Closes over this turn's tool_name -> detailed-usage-instructions
    map (built fresh per query() from every loaded plugin's own
    Plugin.usage_instructions -- see plugins/loader.py) so the model can
    pull a specific tool's full usage guidance ON DEMAND, as a real tool
    call, instead of it being auto-appended to every system prompt.
    Explicit architectural requirement (2026-09-09): the model always
    sees a general tool list with each tool's own short `description`
    (standard MCP field), but detailed "how to use this well" guidance is
    fetched only when the model actually asks for it -- keeps the system
    prompt itself small regardless of how many plugins are loaded."""

    @sdk_tool(
        "get_tool_instructions",
        "Fetches the detailed usage guidance for a specific tool by name (not every tool has any -- most "
        "are self-explanatory from their own short description alone). Call this when you're about to use "
        "a tool whose behavior/conventions/gotchas you're not fully sure of, or when a tool's own short "
        "description hints there's more nuance (e.g. \"see get_tool_instructions\"). Cheap to call, no side "
        "effects.",
        {"tool_name": str},
    )
    async def get_tool_instructions(args: dict[str, Any]) -> dict[str, Any]:
        tool_name = args["tool_name"]
        text = tool_instructions.get(tool_name)
        log_event("engine", "get_tool_instructions_called", tool_name=tool_name, found=text is not None)
        if text is None:
            return {"content": [{"type": "text", "text": f'No detailed usage instructions for "{tool_name}" -- its own short description is all there is.'}]}
        return {"content": [{"type": "text", "text": text}]}

    return get_tool_instructions


def build_operations_mcp_server(tool_instructions: dict[str, str] | None = None) -> McpServerConfig:
    """The generic operation-control tools (check_operation_status/
    stop_operation) plus get_tool_instructions, registered ONCE at the
    engine level (not per plugin) -- every plugin tool call goes through
    dispatch() above, which is what makes the first two apply
    universally; get_tool_instructions is built fresh per call here since
    it needs this turn's actual tool_instructions map."""
    tools = [check_operation_status, stop_operation, _build_get_tool_instructions_tool(tool_instructions or {})]
    return create_sdk_mcp_server(name="operations", tools=tools)
