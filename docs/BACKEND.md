# Backend (`backend-py`)

The live backend is a FastAPI application. This document is the map: entry points, the network
surface, the control-op protocol, and where each concern lives in `backend-py/app/`.

## Entry points and processes

| File | Role |
| --- | --- |
| `run_server.py` | Production entry. Puts its own directory on `sys.path`, imports `app.main`, seeds the default skills (`skills_seed.seed_skills`) and runs uvicorn on `PORT` and `REMOTE_PORT`. Used instead of `python -m app.main` because the embeddable Python's `._pth` file replaces `sys.path`. |
| `supervisor.py` | Standalone supervisor (HTTP control API on `CAROLINE_SUPERVISOR_PORT`, default 48766). Spawns and owns `run_server.py`, polls health every 30 s, restarts after 2 bad checks, at most 5 restarts per 10 minutes, 10 minutes of start-up grace, treats a turn stuck for 5 minutes as unhealthy. Imports only `app.logging_setup`, never the heavy backend. Both the WPF and the Linux shell drive it over HTTP. |
| `app/main.py` | The FastAPI app: HTTP routes, the WebSocket handler, the control-op dispatcher, background loops (reminders, channels, update checks) and per-tab session registry. About 2300 lines; the largest single module together with `chat_session_shared.py` and `small_model_engine.py`. |
| `app/dialogue_refresh_worker.py` | Run as a *separate process* (`python dialogue_refresh_worker.py <session|-> <tab> <workspace>`) to rebuild a tab's "last 24 hours of dialogue" file, because the transcript walk used to stall the event loop. |

## Ports and network exposure

| Port | Default | Bound to | Purpose |
| --- | --- | --- | --- |
| `CAROLINE_PORT` | 48765 | 127.0.0.1 | Backend HTTP + WebSocket used by the shell |
| `CAROLINE_SUPERVISOR_PORT` | 48766 | 127.0.0.1 | Supervisor control API |
| `CAROLINE_REMOTE_PORT` | 48768 | 0.0.0.0 | Optional remote access (`CAROLINE_REMOTE_ACCESS`); requests arriving on this port go through a gate in `main.py` |

`app/origin_guard.py` rejects cross-origin browser requests to the loopback APIs (a malicious web
page can reach `127.0.0.1` even though a remote attacker cannot), and `main.py` additionally
requires loopback requests to address `127.0.0.1` or `localhost`. `CAROLINE_FRONTEND_ORIGIN`
sets the allowed web origin. Outbound HTTPS in the backend shares one TLS context (`http_tls.py`)
because building one per `httpx` client blocked the event loop for seconds on Windows.

## HTTP API (`main.py`)

| Route | Purpose |
| --- | --- |
| `GET /api/status` | Backend/tab status for the shell and the supervisor |
| `POST /api/message` | Inject a message into a tab from outside the page |
| `GET /api/local-file` | Serve a local file to the chat page (loopback only) |
| `GET /api/mode`, `POST /api/mode` | Per-tab answer source (Claude / SW / OpenAI) |
| `GET/POST /api/continuous-dialogue` | The continuous voice dialogue switch |
| `GET /api/openai`, `POST /api/openai/login`, `/logout`, `/cancel` | OpenAI (Codex) account state and sign-in |
| `GET /api/update/status`, `POST /api/update/check`, `/confirm`, `/installing`, `GET /api/update/activity` | The shared update client; `activity` answers "is Caroline in use?" for every platform's shell |
| `GET /api/companion/devices`, `/sms/threads`, `/sms/thread/{id}`, `/sms/search`, `/contacts`, `/contacts/search`, `/logs` | Data from the paired Android phone |
| `GET /wizard`, `GET /api/wizard/state`, `POST /api/wizard/login`, `/claude-login`, `/openai-login`, `/mailbox`, `/mailbox/remove`, `/answer` | The start-up wizard (`wizard.py`, page `app/assets/wizard.html`) |
| `POST /api/control` | A control op over HTTP (same dispatcher as the WebSocket's `control_request`) |
| `WS /?tab=<id>` | One connection per tab; see below |

## WebSocket protocol

Each tab's page opens `ws://127.0.0.1:48765/?tab=<tabId>`. Inbound frames the handler understands
(by `type`): `user_message` (text, attachments, voice flag), `interrupt` (the Stop button) and
`control_request` (with an `op`). Outbound frames are the Claude CLI's message JSON shape produced
by `wire.py` (`system/init`, `assistant` blocks for text / tool_use / thinking, `result`) plus
Caroline's own frames (status, connection state, narration, editor/viewer opens, login prompts).
`PRIMARY_TAB_ID` is `"1"`; special tab ids are `ratatosk`, `api` (the companion API) and
per-chain ids for the Akinakes bus.

### Control ops

Handled by `handle_control_request` in `main.py` (op → purpose):

* **Account / mode:** `auth_status`, `auth_login`, `auth_logout`, `own_anthropic_key_get/set`,
  `openai_status`, `openai_login`, `openai_logout`, `openai_key_get/set`, `sw_status`, `sw_logout`,
  `login_submit`, `open_login_from_settings`, `open_payment_from_settings`, `chat_mode_get/set`,
  `mode_get`, `model_override_get/set`.
* **Tabs and history:** `tab_list_set`, `clear_tab`, `get_history`, `expand_dehydrated_ref`,
  `visible_transcript_set`, `transcript_reconcile`, `find_attachment`, `force_restart`,
  `shutdown_sync`, `client_diag`.
* **Persona and voice:** `persona_get/set/reset`, `visual_mode_get/set`,
  `visual_model_download_done`, `continuous_dialogue_get/set`, `local_stt_get/set`, `speech_state`,
  `stt`, `tts`.
* **Tools and files:** `mcp_list/add/remove`, `open_file`, `editor_result`.
* **Channels:** `telegram_channel_status`, `whatsapp_channel_status`, `signal_channel_status`,
  `slack_channel_status`, `discord_channel_status`, `messenger_channel_status`,
  `ratatosk_channel_status`, `ratatosk_status_get`, `ratatosk_stop_session`,
  `ratatosk_own_account_register`.

## Module map (`backend-py/app/`)

**Session core** — `chat_session.py` (only `__init__` and the class declaration), `chat_session_shared.py`
(constants and helpers), `chat_session_mixins/*` (one subsystem per file), `engines/*`,
`small_model_engine.py` (SW cascade), `claude_ladder.py`, `rung_advice.py`, `failure_classification.py`,
`loop_watchdog.py`, `process_activity.py`, `process_kill.py`, `task_supervisor.py`, `durability.py`,
`pseudo_tab.py`. See [CHAT_SESSION.md](CHAT_SESSION.md) and [ENGINES.md](ENGINES.md).

**Prompts and policy** — `policies.py` (always-on instructions, calibrated wording),
`persona.py`, `owner_profile.py`, `instruction_memory.py`, `language_hint.py`,
`permission_policy.py`, `untrusted_text.py`, `agent_definitions.py`, `skills.py`, `skills_seed.py`.

**Memory and state** — `memory_*` modules, `working_memory.py`, `event_memory.py`,
`action_ledger.py`, `action_registry.py`, `vault_backup.py`, `settings_mirror.py`,
`dialogue_log.py`, `transcript_journal.py`, `transcript_rotate.py`, `archive_*`, `history.py`,
`sw_history.py`. See [MEMORY_AND_STATE.md](MEMORY_AND_STATE.md).

**Context safety** — `compaction*.py`, `context_cap.py`, `pdf_pages.py`, `file_facts.py`.

**Accounts and billing** — `subscription_mode.py`, `openai_mode.py`, `openai_account.py`,
`account_state.py` (cached account facts so Settings does not wait), `login_api.py`, `sw_gate.py`,
`external_session.py` (host-provided SquirrelWisdom session), `model_billing.py`,
`model_key_provisioning.py`, `sw_service_keys_*.py`, `reforce_v2.py`.

**Voice** — `local_edge_tts.py`, `edge_tts_token_patch.py`, `local_stt.py`, `stt_sanity.py`,
`stt_adaptation.py`, `speech_ahead.py`, `filler_phrases.py`, `continuous_dialogue.py`, `visual_mode.py`.

**Channels** — `*_channel.py` for Telegram, WhatsApp, Signal, Slack, Discord, Messenger, Ratatosk,
Akinakes; `sidecar_process.py`, `secret_store.py`. See [MESSENGERS.md](MESSENGERS.md).

**Web** — `web_browser.py` (own headless Chromium), `web_research.py` (agentic search loop).

**Updates** — `release_manifest.py`, `update_service.py`, `update_activity.py`, `update_cli.py`,
`overlay_install.py`. See [BUILD_AND_RELEASE.md](BUILD_AND_RELEASE.md).

**Plumbing** — `logging_setup.py` (JSON-lines to stdout; the shell pipes it into `caroline.log`),
`wire.py`, `session_context.py` (contextvars so plugin handlers can reach the calling session),
`operations.py`, `window_registry.py`, `workspace_dir.py`, `win_subprocess_patch.py`
(CREATE_NO_WINDOW for the SDK's CLI spawn), `cli_control.py` (one-shot `claude` CLI calls for
auth and MCP management).

## Logging

Every record is one JSON object per line: timestamp, component, event, plus caller fields. The
WPF shell writes the backend's stdout into `%LOCALAPPDATA%\Caroline\caroline.log` together with
its own logs. A healthy start ends with `backend listening on ws://127.0.0.1:48765`
(see `BUILD.md` for the verification checklist). `loop_watchdog.py` logs the stack of whatever
blocks the event loop for longer than its threshold.
