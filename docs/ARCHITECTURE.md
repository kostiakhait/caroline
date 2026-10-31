# Architecture

Caroline is a desktop AI secretary: a tray application with a chat window that can use e-mail,
notes, messengers, the browser and the desktop through tools, and answers through one of three
sources (the user's Claude account, the user's ChatGPT/Codex account, or a pay-as-you-go
SquirrelWisdom wallet).

## Layers

```
 +-----------------------------+      +-----------------------------------------+
 | Shell (one per platform)    |      | Backing service (separate repository)   |
 |  Windows: WPF + WebView2    |      |  SquirrelWisdom / Camerlengo ("reforce")|
 |  Linux:   GTK + WebKit2     |      |  Notes, e-mail, voice, consult, wallet, |
 |  chat UI = wwwroot/chat.*   |      |  memory microagents, model proxy        |
 +--------------+--------------+      +--------------------^--------------------+
                | ws://127.0.0.1:48765/?tab=<id>                | HTTPS (v2 API)
                | http://127.0.0.1:48765/api/*                  |
 +--------------v---------------------------------------------+ |
 | backend-py  (FastAPI, one process)                          | |
 |  main.py  -> ChatSession per tab -> engine                  +-+
 |  plugins (tools) / microagents / channels / schedulers      |
 +--------+----------------------+-----------------------------+
          |                      |
 +--------v--------+   +---------v----------+   +------------------------------+
 | claude CLI      |   | codex-app-server   |   | sidecars: WhatsApp (Baileys),|
 | (Agent SDK)     |   | (JSON-RPC / stdio) |   | Messenger (fca), signal-cli  |
 +-----------------+   +--------------------+   +------------------------------+
```

1. **Shell.** Owns the window, the tray icon, the global hotkey, autostart, the update prompt and
   the secondary windows (document viewer, embedded browser, Visual Mode, wizard). It hosts the
   same chat page on both platforms: `Windows/Caroline/wwwroot/chat.html` + `chat.js`. The shell
   spawns the supervisor, which spawns the backend. See [SHELLS_AND_INSTALLER.md](SHELLS_AND_INSTALLER.md).
2. **Supervisor** (`backend-py/supervisor.py`, port 48766). A standalone Python process that starts,
   health-checks and restarts the backend with a rate limit. Deliberately independent of the `app`
   package so a broken backend import cannot also break the thing that restarts it.
3. **Backend** (`backend-py/`, port 48765). The only place with logic: sessions, engines, tools,
   memory, channels. Started through `run_server.py` (not `-m app.main`; the embeddable Python's
   `._pth` file removes the working directory from `sys.path`).
4. **Engines.** Behind a `ChatSession` the model is run by (a) the Claude CLI through the vendored
   `claude_agent_sdk`, (b) one `codex-app-server` per tab, or (c) the in-process SW cascade
   (`small_model_engine.py`) that calls models through the SquirrelWisdom proxy.
5. **Backing service.** Notes, mail, voice, the wallet and the memory microagents live in a
   separate repository and are reached only over its versioned HTTP API
   (`app/plugins/sw_api.py`, `app/reforce_v2.py`). Caroline never imports its code (a few files are
   *vendored stubs*, see `backend-py/camerlengo-vendor-stubs`).
6. **Installer** (`Windows/CarolineInstaller`). A no-admin bootstrapper that downloads
   `Caroline.zip` and provisions the isolated runtimes.

## Portability rule

Nothing at run time may reference a path outside the install directory, the runtime directory
(`%LocalAppData%\Caroline\runtime\`) or the per-user workspace
(`%LocalAppData%\Caroline\workspace\`, override `CAROLINE_WORKSPACE_DIR`). Runtime locations are
handed to the backend through `CAROLINE_*` environment variables (see [CONFIGURATION.md](CONFIGURATION.md)).

## Repository map

| Path | Content |
| --- | --- |
| `backend-py/app/` | The backend package (about 110 modules at the top level) |
| `backend-py/app/plugins/` | Tool plugins, ~50 `*_plugin.py` files plus helpers |
| `backend-py/app/microagents/` | Client-executed microagents (`reply:` domain and the SW cascade's advisors) |
| `backend-py/app/engines/` | `AgentEngine` contract, Claude and Codex engines, Codex JSON-RPC client |
| `backend-py/app/chat_session_mixins/` | The subsystems composed into `ChatSession` |
| `backend-py/skills-src/` | Default skills seeded into the workspace on every start |
| `backend-py/camerlengo-vendor-stubs/` | Vendored client-side copies of server helper modules (`AI.py`, `Microagent.py`, ...) |
| `backend-py/evals/` | Evaluation scripts for narrator, verifier, STT sanity and others |
| `backend-py/test_*.py` | Unit tests (pytest), about 49 files |
| `Windows/Caroline/` | WPF shell and the shared web UI (`wwwroot/`) |
| `Windows/Caroline.NativeHost/` | Native helper host (framework-dependent .NET) |
| `Windows/Caroline.Linux/` | Linux counterpart of the native pieces |
| `Windows/CarolineInstaller/` | The bootstrapper |
| `linux-shell/` | GTK shell, AppImage scripts, update checker, Visual Mode window |
| `companion-apps/` | `whatsapp-sidecar`, `messenger-sidecar` (Node) and the `android` companion |
| `vendor/XcfaRenderer/` | The talking-head renderer used by Visual Mode (C#) |
| `art/` | Icons, splash, banners, persona looks, talking-head models (large, not in git) |
| `tools/` | Release and edition tooling (`release.py`, `edition_*.py`) |
| `runtime/python/` | The Python runtime that is bundled for packaging |
| `Makefile`, `BUILD.md`, `deploy.mk` | Build pipeline (`deploy.mk` is gitignored, maintainer-only) |

## One message, end to end

1. The user types or speaks in the chat page. `chat.js` sends `{type:"user_message", ...}` over the
   tab's WebSocket (`ws://127.0.0.1:48765/?tab=<id>`).
2. `main.py`'s WebSocket handler saves the message to `pending-turn-<tab>.json` (durability), then
   passes it to `submit_or_try_small_model` (a 5 second merge window joins quick follow-ups).
3. `ChatSession.submit()` (`chat_session_mixins/turn_submission.py`) is the one choke point for
   *every* turn: a real user message, an internal nudge, a reminder, a replay after restart.
   It runs the memory advisor, picks the engine and the start rung, and hands the text to it.
4. The engine runs the model. The model calls tools; every tool call goes through
   `operations.dispatch()` so it gets the same start/status/stop contract, logging and permission
   checks.
5. Engine messages come back as `claude_agent_sdk`-shaped objects (the Codex and SW engines adapt
   to this shape). `wire.py` converts them to the JSON the page already renders.
6. Before an answer reaches the window it passes the reply filters (`reply.review`, translation,
   delivery-completeness, the ladder's verifier). `dialogue_log.py` records what the user was
   actually shown.
7. When the turn ends the pending-turn file is removed; background work (narration, memory
   replay, compaction, the end-of-turn delivery report) runs on supervised loops.

## Design principles that recur in the code

* **Microagents decide, code supplies facts.** Fuzzy judgments (does this reply leak plumbing, did
  the user just say yes, is this contact the same person) go to a small, single-purpose model call
  with a strict output schema; they are never hand-written keyword rules. Plain code decides only
  what is mechanical (`permission_policy.py`).
* **The model gets less than everything.** Tools are offered as groups (`tool_groups.py`), usage
  instructions are fetched on demand (`get_tool_instructions`), standing rules are kept short.
* **Nothing important lives only in a process.** Pending turns, resume ids, save queues, schedules,
  agents and the action ledger are files in the workspace so a crash or an update loses nothing.
* **Every background loop is supervised** (`task_supervisor.supervise`), because a loop that dies
  silently was the cause of several incidents.
* **One update rule, decided once** (`update_activity.py`), asked by every platform's shell.
