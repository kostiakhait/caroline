"""Tool groups instead of a flat list of every tool (2026-10-26, explicit instruction).

Before: every request of every engine carried the names, descriptions and schemas of all ~270 tools of ~45
plugins (~35 thousand tokens) -- for a model that needs two or three of them. Now the base request holds only
the GROUPS (one line per plugin); the model asks for what it needs, step by step:

    list_groups()                     -> the groups, one line each (the same text is in this tool's description)
    list_tools(group)                 -> the tools of one group, name and short description
    describe_tool(name)               -> full description, parameters and usage guidance of one tool
    call_tool(name, args)             -> calls the tool (through the same dispatch() every tool call uses, so
                                         permissions, logging, operations and memory nudges are unchanged)

A plugin with `always_loaded=True` (the memory tools, used constantly) stays a normal, directly callable
server. Everything is derived from discover_plugins() -- no tool or plugin name is written down here.
The SW cascade uses the same text builders (small_model_engine.py), the Claude and Codex engines get the
tools from plugins/loader.py's build_mcp_servers().

CAROLINE_FLAT_TOOLS=1 brings back the old flat registration."""

from __future__ import annotations

import os
from typing import Any

LIST_GROUPS = "list_groups"
LIST_TOOLS = "list_tools"
DESCRIBE_TOOL = "describe_tool"
CALL_TOOL = "call_tool"
META_TOOL_NAMES = (LIST_GROUPS, LIST_TOOLS, DESCRIBE_TOOL, CALL_TOOL)

MAX_NAMES_IN_GROUP_LINE = 8
MAX_TOOL_LINE_CHARS = 200

_server_of: dict[str, str] = {}      # tool name -> "caroline-<plugin>", filled by remember_catalog()


def enabled() -> bool:
    return os.environ.get("CAROLINE_FLAT_TOOLS") != "1"


def remember_catalog(plugins: list[Any]) -> None:
    _server_of.clear()
    for plugin in plugins:
        for tool in plugin.tools:
            _server_of[tool.name] = f"caroline-{plugin.name}"


def is_grouped(plugin: Any) -> bool:
    """True for a plugin whose tools are reached through the meta tools."""
    return enabled() and not getattr(plugin, "always_loaded", False)


def grouped_plugins(plugins: list[Any]) -> list[Any]:
    return [p for p in plugins if is_grouped(p) and p.tools]


def group_line(plugin: Any) -> str:
    names = [t.name for t in plugin.tools]
    more = f", +{len(names) - MAX_NAMES_IN_GROUP_LINE} more" if len(names) > MAX_NAMES_IN_GROUP_LINE else ""
    what = (getattr(plugin, "description", None) or "").strip()
    listed = ", ".join(names[:MAX_NAMES_IN_GROUP_LINE]) + more
    return f"- {plugin.name} ({len(names)} tools){': ' + what if what else ''} [{listed}]"


def groups_text(plugins: list[Any]) -> str:
    lines = [group_line(p) for p in grouped_plugins(plugins)]
    return "\n".join(lines) if lines else "(no tool groups)"


def _short(text: str | None) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= MAX_TOOL_LINE_CHARS else one[:MAX_TOOL_LINE_CHARS - 1] + "…"


def find_group(plugins: list[Any], name: str) -> Any | None:
    wanted = (name or "").strip().removeprefix("caroline-").lower()
    return next((p for p in grouped_plugins(plugins) if p.name.lower() == wanted), None)


def find_tool(plugins: list[Any], name: str) -> tuple[Any, Any] | None:
    """(plugin, tool) of a grouped tool by its name."""
    wanted = (name or "").strip()
    for plugin in grouped_plugins(plugins):
        for tool in plugin.tools:
            if tool.name == wanted:
                return plugin, tool
    return None


def list_tools_text(plugins: list[Any], group: str) -> str:
    plugin = find_group(plugins, group)
    if plugin is None:
        return f'No tool group "{group}". The groups:\n{groups_text(plugins)}'
    lines = [f"- {t.name}: {_short(t.description)}" for t in plugin.tools]
    hint = "" if not getattr(plugin, "usage_instructions", None) else "\n(This group has usage guidance: describe_tool shows it.)"
    return f"Group {plugin.name}:\n" + "\n".join(lines) + hint


def _schema(tool: Any) -> Any:
    try:
        from claude_agent_sdk import SdkMcpTool, _build_input_schema
        from app.plugins.loader import schema_with_optionals

        return _build_input_schema(SdkMcpTool(
            name=tool.name, description=tool.description, input_schema=schema_with_optionals(tool.input_schema), handler=None,
        ))
    except Exception:  # noqa: BLE001 -- the raw schema is still informative
        return tool.input_schema


def describe_tool_text(plugins: list[Any], name: str) -> str:
    import json

    found = find_tool(plugins, name)
    if found is None:
        return f'No tool "{name}" in the groups. Use list_groups, then list_tools(group).'
    plugin, tool = found
    parts = [
        f"{tool.name} (group {plugin.name})",
        (tool.description or "").strip(),
        "Parameters (JSON schema): " + json.dumps(_schema(tool), ensure_ascii=False, default=str),
    ]
    if getattr(plugin, "usage_instructions", None):
        parts.append("Usage guidance for this group:\n" + plugin.usage_instructions)
    parts.append(f'Call it with call_tool(name="{tool.name}", args={{...}}).')
    return "\n\n".join(parts)


def refusal_for_call(plugins: list[Any], name: str, args: Any) -> str | None:
    """A reason the call cannot be made (unknown tool, args not an object), written so the model can fix its next call; else None."""
    if not isinstance(args, dict):
        return 'call_tool needs "args": an object with the tool\'s parameters (use {} for a tool without parameters).'
    if find_tool(plugins, name) is None:
        return f'No tool "{name}" in the groups. Use list_groups, then list_tools(group) for the exact names.'
    # Missing parameters are NOT checked here: the schema lists every parameter as required, while the tools
    # fill in what they know themselves (a saved mailbox's password and servers, say) -- dispatch() reports
    # what is really missing, as it always did.
    return None


# --- the meta calls as seen by code that reads tool calls --------------------------

def unwrap_call(name: str, args: Any) -> tuple[str, dict[str, Any]]:
    """call_tool(name, args) -> the real (name, args); anything else unchanged."""
    if name.rsplit("__", 1)[-1] == CALL_TOOL and isinstance(args, dict) and isinstance(args.get("name"), str):
        inner = args.get("args")
        return args["name"], inner if isinstance(inner, dict) else {}
    return name, args if isinstance(args, dict) else {}


def effective_tool_name(name: str, args: Any) -> str:
    """The name a reader of the tool-call stream should see: for call_tool, the real tool's own name in the
    "mcp__<server>__<tool>" form it would have had when registered directly."""
    real, _ = unwrap_call(name, args)
    if real == name:
        return name
    server = _server_of.get(real)
    return f"mcp__{server}__{real}" if server else real


def normalize_call(name: str, args: Any) -> tuple[str, Any]:
    """(name, args) of a tool call as a reader of the stream should see it: a call_tool becomes the real tool's own
    qualified name and its own arguments; anything else is returned unchanged."""
    real, inner = unwrap_call(name, args)
    if real == name:
        return name, args
    return effective_tool_name(name, args), inner
