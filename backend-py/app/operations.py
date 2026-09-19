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

from app.durability import clear_pending_operation, save_pending_operation
from app.logging_setup import log_event
from app.session_context import get_inject_proactive, get_tab_id
from app.workspace_dir import WORKSPACE_DIR

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
    # Per explicit instruction (2026-09-10): which tab's turn started this
    # operation (session_context.py's get_tab_id(), sampled once at
    # creation time) -- lets ChatSession.stop() cancel exactly ITS OWN
    # tab's in-flight background operations without touching another
    # tab's still-running work, since this registry is process-wide, not
    # per-tab. None if dispatch() was somehow called outside a live
    # ChatSession turn (a throwaway test script) -- such an operation is
    # simply never matched by any tab's stop().
    tab_id: str | None = None
    # Per explicit instruction (2026-09-15): set True by dispatch() exactly
    # when an operation leaves the FAST_PATH_TIMEOUT_S window (the "running"
    # outcome, not "done") -- i.e. the caller already walked away without
    # the real result in hand. Confirmed live as a real, "regular" gap: a
    # background operation (an 8-mailbox cleanup the model explicitly
    # promised to report back on) had no way to surface its own completion
    # unless something happened to poll check_operation_status again later
    # -- nothing guaranteed that. run()'s own completion checks this flag
    # to fire a proactive nudge automatically; a fast-path operation never
    # needs one, since its result already reached the caller synchronously.
    notify_on_completion: bool = False


class OperationRegistry:
    """One per backend process (not per tab/session) -- operation ids are
    globally unique and short-lived (cleared once observed as done/error/
    cancelled via a status check, so this never grows unbounded)."""

    def __init__(self) -> None:
        self._ops: dict[str, Operation] = {}

    def create(self, tool_name: str, tab_id: str | None = None) -> Operation:
        op = Operation(id=uuid.uuid4().hex[:12], tool_name=tool_name, tab_id=tab_id)
        self._ops[op.id] = op
        return op

    def get(self, operation_id: str) -> Operation | None:
        return self._ops.get(operation_id)

    def forget(self, operation_id: str) -> None:
        self._ops.pop(operation_id, None)

    def cancel_for_tab(self, tab_id: str) -> int:
        """Cancels every still-running operation tagged with this tab_id --
        called from ChatSession.stop() alongside client.interrupt() so
        Stop actually reaches a tool call that already crossed dispatch()'s
        fast-path window and became a detached background task (interrupt()
        alone only stops the model's own generation stream, not that
        task). Returns how many were actually cancelled, for logging."""
        cancelled = 0
        for op in self._ops.values():
            if op.tab_id == tab_id and op.status == "running" and op.task is not None and not op.task.done():
                op.task.cancel()
                cancelled += 1
        return cancelled


REGISTRY = OperationRegistry()


def _notify_operation_completed(plugin_name: str, op: Operation) -> None:
    """Per explicit instruction (2026-09-15): confirmed live as a real,
    "regular" gap -- a background operation the model already walked away
    from (dispatch()'s slow-path "running" outcome) had no way to surface
    its own completion; nothing guaranteed anyone would ever poll
    check_operation_status again to find out. Fires a proactive nudge into
    the SAME tab's session via session_context.get_inject_proactive
    (mirrors viewer_plugin.py's editor_result -> inject_proactive pattern
    for the identical "async thing finished, tell the model" shape). Silent
    no-op outside a live ChatSession turn (get_inject_proactive returns
    None then) -- a throwaway test script's dispatched operation has no
    session to notify, same non-fatal shape as get_tab_id()."""
    inject = get_inject_proactive()
    if inject is None:
        return
    if op.status == "done":
        outcome = f"finished successfully. Result: {op.result}"
    elif op.status == "error":
        outcome = f"failed with an error: {op.error}"
    else:
        return
    inject(
        f"[Internal: a background operation you started earlier (tool: {op.tool_name}, "
        f"operation_id: {op.id}) just {outcome} You never checked back on this one directly -- "
        "react to it now if it's relevant (e.g. tell the user what happened), rather than "
        "leaving it unmentioned.]"
    )
    log_event(f"plugin:{plugin_name}", "operation_completion_notified", tool=op.tool_name, operation_id=op.id, status=op.status)


async def dispatch(plugin_name: str, tool_name: str, handler: ToolHandler, args: dict[str, Any]) -> dict[str, Any]:
    """Runs one tool call through the uniform start/status/stop contract.
    Always logs call/result/error/duration at the engine level (see
    plugins/loader.py's wrap_tool, which calls this) -- individual plugins
    never implement their own polling/cancellation plumbing."""
    op = REGISTRY.create(tool_name, tab_id=get_tab_id())

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
        finally:
            # Per explicit instruction (2026-09-15): only for an operation
            # dispatch() already told its caller "running" for (see the
            # TimeoutError branch below, which sets notify_on_completion)
            # -- a fast-path operation's caller already has the real
            # result synchronously, nothing to notify. Never for
            # "cancelled" -- whoever cancelled it (REGISTRY.cancel_for_tab,
            # stop_operation) already knows.
            if op.notify_on_completion and op.status != "cancelled":
                _notify_operation_completed(plugin_name, op)
            # Same "only if it was ever persisted" gating -- see
            # save_pending_operation's own call site below. Clears
            # regardless of tab_id being set; clear_pending_operation is a
            # safe no-op if nothing was ever saved for this operation_id.
            if op.notify_on_completion and op.tab_id:
                clear_pending_operation(WORKSPACE_DIR, op.tab_id, op.id)

    op.task = asyncio.create_task(run())
    log_event(f"plugin:{plugin_name}", "operation_started", tool=tool_name, operation_id=op.id)

    try:
        result = await asyncio.wait_for(asyncio.shield(op.task), timeout=FAST_PATH_TIMEOUT_S)
        duration_ms = round((time.monotonic() - op.started_at) * 1000, 1)
        log_event(f"plugin:{plugin_name}", "operation_done_fast", tool=tool_name, operation_id=op.id, duration_ms=duration_ms)
        REGISTRY.forget(op.id)
        return {"operation_id": op.id, "status": "done", "result": result}
    except asyncio.TimeoutError:
        op.notify_on_completion = True
        # Per explicit instruction (2026-09-15): see durability.py's own
        # "per-tab in-flight background operations" section for why this
        # exists -- a full backend-process restart while this is still
        # running wipes OperationRegistry with zero trace; this is the
        # trace. Only when tab_id is actually known (a throwaway test
        # script's dispatched operation has no tab to recover for anyway).
        if op.tab_id:
            save_pending_operation(WORKSPACE_DIR, op.tab_id, op.id, tool_name)
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


def _build_describe_own_backend_tool(own_plugins: list[dict[str, Any]]) -> SdkMcpTool[Any]:
    """Per explicit instruction (2026-09-18), after a real incident and a
    direct architectural correction: Caroline is a product installed on
    many different machines, each with its own, potentially completely
    different set of independently-registered MCP servers -- nothing
    about any SPECIFIC external server name can ever be hardcoded
    anywhere in this codebase (a prior attempt at exactly that, naming
    caroline-browser/caroline-voice/caroline-screen-video directly in
    policies.py and chat_session.py's disallowed_tools, was explicitly
    rejected as the wrong shape of fix -- the same rewrite-it-again
    anti-pattern every time some OTHER machine's install turns out to
    have yet another stale external server registered).

    The general fix: this tool is THE single source of truth for "what's
    mine" -- built fresh every turn (own_plugins, passed in by
    plugins/loader.py's build_mcp_servers(), reflects THIS install's
    actual current plugin set, discovered dynamically via
    discover_plugins(), never a fixed list) so Caroline can always tell
    her own backend-provided tools apart from anything else she might see
    (an external MCP server registered independently of this backend,
    which varies install to install and this backend has no way to know
    about ahead of time), on demand, without any name ever needing to be
    written down in advance -- see prefer_own_backend_tools_instruction
    (policies.py), which points at this tool instead of naming anything
    itself."""

    @sdk_tool(
        "describe_own_backend",
        "Returns a live, authoritative description of your own backend right now, on this exact install -- "
        "which plugins/tools it currently provides. Call this whenever you're unsure whether a specific tool "
        "belongs to your own backend or comes from somewhere else (a separately/independently-registered MCP "
        "server, which varies from machine to machine), or when the user asks about your own architecture, "
        "setup, or capabilities.",
        {},
    )
    async def describe_own_backend(args: dict[str, Any]) -> dict[str, Any]:
        log_event("engine", "describe_own_backend_called", plugin_count=len(own_plugins))
        body = {
            "architecture": (
                "You run on a Python backend with a plugin system. Every plugin is exposed to you as its own "
                "MCP server, named \"caroline-<plugin-name>\" (see \"plugins\" below for the exact current "
                "list, name and tools included) -- rebuilt fresh from this backend's actual plugin set on "
                "every turn, so this is always accurate for right now, not something memorized or stale. Any "
                "OTHER MCP server you can see that is NOT in this list comes from somewhere else entirely -- "
                "registered independently of this backend, on this specific machine, outside this backend's "
                "knowledge or control, and not guaranteed to even be working. Whenever a task can be done by "
                "a tool listed here, always prefer it over a same-purpose tool from an unlisted server, even "
                "if the other one looks more convenient, is already connected, or seems more familiar."
            ),
            "plugins": own_plugins,
        }
        return {"content": [{"type": "text", "text": str(body)}]}

    return describe_own_backend


def build_operations_mcp_server(tool_instructions: dict[str, str] | None = None, own_plugins: list[dict[str, Any]] | None = None) -> McpServerConfig:
    """The generic operation-control tools (check_operation_status/
    stop_operation) plus get_tool_instructions and describe_own_backend,
    registered ONCE at the engine level (not per plugin) -- every plugin
    tool call goes through dispatch() above, which is what makes the
    first two apply universally; get_tool_instructions/describe_own_backend
    are built fresh per call here since they need this turn's actual
    tool_instructions/own_plugins data (see plugins/loader.py's
    build_mcp_servers(), which computes both from discover_plugins())."""
    tools = [
        check_operation_status, stop_operation,
        _build_get_tool_instructions_tool(tool_instructions or {}),
        _build_describe_own_backend_tool(own_plugins or []),
    ]
    return create_sdk_mcp_server(name="operations", tools=tools)
