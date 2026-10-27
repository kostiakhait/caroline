# Caroline — project overview

A standalone Windows desktop AI secretary: email, notes, browser/Windows automation, messaging,
phone-companion SMS, scheduling — driven by natural-language chat, as a tray app with a global
hotkey and a chat window, no terminal or IDE required. A tab can answer through the user's own
Claude account (Claude Agent SDK), the user's own ChatGPT/Codex account, or pay-as-you-go through
a SquirrelWisdom wallet — same assistant, same tools, same chat window, switchable per tab. Built
with a persistent identity (persona), voice, and a growing plugin + server-side microagent tool
set.

This file is the detailed, code-derived companion to `README.md` (portability/build basics). It
describes what's actually implemented, not the roadmap — rewritten 2026-10-11 to describe the
current Python backend (`backend-py/`), which replaced the original Node/TypeScript backend
(`backend/`) as the live engine. That Node tree was removed on 2026-10-24: its skills moved to
`backend-py/skills-src/`, and its MCP servers and native helper exes were replaced by Python
plugins.

## Four layers

- **`Windows/Caroline/`** — WPF shell (C#). Tray icon, global hotkey (default Ctrl+Alt+C), a
  WebView2-hosted chat UI (`wwwroot/chat.html`/`chat.js`), a Settings panel, and several
  secondary windows (`DocumentViewerWindow`, `AppBrowserWindow`, `VisualModeWindow`,
  `SplashWindow`). Spawns and supervises the backend sidecar process (`Native/SupervisorClient.cs`
  talks to `backend-py/supervisor.py`).
- **`backend-py/`** — the live Python backend (FastAPI). `app/chat_session.py`'s `ChatSession` is
  the per-tab core; `app/plugins/` is the tool roster; `app/*_channel.py` own the always-running
  messenger connections. See "Chat session core" and "Plugins & tools" below.
- **`backend-py/skills-src/`** — the default skills, seeded into the workspace on every start
  (`app/skills_seed.py`). Every tool, desktop automation included (`app/plugins/_win32*.py` on
  Windows, `_x11_*.py`/`_linux_chain.py` on Linux), lives in `backend-py`; there is no Node backend,
  MCP server bundle or native helper exe any more (removed 2026-10-24).
- **The backing SquirrelWisdom/Camerlengo service** (a separate repository, `reforce`) — every
  SW-gated feature (Notes/memory, email, consult, voice, pay-as-you-go chat billing) and the
  cross-product **microagent layer** (memory/documents/contacts/search — see "Memory and
  microagents" below) live here, not in Caroline's own process; Caroline's own `reply:`-domain
  microagents execute client-side instead, reached through this same service's one generic
  raw-model-call command. Caroline talks to it exclusively over its versioned HTTP API
  (`app/plugins/sw_api.py`'s `call_v2`/`CAROLINE_SW_KEY`), never by importing its code.
- **`Windows/CarolineInstaller/`** — a separate, no-admin-rights WPF bootstrapper that downloads
  `Caroline.zip` from `downloader.multi-portal.org`, installs isolated Node/Python runtimes under
  `%LocalAppData%\Caroline\runtime\` (never touches system PATH), and Chromium for Playwright.

Caroline is **portable by design**: nothing at runtime references any repo outside this one
(the backing SquirrelWisdom service is reached over HTTP, like any other cloud dependency, not
checked out locally). Everything ships inside the install directory or lives in the per-user
workspace (`%LOCALAPPDATA%\Caroline\workspace\`).

## Chat session core (`chat_session.py` + engines)

`ChatSession` (`backend-py/app/chat_session.py`, one instance per open tab) is the Python port of
the original Node backend's `runLoop` resilience layer — hang detection, a bounded restart
budget, dehydration/compaction, a failure-classification gauntlet, a continuity-archive pointer,
and durability across a full app restart, ported "near-verbatim" per its own module docstring,
which calls it out as the riskiest single piece of that whole migration. As of a 2026-10-17 split,
`chat_session.py` itself only holds `__init__` and composes the class from
`app/chat_session_mixins/*` (shared imports/helpers factored into `app/chat_session_shared.py`) —
`submit()` specifically lives in `chat_session_mixins/turn_submission.py`. It's still the single
choke point every turn goes through — a real user message, an internal/proactive nudge, or a
replay after a restart all funnel through it, and it logs every call.

A turn runs on one of two engines behind a shared `AgentEngine` interface
(`app/engines/base.py`): `ClaudeEngine` wraps the vendored `claude_agent_sdk`'s `ClaudeSDKClient`;
`CodexEngine` runs one `codex-app-server` process per tab (JSON-RPC over stdio) and adapts its
events into the same message shape, so wire format, failure classification, status, and history
are shared code regardless of which one answered. Caroline's own plugin tools are handed to Codex
as `dynamicTools` over an in-process MCP bridge, so a tool behaves identically under either engine.
A third, older path — `app/small_model_engine.py`'s `run_small_model_turn` — is ChatSession's own
pay-as-you-go primary path: a cheap OpenRouter-backed model (billed through a SquirrelWisdom
wallet, not through the AgentEngine abstraction) running its own tool-calling loop, used when a
tab is on the SquirrelWisdom chat source rather than Claude or OpenAI.

Every long-running background loop a `ChatSession` owns (the watchdog, narration, dialogue
refresh) runs through `app/task_supervisor.py`'s `supervise()`, added after a real incident where
an uncaught exception silently killed a background loop with no log line: `supervise()` logs any
non-cancellation exception in full and restarts the loop automatically. A 2026-10-11 addition adds
an opt-in heartbeat (`heartbeat_timeout_s` + a `touch()` the loop body calls to prove liveness) for
loops that legitimately run forever (a live messenger connection) where elapsed time alone can't
tell "healthy" from "stuck."

**Resuming an unfinished request** (`app/durability.py`, `main.py`'s WS handler). A real user
message is saved to `pending-turn-<tab>.json` the moment it is *received* — including while it
still waits out the 5-second merge window in `submit_or_try_small_model` — and removed only when its
turn finishes or the user presses Stop. Closing the window disposes the tab's session and cancels the
running turn, but deliberately leaves the file: the next connect of that tab finds it and starts the
resume turn (`resuming_unfinished_turn` in the log). The once-per-process guard
(`_resumed_unfinished_turn_for_tab`) is re-armed on every disconnect, so this works when the backend
outlived the window (an app update, a webview reload), not only after a full process restart. History
recovery (`get_history`, `history.py`) drops Caroline's internal turns (`SYNTHETIC_TURN_MARKER`), so a
startup nudge never shows up as the user's own bubble.

## Paying for chat: three sources, one assistant

`app/subscription_mode.py` resolves the Claude path: per explicit policy, Claude is reached
**only** through the user's own account — their own `claude auth login` CLI session, or a pasted
Anthropic API key (`subscription.json`) — never proxied through SquirrelWisdom. SquirrelWisdom is
not a Claude backend; it's a client for Notes/email/Ratatosk/voice/consult, and separately bills
the small-model path pay-as-you-go against a PIA wallet balance (`chat_mode_eligible()` requires
a SquirrelWisdom login with a positive balance — no Claude subscription needed: SW is meant to
become the primary and eventually only path, and an sw-mode tab never builds a Claude session). `app/openai_mode.py` +
`app/openai_account.py` resolve the third source the same way as Claude: the user's own
ChatGPT/Codex sign-in, or a separately pasted `OPENAI_API_KEY`. A tab switches between all three
with the `Claude / SW / OpenAI` header control; nothing migrates, each source keeps its own resume
state (`tab-session-<tab>.json`).

## Identity, voice, and persona

`app/persona.py` owns Caroline's own identity — curated Caroline/Peter profiles, custom personas,
name/gender/age/bio/photos — and `persona_system_prompt_append()` enforces self-reference and
grammatical-gender agreement in the system prompt (added after a confirmed live bug: without it,
Russian self-reference drifted into the wrong grammatical gender). `app/owner_profile.py` is the
complementary piece for the *user's own* side: a deliberately small, always-injected cache
(name/gender/age) so the model can address the owner correctly — not the user's full profile, which
lives in a "Caroline:Profile" Notes folder instead; kept small after a past incident where inlining
a full Notes profile into every system prompt overflowed Windows' `CreateProcess` command-line
length limit (~32K characters) and broke every tab at once.

## Plugins & tools (`app/plugins/`)

A plugin is any module directly under `app/plugins/` exposing a module-level `PLUGIN` object (a
`Plugin` dataclass: `name`, a list of `PluginTool`s, optional `usage_instructions`). Every tool is
a plain async `handler(args, report_progress)` — `app/plugins/loader.py`'s `wrap_tool()` routes
every call through `app/operations.py`'s `dispatch()`, giving every tool the same start/status/stop
contract (fast calls return their real result immediately; slow ones return an `operation_id` to
poll) and the same call/result/duration logging, so individual plugins never implement polling or
logging themselves. `discover_plugins()` reloads every plugin module fresh on each turn's `query()`
build, so a plugin edit is live within one turn, no restart. `usage_instructions` is never injected
into the system prompt automatically — it's fetched on demand through the shared
`get_tool_instructions` tool, alongside `check_operation_status`/`stop_operation`/
`describe_own_backend` (`caroline-operations`, registered once at the engine level).

Rough roster by category:

- **Messaging** — `telegram_plugin.py`, `whatsapp_plugin.py`, `signal_plugin.py`,
  `discord_plugin.py`, `slack_plugin.py` (thin tool layers, see "Messenger channels" below);
  `ratatosk_plugin.py` (SquirrelWisdom's own messenger, as the owner's account or Caroline's own
  separate auto-generated account); `email_plugin.py` + `email_batch_plugin.py`; `companion_plugin.py`
  + `companion_api.py` (real SMS send/read and phone contacts from a paired Android phone).
- **Memory & notes** — `memory_plugin.py` (current `save_info`/`request_info`, dispatched to the
  server-side microagents — see below); `notes_plugin.py` (direct Notes CRUD); `event_memory_plugin.py`
  (a durable record of things Caroline arranged); `contacts_plugin.py`. `memory_search_plugin.py`,
  `topics_plugin.py`, and `working_memory_plugin.py` are deprecated, superseded by `memory_plugin.py`
  (`app/deprecated_memory.py`), kept read-only for continuity. `instruction_memory_plugin.py`
  (`app/instruction_memory.py`) keeps the owner's standing rules locally and puts them into every
  system prompt of both engines; entries are ranked by priority, then use count (LFU), then recency,
  and the lowest rank is evicted when the list is full.
- **Automation** — `mouse_plugin.py`, `keyboard_plugin.py`, `inspect_plugin.py`, `chain_plugin.py`
  (a linear GUI-automation step interpreter), `screenshot_plugin.py`, `windows_plugin.py` (a
  cross-plugin registry of Caroline's own open windows/tabs) and their window-targeted twins, plus
  Linux ports (`_x11_input.py`, `_x11_window.py`, excluded from discovery by their leading
  underscore).
- **Browser** — `app_browser_plugin.py` + `app_browser_cdp.py`: one persistent, labeled WebView2
  window per site, driven over a real CDP connection, not simulated input.
- **Voice** — `voice_api.py` (TTS/STT/translation/reply-review/permission-guard calls — see below),
  `speech_plugin.py`, `voice_playback_plugin.py` (including Visual Mode talking-head playback),
  `visual_models_plugin.py` (buying/activating extra Visual Mode talking-head models through the
  piaster wallet).
- **Misc** — `login_plugin.py`/`sw_api.py` (SquirrelWisdom login + shared v2 client),
  `consult_plugin.py` (a GPT-5-class second opinion for high-stakes wording), `summarize_plugin.py`
  (one tool, `summarize_content`, over the `content.summarize` microagent), `files_plugin.py`,
  `shell_plugin.py`, `scheduler_plugin.py`, `viewer_plugin.py`/`office_editor.py` (embedded
  OnlyOffice docx/xlsx/pptx/pdf editing), `reforce_plugin.py` (Wikipedia search/fact extraction),
  `image_gen_plugin.py`, `anvar_plugin.py` (Caroline's own access to Anvar, Camerlengo's
  email-agent manager), `owner_profile_plugin.py` (tools over `app/owner_profile.py`'s cache —
  see "Identity, voice, and persona" above), `time_plugin.py` (a trivial Phase-1 proof-of-life
  plugin, kept as-is).

## Messenger channels vs. plugins

For every messenger (Telegram/WhatsApp/Signal/Discord/Slack, plus Ratatosk — SquirrelWisdom's own
messenger, the sixth in the identical pattern), an `app/*_channel.py` module owns the always-running
connection/session — login, reconnects, the two-account model where relevant (Caroline's own "self"
account and the human owner's "owner" account), and encrypts session state at rest via
`app/secret_store.py`. The matching `app/plugins/*_plugin.py` is a thin, per-turn tool layer that
imports functions directly from its channel module and exposes them to the model — consistently the
same split across all six integrations.

## SquirrelWisdom-gated features and login

`app/sw_gate.py`'s `require_sw_or_prompt()` is the shared gate: on a gated tool's first refusal it
auto-opens the native login window; on repeat refusals it just says so without reopening.
Credentials never pass through the model's context — the native login form posts straight to the
backend over its own WebSocket control op (`login_submit`), persisted via `app/plugins/sw_api.py`'s
`save_credentials()` to `~/.mcp-notes/credentials.json`, shared with every other SW-backed tool on
the machine. Gated feature families (`SW_GATED_FEATURES`): `consult`, `ratatosk-owner`,
`office-editor`, `notes`, `email`, `memory` (`save_info`/`request_info`/`save_document`), `speech`.

SMS is no longer a per-user SMTP2GO account (that path was removed 2026-09-27) — it now sends and
reads real SMS through a paired Android companion phone's own SIM instead (`companion_plugin.py`).

## Memory and microagents (the backing SquirrelWisdom/Camerlengo service)

Several of Caroline's features don't reason with one big prompt — they hand a narrow, specific
judgment call to a **microagent**: a small, separately-invoked model call with a strict input
schema, a strict output schema, and exactly one job.

**Why a microagent instead of a bigger prompt or hand-written code.** Two things, confirmed
independently, by different incidents: (1) a system-prompt instruction is a request, not a
guarantee — it can be perfectly clear and still get missed a few turns later; a microagent is an
independent check that doesn't share the main model's blind spot, because it isn't the same call.
(2) Wherever a decision is genuinely fuzzy (does this name match that contact, does this reply
leak something it shouldn't, did the user's reply actually mean yes) — that decision is always
left to a model, never to hand-written string/keyword comparison, which quietly stops matching the
day something is phrased a little differently.

**Where a microagent runs — the universal-server boundary (2026-10-18).** Camerlengo is a
*universal* server, shared by every product built on it (Caroline, BeautySqrl, Ratatosk, …) —
nothing specific to one product belongs living there. Memory, document indexing, the address book
and fuzzy search (`apps/microagents/FactMemory.py`, `TopicMemory.py`, `DocumentMemory.py`,
`AddressBook.py`, `Search.py` in `reforce`) are genuinely cross-product infrastructure and stay
server-executed for exactly that reason. Everything that's Caroline's own judgment call — the
`reply:` domain covered below — executes **in Caroline's own process** instead, reached only
through one domain-agnostic primitive the server exposes (`microagent:call`: model class, prompt,
timeout in, text and usage out — no idea what agent or product is calling). Real provider keys and
billing never leave the server either way; what moved is only *whose code decides what the prompt
says and whether the result is good enough*.

**The escalation ladder** (same on both sides of that boundary). Every microagent call starts on
the cheapest model that can plausibly do the job (`nano`), and only escalates to a stronger one
(`nano` again, told what failed; then `mini`) if the cheaper attempt's answer fails *validation* —
malformed structure, a failed sanity check — never merely because an answer seemed debatable but
was still well-formed. A single, shared `Runtime` (one adapter, one ladder, one concurrency limit)
backs every microagent; each domain registers its own agents into it from its own point in the
code, so unrelated domains never have to share files just to share the Runtime.

**The microagents today:**

| Microagent | Runs | What it decides |
|---|---|---|
| `FactMemory` | server | What's actually worth remembering out of something the user said |
| `AddressBook` | server | Whether a name in conversation is an existing contact, by meaning, not spelling |
| `TopicMemory` | server | Which stored memories a new question is actually about |
| `DocumentMemory` | server | How a saved document gets indexed and searched later |
| `turn.permission.identify` | client | Pure fact-finding: is a pending send/spend outstanding, and whose identity does it go out under |
| `turn.permission.guard` | client | Whether the assistant actually has the user's OK for what it's about to do |
| `reply.review` | client | Whether an outbound reply is on-topic, free of internal leakage, and sounds human |
| `turn.refusal.guard` | client | Whether the assistant is quietly declining a real task on its own judgment |
| `turn.action.summary` | client | One-sentence description of exactly what a send-type tool call is about to do |
| `turn.reply.expected` | client | Whether a reply is actually expected after a message just sent |
| `turn.background.relay.worth` | client | Whether a background/reminder result is worth interrupting the user for at all |
| `content.summarize` | client | Compressing a long piece of text to its gist |
| `reply.translate` | client | Translating outbound text, preserving deliberate quotes/examples in another language |
| `action.sanction` | client | Whether a real user message just granted or revoked approval for an open action |
| `turn.delivery.completeness` | client | Whether an outbound message actually contains what it claims to be delivering right now |

The first four back `memory_plugin.py`'s `save_info`/`request_info` and stay server-side. Everything
from `turn.permission.identify` down is Caroline's own `reply:` domain, client-executed, living in
`backend-py/app/microagents/` (vendored from `reforce` for the first eight — business logic
unchanged, only the `Microagent` import differs — plus two written directly for Caroline:
`action_sanction.py`, `delivery_completeness.py`). Three of them are about trust rather than
usefulness and worth a closer look:

- **`reply.review`** checks every outbound message — including the small "still working on it"
  progress remarks — for three things: is it actually about the task, does it leak anything about
  Caroline's own internal plumbing (a tool name, an internal retry, an internal error) instead of
  just talking about the work, and does it read like a person rather than a system describing
  itself. A failure on any of those gets rewritten before it reaches the user, not just asked
  nicely not to happen. Called from `app/plugins/voice_api.py`'s `review_reply()`, right after
  translation, at every real send site in `chat_session.py`.
- **`turn.permission.guard`** closes a specific, observed gap: Caroline would ask a yes/no
  permission question and then, a turn or two later, act anyway even though nobody had answered.
  An unanswered (or ambiguous) yes/no question is now treated as a **refusal**, not permission by
  default. The same strict rule applies even without a spoken question: anything that communicates
  with a third party on the user's behalf, or touches the user's money, needs a clear, *specific*
  approval already on record — a general "sure, go ahead" for the task doesn't count. As of
  2026-10-18, that approval is also tracked explicitly, not just re-derived from a dialogue window
  every time (see "Action boundaries" below) — an approval the user already gave can no longer
  scroll out of view and get re-asked. The dialogue it judges is read from the transcript on disk, which
  lacks a real user message until the CLI dequeues it a few seconds later; the latest real user message
  is therefore appended to it (as the narrator already does), or a "yes" given a moment earlier is
  invisible and the guard stops the turn and makes the model ask again (seen live, 2026-10-26).
- **`turn.delivery.completeness`** (2026-10-18) closes the mirror-image gap: Caroline *believing*
  she showed the user something (a draft, a figure, an attachment) that the actual outbound message
  never contained. Checked right after `reply.review`/translation, on every outbound block; never
  blocks the send, corrects course with a proactive follow-up note instead.

**Action boundaries** (`app/action_registry.py`, 2026-10-18). A short-lived, per-tab record of a
*bounded operation* ("send the renewal email to Jessica Zhou") — distinct from a **track**
(`TopicMemory`'s long-lived, cross-tab topic; unrelated, untouched by this). `turn.permission.
identify` matches a pending action against up to 5 open ones (LRU-evicted) on the same call it
already makes; a separate microagent, `action.sanction`, reconsiders every open action's approval
after each real user message. An action already flagged `approved` skips `turn.permission.guard`
entirely for that one call — no re-derivation, no window to scroll out of.

**Agentic evolution** (`API/Api2Evolution.py` in `reforce`). The main model can't be retrained on
the fly — a system prompt or a microagent's own instructions are the only lever available in the
moment. But a microagent's *prompt* can improve from real experience, continuously, without ever
touching the main model: Camerlengo can clone one of its own prompts, mutate the clone
(`evolution.mutator`), and run the mutant head-to-head against the original on the exact same real
task the original was about to handle anyway. An independent judge (`evolution.selector`) picks
the better result for that one call, blind to which side is the incumbent; win/loss/tie accumulates
in `microagent_tournaments`, and a challenger that keeps winning gets promoted — the new default,
from that point on, with no shadow period. A role with no settings row, or whose dice roll says
"not this time," pays zero extra cost — the overwhelming majority of calls. For the client-executed
`reply:` domain, the server only ever hands back *which* genome to run (`evolution:resolveCalls`/
`mintClone`/`recordOutcome`) — it never executes a model call itself for those roles; the mutation,
the champion/challenger run, and the judging all happen in Caroline's own process, same as every
other client-executed microagent.

## Security: secrets handling

`app/secret_store.py` is a Windows-DPAPI-based encryption-at-rest helper
(`CryptProtectData`/`CryptUnprotectData`, scoped to the current Windows user account), used for
messenger integration credentials and session state (`<workspace>/messengers/<service>/`). Most
other local state — SquirrelWisdom credentials, Ratatosk's own account file — is plain JSON on
disk; broader secrets (API keys, tokens, passwords a user shares in conversation) are never
written to local files, chat history, or Skills content at all — they're saved as a note in a
dedicated SquirrelWisdom Notes "vault" folder instead, a server-side policy enforced through the
system prompt (`app/policies.py`) rather than a client-side encryption boundary.

## OpenAI as a third answer source (Codex engine)

A tab answers through Claude, SW, or OpenAI, chosen with the `Claude / SW / OpenAI` text switcher
in the native tab header. `CodexEngine` runs one `codex-app-server` process per tab and is embedded
rather than exposed — every launch disables Codex's own plugin system, update checks, and
analytics, and its state lives under a dedicated `codex-home`, separate from any Codex the user
runs themselves. Sign-in is a ChatGPT subscription, a device code, or a pasted API key
(`openai_account.py`); install is a pinned, sha256-verified `codex-app-server.exe`
(`CodexInstaller.cs`) under `runtime\codex` — missing it simply makes OpenAI unavailable, nothing
else breaks.

## Settings screen (`chat.html`/`chat.js`)

Personality (persona editor), Window (always-on-top), Voice (auto-send toggle), Speech recognition
(local STT on/off), Visual mode, Models (Claude/OpenAI model picker), Claude account (OAuth
login/logout/status), Account & Billing (resolved chat source, own-API-key, SquirrelWisdom
login/balance/top-up), OpenAI account, Ratatosk messenger (owner status + register Caroline's own
account), MCP servers (list/add/remove — the standard Claude Code mechanism, not a custom config
format). Phone-companion pairing is no longer a Caroline-side settings flow — the Android companion
app registers itself against the SquirrelWisdom account server-side instead
(`app/plugins/companion_api.py`), with no pairing UI in `chat.html`.

## Build & deploy

The root `Makefile` builds the WPF shell, bundles the (still Node-built) default MCP servers from
`backend/`, copies `backend-py/app` as "the live backend" into the installer output, and does a
self-contained `dotnet publish` into `dist/`. `make installer` additionally zips `dist/` and
publishes a self-extracting `CarolineInstaller.exe`; `make deploy` (private, `deploy.mk`,
gitignored) uploads `Caroline.zip`/`.sha256`/`.version`/`CarolineInstaller.exe` to
`downloader.multi-portal.org/apps/caroline/`.
