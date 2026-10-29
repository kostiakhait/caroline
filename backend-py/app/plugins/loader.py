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

from app import tool_groups
from app.logging_setup import log_event
from app.operations import ToolHandler, build_operations_mcp_server, dispatch
import types as _types
import typing as _typing


def _is_optional_type(annotation: Any) -> bool:
    """True for `X | None` / Optional[X] -- a parameter the model may leave out."""
    if _typing.get_origin(annotation) in (_typing.Union, _types.UnionType):
        return type(None) in _typing.get_args(annotation)
    return annotation is type(None)


def optional_keys(input_schema: Any) -> list[str]:
    """The parameters of a `{name: type}` schema that were declared optional (`str | None`)."""
    if not isinstance(input_schema, dict) or ("type" in input_schema and "properties" in input_schema):
        return []
    return [name for name, annotation in input_schema.items() if _is_optional_type(annotation)]


def missing_required(plugin_tool: "PluginTool", args: dict[str, Any]) -> list[str]:
    """The required parameters of a tool that a call left out or passed as null -- from a
    `{name: type}` schema (the ones not declared `X | None`) or a JSON Schema's "required"."""
    schema = plugin_tool.input_schema
    if not isinstance(schema, dict):
        return []
    if "type" in schema and "properties" in schema:
        required = list(schema.get("required") or [])
    else:
        optional = set(optional_keys(schema))
        required = [name for name in schema if name not in optional]
    return [name for name in required if args.get(name) is None]


def schema_with_optionals(input_schema: Any) -> Any:
    """The JSON Schema of a tool whose optional parameters are really optional.

    The SDK turns a `{name: type}` schema into one with EVERY parameter required, `X | None` included, so a model
    had to supply even the ones it had nothing for -- a smaller model failed request_info five times in a row on
    "'history' is a required property" (2026-10-26). Here only the parameters not declared optional stay required;
    wrap_tool() fills the left-out optional ones with None, which is what a model passing null used to produce."""
    optional = optional_keys(input_schema)
    if not optional:
        return input_schema
    full = _sdk_build_input_schema(SdkMcpTool(name="", description="", input_schema=input_schema, handler=None))
    return {**full, "required": [k for k in full.get("required", []) if k not in optional]}


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
    # One line saying what the plugin is for -- shown in the tool-group list (app/tool_groups.py). Optional: the
    # group line always lists the tool names anyway.
    description: str | None = None
    # True: the tools stay directly callable, not behind the group tools. For the few tools used all the time.
    always_loaded: bool = False


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

    left_out_ok = optional_keys(plugin_tool.input_schema)

    @sdk_tool(plugin_tool.name, plugin_tool.description, schema_with_optionals(plugin_tool.input_schema))
    async def dispatching_handler(args: dict[str, Any]) -> dict[str, Any]:
        if left_out_ok:
            args = {**{k: None for k in left_out_ok}, **args}  # what a model passing null would have sent
        envelope = await dispatch(plugin_name, plugin_tool.name, plugin_tool.handler, args)
        response = _envelope_to_mcp_response(envelope)
        if envelope.get("memory_note"):
            response["content"] = list(response["content"]) + [{"type": "text", "text": envelope["memory_note"]}]
        return response

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
        # Bug fix (2026-10-16), confirmed live on a real Linux test box: a
        # plugin module that fails to import (e.g. app_browser_cdp.py's
        # unconditional `from playwright.async_api import ...` -- playwright
        # is a Windows-only extra per requirements-linux.txt's own docstring,
        # deliberately absent on Linux) used to propagate straight out of
        # this function, since import_module() here had no try/except at
        # all. discover_plugins() runs fresh on EVERY query() build (see
        # this module's own docstring), so that ModuleNotFoundError crashed
        # every single turn -- not just disabled one plugin -- putting every
        # tab into a permanent restart_backoff loop ("Trouble reconnecting").
        # requirements-linux.txt's own claim that Windows-only plugins "fail
        # to import on Linux and are skipped" was never actually true at
        # this choke point; this is what makes it true.
        try:
            # A module already loaded is never reloaded (2026-10-26): a reload made new exception classes in sw_api that the
            # modules loaded before it (memory, speech, companion, calendar, evolution_client) no longer caught, and reset
            # module state (the funds flag, every plugin's SW session). A new plugin file is still picked up here.
            module = importlib.import_module(f"app.plugins.{module_info.name}")
        except Exception as exc:  # noqa: BLE001 -- one broken/platform-unavailable plugin must never cost every other plugin, or the whole turn
            log_event("engine", "plugin_import_failed", module=module_info.name, error=str(exc), error_type=type(exc).__name__)
            continue
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
    stop_operation/get_tool_instructions/describe_own_backend,
    app/operations.py) -- those apply uniformly to every plugin tool
    call, not just one plugin's own. get_tool_instructions is built with
    THIS turn's tool_instructions map (every tool name a loaded plugin
    exposes -> that plugin's own usage_instructions, if it set one) so
    the model can fetch a specific tool's detailed guidance on demand
    instead of it being injected into the system prompt (see policies.py's
    module docstring for why).

    Per explicit instruction (2026-09-18): own_plugins (server_name + each
    tool's name/description, one entry per plugin discovered THIS turn)
    is built here, alongside tool_instructions, and handed to
    build_operations_mcp_server for describe_own_backend -- this is the
    ONE place that knows the real, current plugin set, so it's the only
    place that should ever compute it. Never hardcode a specific plugin/
    server/tool name anywhere else in this codebase to reason about "is
    this one of Caroline's own tools" -- always derive it from here,
    fresh, since which plugins exist can differ across installs and even
    across a single install's own history as plugins are added/removed.
    """
    plugins = discover_plugins()
    tool_instructions: dict[str, str] = {}
    own_plugins: list[dict[str, Any]] = []
    mcp_servers: dict[str, McpServerConfig] = {}
    tool_groups.remember_catalog(plugins)
    for plugin in plugins:
        server_name = f"caroline-{plugin.name}"
        if not tool_groups.is_grouped(plugin):
            wrapped_tools = [wrap_tool(plugin.name, t) for t in plugin.tools]
            mcp_servers[server_name] = create_sdk_mcp_server(name=plugin.name, tools=wrapped_tools)
        own_plugins.append({
            "server_name": server_name,
            "tools": [{"name": t.name, "description": t.description} for t in plugin.tools],
        })
        if plugin.usage_instructions:
            for t in plugin.tools:
                tool_instructions[t.name] = plugin.usage_instructions
    if tool_groups.grouped_plugins(plugins):
        mcp_servers["caroline-tools"] = _build_group_tools_server(plugins)
    mcp_servers["caroline-operations"] = build_operations_mcp_server(tool_instructions, own_plugins)
    return mcp_servers


def _build_group_tools_server(plugins: list[Plugin]) -> McpServerConfig:
    """list_groups / list_tools / describe_tool / call_tool (app/tool_groups.py): the only way to reach the tools
    of the grouped plugins, so the base request carries one line per group instead of every tool's full
    schema. call_tool runs the tool through wrap_tool() -- the same dispatch() as a direct registration."""
    wrapped: dict[str, SdkMcpTool[Any]] = {}

    def reply(text: str, error: bool = False) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}], **({"isError": True} if error else {})}

    groups = tool_groups.groups_text(plugins)

    @sdk_tool(
        tool_groups.LIST_GROUPS,
        "Your tool groups. Most of your tools are not listed one by one: they sit in groups, one line each "
        "(group, number of tools, the first tool names). To use one: list_tools(group) for the group's tools, "
        "describe_tool(name) for its parameters and guidance, then call_tool(name, args). The groups now:\n" + groups,
        {},
    )
    async def list_groups(args: dict[str, Any]) -> dict[str, Any]:
        log_event("engine", "tool_groups_list_groups")
        return reply(groups)

    @sdk_tool(tool_groups.LIST_TOOLS, "The tools of one group: name and short description. Takes the group name from list_groups.", {"group": str})
    async def list_tools(args: dict[str, Any]) -> dict[str, Any]:
        log_event("engine", "tool_groups_list_tools", group=args.get("group"))
        return reply(tool_groups.list_tools_text(plugins, str(args.get("group") or "")))

    @sdk_tool(
        tool_groups.DESCRIBE_TOOL,
        "One tool in full: description, parameters (JSON schema) and the usage guidance of its group. Call it before "
        "the first use of a tool you have not used in this conversation.",
        {"name": str},
    )
    async def describe_tool(args: dict[str, Any]) -> dict[str, Any]:
        log_event("engine", "tool_groups_describe_tool", tool=args.get("name"))
        return reply(tool_groups.describe_tool_text(plugins, str(args.get("name") or "")))

    @sdk_tool(
        tool_groups.CALL_TOOL,
        "Calls a tool of a group: name = the tool's name, args = an object with its parameters ({} when it has none). "
        "It behaves exactly as a direct call of that tool (results, long-running operations, check_operation_status).",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}, "args": {"type": "object"}},
            "required": ["name"],
        },
    )
    async def call_tool(args: dict[str, Any]) -> dict[str, Any]:
        name, inner = str(args.get("name") or ""), args.get("args")
        inner = {} if inner is None else inner
        refusal = tool_groups.refusal_for_call(plugins, name, inner)
        if refusal:
            log_event("engine", "tool_groups_call_refused", tool=name, reason=refusal[:200])
            return reply(refusal, error=True)
        plugin, plugin_tool = tool_groups.find_tool(plugins, name)  # type: ignore[misc]
        if name not in wrapped:
            wrapped[name] = wrap_tool(plugin.name, plugin_tool)
        return await wrapped[name].handler(inner)

    return create_sdk_mcp_server(name="tools", tools=[list_groups, list_tools, describe_tool, call_tool])
