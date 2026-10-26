"""Tool groups (app/tool_groups.py) and the group tools server built by plugins/loader.py.
Run: python -m pytest test_tool_groups.py -q"""

import asyncio

import pytest

from app import tool_groups
from app.plugins import loader
from app.plugins.loader import Plugin, PluginTool


def _tool(name, calls, description="does a thing"):
    async def handler(args, report_progress):
        calls.append((name, args))
        return {"ok": True, "echo": args}

    return PluginTool(name=name, description=description, input_schema={"x": str}, handler=handler)


@pytest.fixture
def calls():
    return []


@pytest.fixture
def plugins(calls):
    return [
        Plugin(name="greeter", tools=[_tool("greet", calls), _tool("wave", calls)],
               usage_instructions="Be polite.", description="Says hello"),
        Plugin(name="core", tools=[_tool("save", calls)], always_loaded=True),
    ]


def test_groups_text_lists_only_grouped_plugins(plugins):
    text = tool_groups.groups_text(plugins)
    assert "greeter (2 tools)" in text and "greet, wave" in text
    assert "core" not in text


def test_list_and_describe(plugins):
    assert "- greet:" in tool_groups.list_tools_text(plugins, "greeter")
    assert "caroline-greeter" and "Group greeter" in tool_groups.list_tools_text(plugins, "caroline-greeter")
    assert "No tool group" in tool_groups.list_tools_text(plugins, "nope")
    d = tool_groups.describe_tool_text(plugins, "greet")
    assert "Be polite." in d and "call_tool" in d
    assert "No tool" in tool_groups.describe_tool_text(plugins, "save")  # always-loaded: not behind the groups


def test_refusal(plugins):
    assert tool_groups.refusal_for_call(plugins, "greet", {}) is None
    assert tool_groups.refusal_for_call(plugins, "greet", "x")
    assert tool_groups.refusal_for_call(plugins, "unknown", {})


def test_flat_switch(plugins, monkeypatch):
    monkeypatch.setenv("CAROLINE_FLAT_TOOLS", "1")
    assert not tool_groups.enabled()
    assert tool_groups.groups_text(plugins) == "(no tool groups)"


def test_normalize_call(plugins):
    tool_groups.remember_catalog(plugins)
    name, args = tool_groups.normalize_call("mcp__caroline-tools__call_tool", {"name": "greet", "args": {"x": "1"}})
    assert name == "mcp__caroline-greeter__greet" and args == {"x": "1"}
    assert tool_groups.normalize_call("mcp__caroline-core__save", {"a": 1}) == ("mcp__caroline-core__save", {"a": 1})
    assert tool_groups.normalize_call("call_tool", {"name": "greet", "args": None})[1] == {}


def test_build_group_server_and_call(plugins, calls, monkeypatch):
    monkeypatch.setattr(loader, "discover_plugins", lambda: plugins)
    captured = {}

    def fake_create(name, tools):
        captured[name] = {t.name: t for t in tools}
        return {"type": "sdk", "name": name}

    monkeypatch.setattr(loader, "create_sdk_mcp_server", fake_create)
    monkeypatch.setattr(loader, "build_operations_mcp_server", lambda *a, **k: {"type": "sdk", "name": "operations"})
    servers = loader.build_mcp_servers()
    assert "caroline-tools" in servers and "caroline-core" in servers and "caroline-greeter" not in servers
    group_tools = captured["tools"]
    assert set(group_tools) == set(tool_groups.META_TOOL_NAMES)
    assert "greeter" in group_tools["list_groups"].description

    out = asyncio.run(group_tools["call_tool"].handler({"name": "greet", "args": {"x": "hi"}}))
    assert calls == [("greet", {"x": "hi"})] and not out.get("isError")
    bad = asyncio.run(group_tools["call_tool"].handler({"name": "nope", "args": {}}))
    assert bad.get("isError")


def test_real_catalog_end_to_end(monkeypatch):
    """The real plugins: every grouped tool can be described, and a harmless one runs through call_tool -> dispatch."""
    captured = {}
    original = loader.create_sdk_mcp_server

    def capture(name, tools):
        captured[name] = {t.name: t for t in tools}
        return original(name=name, tools=tools)

    monkeypatch.setattr(loader, "create_sdk_mcp_server", capture)
    loader.build_mcp_servers()
    real = loader.discover_plugins()
    grouped = tool_groups.grouped_plugins(real)
    assert grouped and "caroline-tools" is not None
    for plugin in grouped:
        for tool in plugin.tools:
            assert not tool_groups.describe_tool_text(real, tool.name).startswith("No tool")
    out = asyncio.run(captured["tools"]["call_tool"].handler({"name": "get_current_time", "args": {}}))
    assert "done" in out["content"][0]["text"]
