"""The Ferret editor deployment: CAROLINE_ONLY_PLUGINS / CAROLINE_EXCLUDED_TOOLS shape the tool set, the ferret-editor plugin exists only when
CAROLINE_FERRET_EDITOR=1, and a page's answer reaches the waiting call. Everything is inert when the variables are unset."""
import asyncio
import importlib

from app.plugins import loader


def _reload_plugin(monkeypatch, enabled):
    if enabled:
        monkeypatch.setenv("CAROLINE_FERRET_EDITOR", "1")
    else:
        monkeypatch.delenv("CAROLINE_FERRET_EDITOR", raising=False)
    from app.plugins import ferret_editor_plugin
    return importlib.reload(ferret_editor_plugin)


def test_unset_changes_nothing(monkeypatch):
    for v in ("CAROLINE_ONLY_PLUGINS", "CAROLINE_EXCLUDED_TOOLS", "CAROLINE_EXCLUDED_PLUGINS"):
        monkeypatch.delenv(v, raising=False)
    mod = _reload_plugin(monkeypatch, False)
    assert mod.PLUGIN is None
    assert loader.only_plugin_names() is None and loader.excluded_tool_names() == set()
    names = {p.name for p in loader.discover_plugins()}
    assert "ferret-editor" not in names and "skills" in names and len(names) > 10


def test_editor_profile_is_exactly_skills_and_ferret_tools(monkeypatch):
    _reload_plugin(monkeypatch, True)
    monkeypatch.setenv("CAROLINE_ONLY_PLUGINS", "skills,ferret-editor")
    monkeypatch.setenv("CAROLINE_EXCLUDED_TOOLS", "import_skill,list_skills")
    plugins = loader.discover_plugins()
    assert {p.name for p in plugins} == {"skills", "ferret-editor"}
    tools = {t.name for p in plugins for t in p.tools}
    assert {"use_skill", "read_skill_file"} <= tools and "import_skill" not in tools
    assert "project_write" in tools and "viewer_screenshot" in tools and "gpu_blender" in tools and "gpu_cancel" in tools
    assert {"ue_places", "ue_import", "ue_export", "ue_status", "ue_cancel"} <= tools  # PLAN-PLAYER 10.7.1
    assert not any(("mail" in t or "telegram" in t or "message" in t or "calendar" in t or "doc" in t.replace("api_docs", "")) for t in tools)


def test_page_answer_reaches_the_waiting_call(monkeypatch):
    mod = _reload_plugin(monkeypatch, True)
    from app import session_context
    sent = []

    async def send(frame):
        sent.append(frame)
        mod.resolve(frame["id"], True, {"ok": True, "x": 1})

    async def run():
        token = session_context.set_send(send)
        try:
            tool = next(t for t in mod.PLUGIN.tools if t.name == "project_list")
            return await tool.handler({"dir": None}, None)
        finally:
            session_context._current_send.reset(token)
    out = asyncio.run(run())
    assert sent[0]["type"] == "ferret_tool" and sent[0]["name"] == "project_list" and sent[0]["args"] == {}
    assert '"x": 1' in out["text"]
    assert mod.resolve("nobody", True) is False


def test_an_error_from_the_page_is_an_error_result(monkeypatch):
    mod = _reload_plugin(monkeypatch, True)
    from app import session_context

    async def send(frame):
        mod.resolve(frame["id"], False, None, '"../x" is outside the project')

    async def run():
        token = session_context.set_send(send)
        try:
            return await next(t for t in mod.PLUGIN.tools if t.name == "project_read").handler({"path": "../x"}, None)
        finally:
            session_context._current_send.reset(token)
    out = asyncio.run(run())
    assert out["is_error"] and "outside the project" in out["text"]


def test_codex_native_tools_are_off_only_when_the_host_asks(monkeypatch):
    from app import openai_mode
    monkeypatch.delenv("CAROLINE_NO_NATIVE_TOOLS", raising=False)
    assert openai_mode._native_tool_overrides() == []
    monkeypatch.setenv("CAROLINE_NO_NATIVE_TOOLS", "1")
    off = openai_mode._native_tool_overrides()
    assert "features.shell_tool=false" in off and "features.browser_use=false" in off
    # they are not a sign-in: has_chatgpt_or_device_login looks at _config_overrides() only
    assert openai_mode._config_overrides() == []


def test_product_role_only_when_the_host_gives_it(monkeypatch, tmp_path):
    from app import product_role, small_model_engine
    from app.persona import get_persona
    monkeypatch.delenv("CAROLINE_PRODUCT_ROLE_FILE", raising=False)
    assert product_role.role_clause() is None
    p = get_persona(str(tmp_path))
    assert "personal assistant" in small_model_engine._persona_system_message(p)
    role = tmp_path / "role.md"
    role.write_text("You are a game designer and game programmer.", encoding="utf-8")
    monkeypatch.setenv("CAROLINE_PRODUCT_ROLE_FILE", str(role))
    assert "game designer" in product_role.role_clause() and "not an assistant" in product_role.role_clause()
    sw = small_model_engine._persona_system_message(p)
    assert "game designer" in sw and "personal assistant" not in sw
