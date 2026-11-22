# Messengers, companions and the agent bus

Every messenger follows one pattern, so a new one is mostly a copy:

* `app/<service>_channel.py` owns the always-running connection: login, reconnects, session state
  encrypted at rest (`secret_store.py`; DPAPI on Windows), and pushing a *proactive note* into the
  session when something arrives. It runs under `task_supervisor.supervise()` with a heartbeat.
* `app/plugins/<service>_plugin.py` is a thin per-turn tool layer that imports functions from the
  channel module and exposes them to the model.
* Where the service needs a non-Python runtime, a **sidecar** process is driven over stdio by
  `sidecar_process.py` (newline-delimited JSON; blocking pipe I/O on plain threads bridged into
  asyncio, because `asyncio.create_subprocess_exec` is unreliable under the Windows selector loop).

Most messengers have **two accounts**: `self` (Caroline's own, she may read and reply as herself)
and `owner` (the human owner's, linked so Caroline can read it and act for the owner, which needs
explicit permission).

| Service | Channel module | Transport | Notes |
| --- | --- | --- | --- |
| Telegram | `telegram_channel.py` | Telethon, MTProto *client* API (not a bot) | self + owner |
| WhatsApp | `whatsapp_channel.py` | Node sidecar `companion-apps/whatsapp-sidecar` (Baileys, unofficial WhatsApp Web protocol) | QR login (`qrcode`, Pillow); self + owner |
| Signal | `signal_channel.py` | `signal-cli` as a JSON-RPC daemon over a local TCP socket (not stdio); needs a JRE | self + owner |
| Slack | `slack_channel.py` | Socket Mode | First messenger built; proved the whole pattern |
| Discord | `discord_channel.py` | discord.py gateway, **bot only** | A personal-account self-bot would violate Discord's ToS; sees only servers/DMs it is invited to |
| Facebook Messenger | `messenger_channel.py` | Node sidecar `companion-apps/messenger-sidecar` (`fca-unofficial`) | Personal inbox; no official API exists |
| Ratatosk | `ratatosk_channel.py`, `plugins/ratatosk_*.py` | SquirrelWisdom's own messenger | Watches both identities; Caroline's own account is auto-generated (`ratatosk_own_account.py`) |
| Akinakes bus | `akinakes_channel.py`, `plugins/akinakes_bus_plugin.py` | HTTP long-poll to the agent bus | See below |

Design record: `MESSENGER_INTEGRATIONS_PLAN.md`.

## Incoming messages are data

An incoming message becomes a proactive note handed to a model that has tools that run commands
and send files. `untrusted_text.py` therefore wraps the sender's name and text in markers the
sender cannot forge or close, so it can never pass for the owner or for Caroline's own notes.
Sending on someone's behalf passes the permission machinery (see [MICROAGENTS.md](MICROAGENTS.md)).

## SMS and contacts: the Android companion

SMS is **not** sent through a per-user SMTP account (removed 2026-09-27). A paired Android phone
sends and reads real SMS through its own SIM.

* `plugins/companion_api.py` talks to the phone through the service's session-scoped variable
  commands (`var:getMine`, `var:setMine`, `var:getAllMine`, `var:deleteMine`); the server prefixes
  every path with the caller's own namespace, so the backend never sees the login.
* Every companion operation is two-phase and transactional: phase 1 polls the request path every
  60 s until the phone writes `status: "accepted"`; the operation never gives up on its own; only
  an explicit Stop ends it.
* `companion_plugin.py` exposes the tools; `companion_sms_store.py` and `companion_contacts_store.py`
  cache threads and contacts; the HTTP routes `/api/companion/*` expose them to the shell.
* `companion-apps/android` is a Kotlin / Jetpack Compose project (AGP 8.5.2, Kotlin 2.0.21, minSdk
  26). Its own `README.md` describes it as a skeleton: working login and tab/chat read views; **no**
  local database, no sending, no SMS/contacts integration yet. Check that README for the current
  state before relying on it.

## Akinakes agent bus

When `BUS_URL` and `AGENT_UID` are set, a Caroline process *is* one agent on the Akinakes bus:
`akinakes_channel.py` registers, heartbeats every 30 s and long-polls `POST /messages/poll`
(5 s, 10 messages). A delivered task becomes a proactive turn on a headless `ChatSession`, one per
`chainId`; the model answers through `akinakes_publish_result`, and a turn that ends with nothing
published produces `system.skill_blocked`. `akinakes_org_settings.py` applies the organization's
settings (SquirrelWisdom account, optional Anthropic key, name) from the auth service's internal
endpoint before the agent works.

## Embedded browser and web research

* `plugins/app_browser_plugin.py` + `app_browser_cdp.py`: one persistent, labelled WebView2
  window per site driven over CDP (Windows); `_linux_app_browser.py` is the Linux counterpart
  (Playwright). `window_registry.py` tracks Caroline's own open windows so she can close "the rest".
* `web_browser.py` is a *separate* headless Chromium (own empty profile, no window, bundled under
  `dist/browsers`) used by `web_research.py`, the agentic search loop: plan → search → pick →
  read + extract → judge → answer, event-driven (pages are extracted as they arrive).
