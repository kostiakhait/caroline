# Plugins and tools

A plugin is any module directly under `backend-py/app/plugins/` (not starting with `_`, other than
`loader.py`) that exposes a module-level `PLUGIN` object. Plugins are the tool roster of every
engine.

## The data model (`plugins/loader.py`)

```python
PluginTool(name: str, description: str, input_schema: Any, handler: ToolHandler)
Plugin(name: str, tools: list[PluginTool], usage_instructions: str | None = None,
       description: str | None = None, always_loaded: bool = False)
```

* `handler(args, report_progress) -> dict` is async and has the **same signature for every tool**,
  whether it takes a millisecond or minutes. `report_progress(data)` publishes intermediate data a
  status poll can see.
* `input_schema` is whatever `claude_agent_sdk.tool()` accepts (a JSON-schema-shaped dict or a
  type). `to_openai_tool_def()` produces the OpenAI function-call form for the SW cascade from the
  *same* `PluginTool` objects.
* `usage_instructions` is **never** put in the system prompt. It is returned by
  `get_tool_instructions` / `describe_tool` on demand, one text shared by all tools of the plugin.
* `description` is the one line shown in the tool-group list; `always_loaded` keeps the tools
  directly callable (see [ENGINES.md](ENGINES.md)).

## Discovery

`discover_plugins()` imports every plugin module on each `query()` build, so a **new** plugin file
is live within one turn. A module already imported is *not* reloaded (a reload once created new
exception classes that previously-loaded modules no longer caught, and reset module state such as
the funds flag and plugin SW sessions). A plugin that fails to import (a Windows-only dependency on
Linux, for example) is logged as `plugin_import_failed` and skipped; it never costs the other
plugins or the turn.

`build_mcp_servers()` returns the dict of in-process SDK MCP servers for the next turn:
one `caroline-<plugin>` server per directly-registered plugin, `caroline-tools` for the grouped
ones, and `caroline-operations`.

## The operations contract (`operations.py`)

Every tool call goes through `dispatch()` via `wrap_tool()`. No call ever blocks the caller until
completion:

* Handlers that finish within `FAST_PATH_TIMEOUT_S` (0.5 s) return their real result at once
  (`status: "done"`).
* Slower ones become an **operation**: the caller gets an `operation_id` and polls with
  `check_operation_status` (which waits up to `CHECK_OPERATION_WAIT_S`, 60 s) or cancels with
  `stop_operation`. A completed operation nobody collected raises a proactive notification after
  `NOTIFY_GRACE_S` (10 s).
* Every call is logged (call, result, duration) in one place, so plugins never implement polling
  or logging.
* Guarded actions: tools whose name contains `send`, and the financial `purchase_visual_model`,
  are checked by the permission machinery before running (`_guarded_action`). A finished send is
  recorded in `action_ledger` by code, not left to the model to remember.
* Messaging tools map to a reply-watch channel (`REPLY_WATCH_CHANNEL_BY_TOOL`) so a missing reply
  can be followed up.

## Roster by area

~50 plugin files. Grouped:

* **Messaging:** `telegram_plugin`, `whatsapp_plugin`, `signal_plugin`, `discord_plugin`,
  `slack_plugin`, `messenger_plugin` (+ `messenger_search`), `ratatosk_plugin` (+ `ratatosk_api`,
  `ratatosk_own_account`), `email_plugin`, `email_batch_plugin`, `email_credentials`,
  `companion_plugin` (+ `companion_api`, `companion_sms_store`, `companion_contacts_store`),
  `akinakes_bus_plugin`.
* **Memory and notes:** `memory_plugin` (`save_info` / `request_info`, always loaded),
  `notes_plugin` (+ `notes_api`), `event_memory_plugin`, `contacts_plugin`,
  `instruction_memory_plugin`, `owner_profile_plugin`, `dialogue_plugin`. Deprecated and read-only:
  `memory_search_plugin`, `topics_plugin`, `working_memory_plugin`.
* **Desktop automation:** `mouse_plugin`, `keyboard_plugin`, `inspect_plugin`, `chain_plugin`
  (linear GUI-step interpreter), `screenshot_plugin`, `windows_plugin` (registry of Caroline's
  own windows), `window_mouse_plugin`, `window_keyboard_plugin`, `window_screenshot_plugin`.
  Platform back ends are underscore-prefixed (never discovered as plugins): `_win32.py`,
  `_win32_chain.py` on Windows; `_x11_input.py`, `_x11_window.py`, `_x11_capture.py`,
  `_linux_chain.py`, `_linux_app_browser.py` on Linux; `_image_util.py` shared.
* **Browser and web:** `app_browser_plugin` + `app_browser_cdp` (one persistent labelled
  WebView2 / Linux browser window per site, driven over a real CDP connection),
  `web_research_plugin`, `wikipedia_plugin`, `reforce_plugin`.
* **Documents and files:** `files_plugin`, `viewer_plugin`, `office_editor` (embedded OnlyOffice for
  docx/xlsx/pptx/pdf), `document_service_plugin`, `summarize_plugin`, `image_gen_plugin`.
* **Voice:** `speech_plugin`, `voice_api`, `voice_playback_plugin`, `visual_models_plugin`.
* **Account and platform:** `login_plugin`, `sw_api` (the shared SquirrelWisdom v2 client:
  `call_v2`, credentials in `~/.mcp-notes/credentials.json`), `consult_plugin`, `anvar_plugin`,
  `calendar_plugin`, `scheduler_plugin` (reminders), `shell_plugin`, `skills_plugin`
  (`use_skill`, `read_skill_file`), `time_plugin`.

## Adding a tool

1. Pick the plugin that owns the area, or add `app/plugins/<name>_plugin.py` with a module-level
   `PLUGIN = Plugin(name="<name>", tools=[...], usage_instructions=..., description=...)`.
2. Write `async def handler(args, report_progress)` returning a dict. Return promptly or report
   progress; never write your own polling. Return model-facing text bounded by
   `context_cap` helpers: a result must never hand the model an unbounded amount of text.
3. Treat anything another person wrote as data: wrap it with `untrusted_text` before it reaches
   the model.
4. If the tool needs a SquirrelWisdom login, call `sw_gate.require_sw_or_prompt()` (first refusal
   opens the native login window, later ones only say so). Credentials must never pass through
   the model.
5. Tools that send something to a third party or spend money must contain `send` in their name or
   be added to `_FINANCIAL_TOOLS`, so the permission machinery covers them.
6. Never hard-code another plugin's or server's tool names; derive the roster from
   `discover_plugins()` (the code was bitten by this more than once).
7. Add a `test_*.py` next to the existing ones under `backend-py/`.

## SquirrelWisdom-gated features

`sw_gate.SW_GATED_FEATURES`: `consult`, `ratatosk-owner`, `office-editor`, `notes`, `email`,
`memory`, `speech`. Their requests go through `sw_api.call_v2` /`reforce_v2.py`; service keys are
read from the gitignored `app/sw_service_keys_local.py` (template: `sw_service_keys_example.py`).
Never commit that file.
