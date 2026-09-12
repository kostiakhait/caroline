"""Plugin discovery/loading -- the new architectural piece replacing today's
Node backend's hand-wiring of every tool family directly into server.ts's
mcpServers object and systemPrompt.append array.

A plugin is any module under app/plugins/ (other than this loader itself)
exposing a module-level `PLUGIN` object (see Plugin below). True
hot-plugging falls out of an architecture Caroline already has, not a new
live-reload mechanism: query() gets recreated on effectively every turn
(dehydration restarts, hang recovery, etc.), so load_plugins() is called
fresh each time that turn's mcp_servers config is assembled -- drop a new
plugin module in and it's live within one turn, no explicit reload call,
no app restart.

Every tool call made through a loaded plugin goes through the uniform
start/status/stop contract (see app/operations.py's dispatch()) and is
automatically logged (args, result or error, duration) -- see wrap_tool()
-- so individual plugins never need to remember their own call/result
logging or their own polling/cancellation plumbing.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import McpServerConfig, SdkMcpTool, create_sdk_mcp_server, tool as sdk_tool
from claude_agent_sdk import _build_input_schema as _sdk_build_input_schema

from app.logging_setup import log_event
from app.operations import ToolHandler, build_operations_mcp_server, dispatch


@dataclass
class PluginTool:
    """One tool a plugin exposes. `input_schema` is whatever
    claude_agent_sdk.tool() accepts -- a JSON-schema-shaped dict or a type
    (see its own signature); kept loose here rather than importing a
    specific schema library, so plugin authors can use pydantic, a plain
    dict, or a dataclass, whichever they prefer.

    `handler(args, report_progress)` -- per the confirmed uniform
    start/status/stop contract (2026-09-09), EVERY tool's handler takes
    this same two-argument shape, whether it finishes in a millisecond or
    runs for minutes. `report_progress(data)` may be called zero or more
    times during execution to publish intermediate data a status poll can
    see; a trivially-fast handler just never calls it. The handler's
    return value is the tool's final result -- dispatch() (app/operations.py)
    decides on the caller's behalf whether that arrived fast enough to
    report directly as status="done", or needs to become a pollable
    operation_id instead. Plugin authors never touch this decision.
    """

    name: str
    description: str
    input_schema: Any
    handler: ToolHandler


@dataclass
class Plugin:
    """What a plugin module must expose as its module-level PLUGIN object.
    `tools` are wrapped with automatic call/result/duration logging (see
    wrap_tool) before being handed to the SDK -- plugin authors write plain
    async functions, the dispatcher supplies the logging guarantee."""

    name: str
    tools: list[PluginTool]
    # Optional detailed usage guidance for this plugin's tools (the Python
    # equivalent of one policies.ts instruction-builder function, but
    # owned by the plugin itself rather than centralized) -- explicit
    # architectural requirement (2026-09-09): this is NEVER auto-appended
    # to the system prompt. It's exposed only through the generic
    # get_tool_instructions tool (app/operations.py), which the model
    # calls on demand for a specific tool name. Every tool this plugin
    # exposes maps to this SAME text (see build_mcp_servers()'s
    # tool_instructions map) -- there is no per-tool granularity, since a
    # plugin's tools are usually used together and share one set of
    # conventions/gotchas. Empty/None means the plugin's tools are
    # self-explanatory from their own short `description` alone.
    usage_instructions: str | None = None


def _envelope_to_mcp_response(envelope: dict[str, Any]) -> dict[str, Any]:
    """Renders dispatch()'s {operation_id, status, result/error} envelope
    into a real MCP tool response ({content: [...], isError?: bool}). A
    handler's own `result` MAY already be MCP-content-shaped (a dict with a
    "content" list of {type:"text"/"image"/...} blocks, e.g.
    screenshot_plugin returning a real image block so the model can
    actually SEE it) -- used directly, rather than being flattened into one
    text blob. A result dict's own "is_error": True (e.g. chain_plugin
    reporting a failed step sequence) is propagated to the MCP response's
    top-level isError, same as the TS servers' own isError-on-failure
    convention. Anything else (the common case -- a plain result dict, e.g.
    time_plugin's {"text": "..."}) falls back to stringifying the whole
    envelope as a single text block."""
    status = envelope.get("status")
    if status == "running":
        return {"content": [{"type": "text", "text": f"Operation {envelope['operation_id']} is still running -- poll it with check_operation_status."}]}
    if status == "error":
        return {"content": [{"type": "text", "text": f"Operation {envelope['operation_id']} failed: {envelope.get('error')}"}], "isError": True}
    result = envelope.get("result")
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        return {"content": list(result["content"]), **({"isError": True} if result.get("is_error") else {})}
    if isinstance(result, dict) and "image_base64" in result:
        blocks: list[dict[str, Any]] = []
        if result.get("text"):
            blocks.append({"type": "text", "text": result["text"]})
        blocks.append({"type": "image", "data": result["image_base64"], "mimeType": result.get("mime_type", "image/png")})
        return {"content": blocks, **({"isError": True} if result.get("is_error") else {})}
    return {"content": [{"type": "text", "text": str(envelope)}]}


def wrap_tool(plugin_name: str, plugin_tool: PluginTool) -> SdkMcpTool[Any]:
    """Wraps one plugin tool's handler with the uniform start/status/stop
    contract (app/operations.py's dispatch(), which also does the
    call/result/duration logging) -- plugin authors write a plain
    (args, report_progress) -> result handler, this is what turns that into
    an MCP tool whose OWN result is always {operation_id, status, ...},
    never a raw blocking return."""

    @sdk_tool(plugin_tool.name, plugin_tool.description, plugin_tool.input_schema)
    async def dispatching_handler(args: dict[str, Any]) -> dict[str, Any]:
        envelope = await dispatch(plugin_name, plugin_tool.name, plugin_tool.handler, args)
        return _envelope_to_mcp_response(envelope)

    return dispatching_handler


def to_openai_tool_def(plugin_tool: PluginTool) -> dict[str, Any]:
    """The OpenAI/Camerlengo function-calling shape for the SAME PluginTool
    wrap_tool() above already wraps for the SDK/MCP path -- per explicit
    instruction (2026-09-12): tool schemas are NOT translated from one
    format to the other, both are supported natively from the one
    PluginTool.input_schema a plugin author already wrote. Reuses the SDK's
    own _build_input_schema() (claude_agent_sdk/__init__.py) -- the exact
    same function the SDK path uses internally to normalize input_schema
    (a plain JSON-schema dict, a TypedDict, or a simple {name: type}
    mapping) into real JSON Schema -- so both paths see byte-identical
    parameter schemas from one source of truth, never a hand-rolled second
    implementation that could quietly drift from the SDK's own. Confirmed
    live: _build_input_schema only ever reads tool_def.input_schema, so it
    works directly on a PluginTool, no adapter object needed."""
    return {
        "type": "function",
        "function": {
            "name": plugin_tool.name,
            "description": plugin_tool.description,
            "parameters": _sdk_build_input_schema(plugin_tool),
        },
    }


def discover_plugins() -> list[Plugin]:
    """Imports every module directly under app/plugins/ (except this loader
    and any module starting with "_") and collects their PLUGIN object.
    Called fresh on every query() build -- see this module's own doc
    comment for why that alone is enough for hot-plugging."""
    plugins: list[Plugin] = []
    package_dir = Path(__file__).parent
    for module_info in pkgutil.iter_modules([str(package_dir)]):
        if module_info.name in ("loader",) or module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"app.plugins.{module_info.name}")
        importlib.reload(module)  # pick up on-disk edits without a process restart
        plugin = getattr(module, "PLUGIN", None)
        if plugin is None:
            log_event("engine", "plugin_skipped", module=module_info.name, reason="no PLUGIN object")
            continue
        plugins.append(plugin)
        log_event("engine", "plugin_loaded", plugin=plugin.name, tool_count=len(plugin.tools))
    return plugins


def build_mcp_servers() -> dict[str, McpServerConfig]:
    """Returns the mcp_servers dict for the NEXT query() -- one in-process
    SDK MCP server per discovered plugin. One discover_plugins() call, so
    a plugin's "plugin_loaded" log line appears exactly once per turn.
    Always includes the generic "operations" server (check_operation_status/
    stop_operation/get_tool_instructions, app/operations.py) -- those apply
    uniformly to every plugin tool call, not just one plugin's own.
    get_tool_instructions is built with THIS turn's tool_instructions map
    (every tool name a loaded plugin exposes -> that plugin's own
    usage_instructions, if it set one) so the model can fetch a specific
    tool's detailed guidance on demand instead of it being injected into
    the system prompt (see policies.py's module docstring for why).
    """
    plugins = discover_plugins()
    tool_instructions: dict[str, str] = {}
    mcp_servers: dict[str, McpServerConfig] = {}
    for plugin in plugins:
        wrapped_tools = [wrap_tool(plugin.name, t) for t in plugin.tools]
        mcp_servers[f"caroline-{plugin.name}"] = create_sdk_mcp_server(name=plugin.name, tools=wrapped_tools)
        if plugin.usage_instructions:
            for t in plugin.tools:
                tool_instructions[t.name] = plugin.usage_instructions
    mcp_servers["caroline-operations"] = build_operations_mcp_server(tool_instructions)
    return mcp_servers
