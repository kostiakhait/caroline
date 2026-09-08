# Caroline — project overview

A standalone Windows desktop AI secretary: email, notes, browser/Windows automation, messaging,
SMS, scheduling — driven by natural-language chat, as a tray app with a global hotkey and a chat
window, no terminal or IDE required. Built on the Claude Agent SDK, with a persistent identity
(persona), voice, and its own SquirrelWisdom-backed feature set.

This file is the detailed, code-derived companion to `README.md` (portability/build basics). It
describes what's actually implemented, not the roadmap.

## Three layers

- **`Windows/Caroline/`** — WPF shell (C#). Tray icon, global hotkey (default Ctrl+Alt+C), a
  WebView2-hosted chat UI (`wwwroot/chat.html`/`chat.js`), a Settings panel, and several
  secondary windows (`DocumentViewerWindow`, `AppBrowserWindow`, `VisualModeWindow`,
  `SplashWindow`). Spawns and supervises the backend sidecar process (`BackendProcess.cs`).
- **`backend/`** — Node/TypeScript sidecar. Hosts a loopback WebSocket (`ws://127.0.0.1:8765`)
  the chat UI talks to, and wraps the Claude Agent SDK's streaming `query()` per chat tab.
- **`backend/mcp-servers/`** — the default MCP tool roster, built from this repo's
  `backend/mcp-servers-src/*` sources by `scripts/bundle-mcp-servers.mjs` (esbuild-bundled into
  single `.mjs` files; `browser` is copied wholesale instead, see that script's own header comment
  for why).
- **`Windows/CarolineInstaller/`** — a separate, no-admin-rights WPF bootstrapper that downloads
  `Caroline.zip` from `downloader.multi-portal.org`, installs isolated Node/Python runtimes under
  `%LocalAppData%\Caroline\runtime\` (never touches system PATH), and Chromium for Playwright.

Caroline is **portable by design**: nothing at runtime references any repo outside this one.
Everything ships inside the install directory or lives in the per-user workspace
(`%LOCALAPPDATA%\Caroline\workspace\`).

## Chat session core (`server.ts` + friends)

Each browser tab in the WebView2 chat UI gets its own `ChatSession`, each running its own Claude
Agent SDK `query()` against `systemPrompt: {type: "preset", preset: "claude_code", append: [...]}`
— the `append` array is where `persona.ts`'s identity block and every instruction in
`policies.ts` get concatenated in.

Reliability engineering, all confirmed-live fixes for real incidents, not speculative hardening:

- **Watchdog + auto-restart**: a hung or crashed `query()` gets killed and restarted, replaying
  the unanswered message, so the app never needs a manual restart.
- **`durability.ts`**: survives a full app close/crash mid-turn (not just an in-process watchdog
  restart) — pending-turn state and each tab's own resumed session id persist to disk.
- **`compaction.ts`**: ages out old context from a tab's ever-growing resumed session without
  ever touching the live/original transcript, so a long-running tab doesn't blow its context
  window.
- **`processReaper.ts`**: cleans up a real leak class — a `query()`'s underlying CLI process (and
  its whole MCP-server child-process tree) that fails to exit when Caroline abandons it for a
  fresh `query()`.
- **`sharedMcpServers.ts`**: every bundled utility server (mouse/keyboard/notes/time/sms/voice/
  screen-video/etc.) runs as ONE long-lived HTTP-mode instance per backend process, shared across
  every tab, instead of each tab spawning its own stdio copy — fixes a confirmed 240+ `node.exe`
  process swarm that accumulated over a day of restarts when stdio (strictly 1:1) was the default.
  `caroline-browser` is the deliberate exception (still per-tab stdio): each tab genuinely needs
  its own browser profile/CDP session.
- **`history.ts`**: reads real conversation history straight from the Claude Code CLI's own
  session transcript (`~/.claude/projects/.../*.jsonl`) — the authoritative record, independent
  of what the chat UI happens to have rendered.

## Identity, voice, and presentation

- **`persona.ts`**: `profileKey` is `"custom"` (freeform name/gender/age/bio) or a standard
  profile (`"caroline"` / `"peter"`), each with its own fixed identity/biography/photo set that
  Settings can override per-field. Default is an independent-minded, middle-aged personal
  secretary persona — deliberately not a blank slate or a deferential assistant (an unset
  gender/identity was observed to drift). Applied via the system-prompt `append`, takes effect on
  the next session restart, not mid-session.
- **`voice.ts`**: STT/TTS via Camerlengo v2 (`ai:tts`/`ai:stt`), with `localTtsServer.ts` as a
  faster local-first path — spawns a local `edge-tts` HTTP server
  (`python-scripts/local_tts_server.py`) once per backend process lifetime, purely to cut
  per-call latency (no per-call Python interpreter startup); falls back to the Camerlengo path
  whenever the local server is unavailable or errors. Chat UI has a mic button, auto-send toggle,
  and a 🔊 replay button on any message.
- **`visualMode.ts`** + `VisualModeWindow`: instead of just playing voice replies aloud, shows a
  small animated talking-head window (Caroline/Peter profiles only).
- **`backend/mcp-servers-src/voice`** (`text_to_speech`/`speech_to_text`) and
  **`backend/mcp-servers-src/screen-video`**
  (`start_screen_recording`/`stop_screen_recording`/`sample_video_frames`) are general-purpose
  sibling MCP servers (same dual-use pattern as `backend/mcp-servers-src/sms` below) bundled in as
  `caroline-voice`/`caroline-screen-video`, paired with the `analyzing-video` skill.

## Paying for chat: `subscriptionMode.ts`

Every session resolves ONE of four chat sources, in this priority order, and never silently
switches someone already paying Anthropic directly onto a metered proxy just because they *also*
happen to have an SW account:

1. **`own-anthropic-oauth`** — the bundled CLI's own `claude auth login` OAuth session, if logged in.
2. **`own-anthropic-key`** — a manually pasted `ANTHROPIC_API_KEY` (Settings → Account & Billing).
3. **`sw-proxy`** — SquirrelWisdom's own account, if logged in: `ANTHROPIC_BASE_URL` points at
   `squirrelwisdom.com`, billed against the user's PIA wallet (see reforce's
   `Api2AnthropicProxy.py`, which forwards to OpenRouter's Anthropic-Messages-shaped endpoint).
4. **`none`** — the CLI's own request just fails; that failure is what triggers the
   credentials-needed UI, not a separate first-run check.

**Own-Anthropic exhaustion fallback**: "available" above means "logged in", not "currently has
room left" — a rate-limited or credit-depleted own-Anthropic account used to make `resolveMode()`
keep re-picking it forever, even with a paid, logged-in SW account sitting unused. Per explicit
instruction (2026-09-08): once own-Anthropic is CONFIRMED exhausted on a real request (a
`billing_error`, or a rejected `rate_limit_event`), `markOwnAnthropicExhausted()` records it
(honoring the SDK's own `resetsAt` when the failure supplied one, else a 30-minute default
cooldown), and `resolveMode()` skips straight to `sw-proxy` while that lasts. Only applies when SW
is logged in; with no SW account there's nowhere to fall back to, so it just keeps retrying
own-Anthropic as before.

Switching back is active, not just a wait for the cooldown to expire — confirmed live the same day
that real availability can flap faster than the SDK's own `resetsAt` suggests (five switches in one
morning), so `resolveMode()` lets a real attempt through to own-Anthropic every
`OWN_ANTHROPIC_RECHECK_INTERVAL_MS` (2 minutes) regardless of how far off the nominal cooldown still
is. If that attempt reaches the CLI's `init` message, `clearOwnAnthropicExhausted()` (called from
server.ts's `init` handler, same trust level as its own connState recovery) treats that as
confirmation and lifts the block; if it fails again, whichever detection path catches it just
re-arms `markOwnAnthropicExhausted()` with a fresh cooldown, same as any other failure.

`lastRateLimitInfo` (crash-attribution memory for a silent stream death, see its own doc comment)
is cleared whenever a deliberate chat-source switch happens — before this fix it survived the
switch to sw-proxy and misattributed that source's own, unrelated failures (confirmed live: a
`Prompt is too long` turn) to "still exhausted on own-Anthropic", which was wrong information even
though the resulting chatSource choice happened to still be correct.

A depleted **SW** balance (`handleBalanceExhausted`, source `"sw"`) never retries against a
different source — there isn't one — so it explains what happened (status-bar/system_notice UI
chrome, always English, not a chat reply) and opens a Revolut-hosted top-up checkout window
directly (`createTopupCheckoutUrl`).

## SquirrelWisdom-backed tools and the login gate

Several features need the user's own SquirrelWisdom account, separate from which account pays
for chat. `swGate.ts`'s `SW_GATED_FEATURES` is the single registry of which tools need it, and
`requireSwOrPrompt()` is the shared gate every one of them calls: on a gated tool's **first**
refusal it auto-opens the native login/sign-up window; on any further refusal it just says so
without reopening — only `ensure_squirrelwisdom_login` (an explicit user request) or Settings'
"Log in" button reopen it after that. Credentials never pass through the chat/model context: the
native login form posts straight to the backend over the app's own channel.

SW-gated features today:

- **Notes** (`caroline-notes`, external MCP, same binary as `backend/mcp-servers-src/notes`) —
  long-term memory.
  `notes_login` is explicitly hidden from the model (`disallowedTools`) because it would otherwise
  ask for the password as a tool parameter, violating the hard "never type a password into chat"
  rule; the native login window is the only path.
- **Ratatosk messaging** (`caroline-ratatosk`, in-process) — every tool takes `as: "owner" |
  "caroline"`. `"owner"` acts as the user's own SquirrelWisdom/Ratatosk session (broad standing
  authorization, confirmed with the user, no per-message confirmation needed) and is what's
  SW-gated; `"caroline"` acts through Caroline's own separate, auto-generated Ratatosk account
  (`ratatoskOwnAccount.ts` — a real navlink.net mailbox + SquirrelWisdom account, no user input
  needed) and is NOT gated, since it's Caroline's own identity, not the user's.
- **`consult_large_model`** (`caroline-consult`, in-process) — asks a GPT-5-class model
  (Camerlengo's `ai:resolve` with `model:"LARGE"`) for wording advice on high-complexity,
  high-importance legal/commercial/social questions. Advice only, folded into Caroline's own
  answer, never relayed verbatim.
- **Office document editing** (`open_in_viewer`'s document branch, `caroline-viewer`) — docx/
  xlsx/pptx/pdf open in a real embedded OnlyOffice editor (`officeEditor.ts` uploads to a
  throwaway path on squirrelwisdom.com and gets back an editor session), since it needs the
  document briefly hosted there.
- **SMS** (`caroline-sms`, external MCP, same binary as `backend/mcp-servers-src/sms`) — see next
  section.

## SMS: per-user SMTP2GO accounts

Send/receive SMS is a general-purpose Camerlengo v2 capability (`sms:send`/`sms:viewReceived`,
`API/Api2SMSCommands.py` in reforce), not Caroline-specific — `backend/mcp-servers-src/sms`
(mirroring `backend/mcp-servers-src/notes`'s shared-login structure) and `caroline-sms` both call
it. Per explicit design, **every user sends/receives through their own SMTP2GO account**, not a
shared one:

- Settings → "SMS account" lets a user paste their own SMTP2GO API key (+ optional dedicated
  sending number). The key is verified against SMTP2GO's real API before being persisted
  server-side (`sms_accounts` table, keyed by SquirrelWisdom login) — never echoed back, only
  "is one set" + the sender number.
- `sms:send`/`sms:viewReceived`, when called with a session, require a registered account (no
  silent fallback to a shared one); without a session (internal/system calls) they fall back to
  a shared account for backward compatibility.
- Receiving is polling, not a webhook — SMTP2GO's "received" messages are replies to SMS you
  sent. A background poller (`poll_received_sms`, started from `Camerlengo.py`) sweeps every
  registered account each tick, each with its own independent persisted high-water-mark.

## General-purpose tool roster

**In-process SDK tools** (built straight into the backend, `server.ts`'s `mcpServers`):
`caroline-scheduler` (reminders — `schedule_reminder`/`list_reminders`/`cancel_reminder`,
persisted to `workspace/schedule.json`, survives app restarts, fires proactively via
`injectProactive` even with no user message in flight), `caroline-files` (open a local file in
its default Windows app), `caroline-viewer` (image/video/document viewer window,
`open_in_viewer`/`close_viewer`), `caroline-login` (`ensure_squirrelwisdom_login`),
`caroline-email` (own in-process fork of `backend/mcp-servers-src/email` — send/delete/move/mark/download return
immediately and report their real outcome via a proactive follow-up rather than blocking the turn
on a slow IMAP/SMTP round trip), `caroline-appbrowser` (the embedded multi-window browser, see
below), `caroline-ratatosk`, `caroline-consult`.

**Shared HTTP-mode utility servers** (`sharedMcpServers.ts`, one instance per backend process,
`caroline-`-prefixed, ports 9401+): `caroline-notes`, `caroline-sms`, `caroline-time`,
`caroline-voice`, `caroline-screen-video`, and the Windows-automation family
(`caroline-windows-{screenshot,mouse,keyboard,inspect}` and their `window-*` window-targeted
twins, `caroline-windows-chain`).

**Per-tab stdio** (`caroline-browser`): `appBrowser.ts`/`appBrowserCdp.ts` — one persistent,
labeled WebView2 window per site (whatsapp/telegram/facebook/etc.), driven over a real CDP
connection (snapshot/find/click/type/press_key/evaluate), not simulated input.

## Skills (`Skills/`, seeded from `backend/skills-src/`)

Copied into the per-user workspace on every start (code-managed defaults, not gated behind a
one-time marker, so a skill content update actually takes effect): `squirrelwisdom-login` (when/
how to call `ensure_squirrelwisdom_login`, the hard no-password-in-chat rule),
`vault-backups` (secrets policy below), `embedded-browser-troubleshooting`,
`ratatosk-messenger`, `showing-files-in-chat`, `python-environment` (the isolated
installer-provisioned Python, not system PATH), `analyzing-video`.

## Security: the vault policy

`policies.ts`'s `vaultSecurityInstruction()`: secrets/passwords/API keys/tokens are never written
to local files, chat history, or Skills files — always saved as a note in the "Caroline:Vault"
Notes folder instead. Backed up there once an hour (`ensureRecurringBackup`) and once on app
close (`shutdown_sync` control op, triggered from `chat.js`'s `pagehide` — the one point where
WebView2 actually unloads rather than just minimizing to tray; best-effort, since the WPF shell
doesn't wait for it before `Process.Kill()`).

## Settings screen (`chat.html`/`chat.js`)

Personality (persona editor), Window (always-on-top), Voice (auto-send toggle), Visual mode,
Claude account (OAuth login/logout/status), Account & Billing (resolved chat source, own-API-key,
SquirrelWisdom login/balance/top-up), Ratatosk messenger (owner status + register Caroline's own
account), SMS account (own SMTP2GO key/sender), MCP servers (list/add/remove — the standard
Claude Code mechanism, not a custom config format).

## Build & deploy

`build.bat` builds the backend, bundles MCP servers, and does a self-contained
`dotnet publish` of the WPF shell into `dist/`. `Caroline.csproj`'s `SyncBackendDist` MSBuild
target copies fresh `backend/dist/*.js` into the build output on every `dotnet build`.
`build_installer.bat` builds `CarolineInstaller.exe`; `deploy.bat` uploads
`Caroline.zip`/`.sha256`/`.version`/`CarolineInstaller.exe` to
`downloader.multi-portal.org/apps/caroline/` over scp/ssh.
