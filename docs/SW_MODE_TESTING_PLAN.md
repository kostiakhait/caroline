# Large-scale automated test of Caroline's sw-mode across 4 task categories

## Goal (restated 2026-10-22, per explicit instruction)

Caroline on the SW subscription must work **no worse and no more expensive than on the Claude
SDK -- with no Claude at all**. The Claude SDK is the reference for behavior: what a Claude turn
does, an sw turn must do too. Every gap found here is a bug to close, not a limitation to note.
There is no side-by-side Claude baseline run (explicit decision): behavior is compared against how
the Claude SDK path works, cost against the Claude subscription price.

## Context

Caroline can answer a real turn two ways: the full Claude Agent SDK ("claude" mode, today's
default) or "sw" mode -- a cheaper small-model cascade (`app/small_model_engine.py`) that reaches
Camerlengo's `resolve_agentic()` with the SAME plugin tools the SDK path uses, billed through the
user's SquirrelWisdom wallet, escalating to the full SDK only on the model's own judgment or a
mechanical tool-loop safety check. Per explicit direction, SW is meant to become Caroline's
*primary and eventually only* path -- so before that bet is made, its actual ability to finish
real, multi-step, multi-tool work needs a real, large, automated test, not an assumption.

Four task categories, all exercised in "sw" chat mode specifically:
1. Reading and replying to email
2. Composing documents
3. Managing messengers
4. Writing code

Hard requirements from discussion: fully automated (no manual click-through), run entirely in an
isolated environment -- a disposable Linux box, never the dev's own machine (standing rule) --
and, per explicit instruction, **all functionality relevant to these 4 categories must first be
confirmed/ported to Linux** (Phase 0 below). One big one-off run, not a recurring suite.

Research done up front (2026-10-20/21): `docs/LINUX_PORT_PLAN.md` (Phases 1-3 done and
live-verified on disposable Vultr boxes already; Phase 4/5 -- shell, AppImage, auto-update --
mostly done), `app/durability.py`'s `load_chat_mode`, `app/small_model_engine.py`'s header,
`app/plugins/loader.py`'s `discover_plugins()` (confirms "sw" and "claude" modes share the exact
same plugin tool set -- not a reduced capability surface), and three Explore passes over every
messenger channel, the email/code/document plugins, and `requirements-linux.txt`.

**Explicitly out of scope for this plan** (per direct instruction): standing up real, logged-in
credentials for WhatsApp/Signal/Discord/Slack/Messenger, and deciding whose SW account/phone
numbers/emails the test instance uses. This plan ports the CODE and verifies each integration can
be *reached* (loads, registers its tools, attempts its connection logic without an OS-level crash)
on Linux -- not that every channel has a live, working login. Which channels get real, end-to-end
task coverage in the final run depends on whatever credentials exist by then (as of Phase 0: only
Telegram and Ratatosk have real logged-in accounts).

## Phase 0 -- Close the Linux code-level gaps this test actually depends on

**Status: done (2026-10-21), verified live** on a disposable Vultr Ubuntu 24.04 box (Xvfb +
openbox, same recipe `docs/LINUX_PORT_PLAN.md` already used; destroyed after verification, per
standing instruction).

Confirmed-working already (no action needed): email (`email_plugin.py`/`email_batch_plugin.py` --
pure Camerlengo `email:*` HTTP calls, no OS dependency at all), sw-mode startup
(`small_model_engine.py`, `model_key_provisioning.py`, `sw_gate.py` -- no Windows dependency,
no `secret_store.py` use), code-writing tools (`shell_plugin.py`/`files_plugin.py` -- already
platform-branched, and confirmed reachable from "sw" mode via the shared plugin loader), document
composition (`office_editor.py` -- uploads/downloads via Caroline's own already-portable file
tools; the OnlyOffice *editor* itself is server-hosted by reforce and rendered through the
already-ported Linux embedded browser, so it needs no Caroline-side port at all), Ratatosk
(`ratatosk_plugin.py`/`ratatosk_own_account.py` -- pure `httpx`, already clean).

Real blockers found and fixed, each independently verified:

1. **`app/secret_store.py`** was Windows-DPAPI-only (`win32crypt`), used by `telegram_channel.py`,
   `discord_channel.py`, `slack_channel.py` for token persistence. Added a Linux branch: a Fernet
   key written once to a file under the workspace dir with `0600` permissions, same
   `encrypt_text_to_file`/`decrypt_text_from_file` call signatures the three channels already use
   -- zero changes needed in the channels themselves. File+local-key, not a desktop keyring (a
   headless disposable box has no keyring daemon to talk to). Verified live: round-trip + `0600`
   permissions confirmed on the test box; Windows DPAPI round-trip re-confirmed unaffected.

2. **`backend-py/requirements-linux.txt`** was missing `playwright` (needed by
   `app_browser_cdp.py`/`_linux_app_browser.py` -- the file's own header wrongly claimed Linux
   excluded it, contradicted by live Phase-3 code) and `qrcode`/`Pillow` (needed by
   `whatsapp_channel.py`'s and `signal_channel.py`'s QR-login image generation, previously failing
   silently into a logged `qr_render_failed`). Added all three pins; `provision_runtime.sh` now
   also runs `playwright install chromium` after installing the pinned requirements. Verified
   live: a real Chromium window opened via Playwright on the test box.

3. **`provision_runtime.sh`** used to provision `runtime/python` and `runtime/codex` only. Added
   Node.js 22.23.4 provisioning (WhatsApp/Messenger Node sidecars) and signal-cli 0.14.9's
   "-Linux-native" (GraalVM native-image, self-contained, no separate JRE needed) provisioning.
   Verified live: `node --version`, `signal-cli --help` both ran correctly on the test box.

4. **`signal_channel.py`**'s `_require_tooling()` unconditionally required `CAROLINE_JAVA_HOME` --
   wrong now that Linux provisions signal-cli's native build (no JRE at all). Gated that check to
   `win32` only. Verified live: `_require_tooling()` no longer raises on Linux with no
   `CAROLINE_JAVA_HOME` set.

5. **`messenger_channel.py`**'s (Facebook) login flow was hardwired to `AppBrowserHost`
   (WebView2 -- Windows-only, no Linux equivalent). Added `_login_browser_open`/
   `_login_browser_get_cookies`/`_login_browser_close`, platform-branching to
   `_linux_app_browser.py` (already-ported, Playwright-backed) on Linux; added a new
   `get_cookies()` function to that module (Playwright's own `BrowserContext.cookies()`, reshaped
   to the same `{"key", "value"}` field naming AppBrowserHost's `/get_cookies` already returns, so
   the caller needs no shape-level branch). Verified live: a real open/get_cookies/close cycle
   against a real Chromium window on the test box.

6. **`shell_plugin.py`**'s tool description/usage text was hardcoded Windows-flavored (`tasklist`,
   `taskkill`, `.exe` examples). Platform-branched both the tool description and
   `_usage_instructions()`. Verified live: Linux text contains `pgrep`/`ps aux`, no leaked Windows
   commands.

7. **Slack** has no registered Slack App at all (`SLACK_CLIENT_ID` empty) -- not OS-specific,
   never exercised live on any platform. No code fix applies; noted so it isn't mistaken for a
   Linux gap when the eventual live-account step (out of scope) gets to it.

Also fixed along the way, found during live verification (not anticipated in the original plan):

- **`make dist-linux`** never copied `whatsapp-sidecar`/`messenger-sidecar` (the Node sidecar
  source) at all -- added, mirroring the Windows build's own stamp rules. Verified live: both
  sidecars run on the provisioned `runtime/node` -- the WhatsApp sidecar reached a genuine QR
  pairing code from WhatsApp's own servers; the Messenger sidecar failed cleanly on its own
  "no appState yet" application logic, not on an import/runtime error.
- **`npm install` against the bundled `runtime/node` fails** unless that runtime's `bin/` is on
  `PATH` first (npm's own shebang resolves `node` via `env`, not via its own absolute path) --
  corrected the Makefile's printed post-build instructions to prepend `PATH=.../runtime/node/bin`.
- `supervisor.py`'s `_child_env` had no Linux branch at all for `CAROLINE_NODE_PATH`,
  `CAROLINE_WHATSAPP_SIDECAR_DIR`, `CAROLINE_MESSENGER_SIDECAR_DIR`, or `CAROLINE_SIGNAL_CLI_BAT` --
  added all four (no `CAROLINE_JAVA_HOME` on Linux, per point 4 above).

Files touched: `backend-py/app/secret_store.py`, `backend-py/requirements-linux.txt`,
`linux-shell/provision_runtime.sh`, `backend-py/app/signal_channel.py`,
`backend-py/app/messenger_channel.py`, `backend-py/app/plugins/_linux_app_browser.py`,
`backend-py/app/plugins/shell_plugin.py`, `backend-py/supervisor.py`, `Makefile`. Committed
2026-10-21 (`6fb3967`), pushed to `origin/main`.

## Phase 1 -- Stand up the disposable test box

**Status: done (2026-10-22), verified live.** Fresh Vultr Ubuntu 24.04 instance (region fra, plan
`vc2-1c-2gb`), same apt package list `docs/LINUX_PORT_PLAN.md`'s own recipe already uses, plus
what Phase 0 added (Node.js, signal-cli -- no JRE needed, the native build is self-contained).
Built with `make dist-linux` (reused the Phase 0 output, unchanged since) and provisioned both
runtimes via the Phase-0-extended `provision_runtime.sh`; `npm install` for both sidecars against
the provisioned `runtime/node` (using the Phase-0-corrected `PATH=...runtime/node/bin:$PATH`
prefix). `Xvfb :1` + `openbox` started for a real virtual X11 desktop. `supervisor.py` launched
with `CAROLINE_APP_ROOT`/`DISPLAY` set -- reached a healthy state with no manual intervention:
`GET http://127.0.0.1:48766/status` (the supervisor's own port) returned 200 with
`backendRunning: true`, and the backend's own `GET http://127.0.0.1:48765/api/status` returned
200 with `{"ok": true, ...}` -- the same bar `docs/LINUX_PORT_PLAN.md` already used for its own
Phase 5. Box kept running (not torn down yet) since Phases 2-5 reuse it.

**Real gotcha found while standing this up, not anticipated in the original plan**: the Vultr
account's API key is now IP-allowlisted, and the dev machine's own (dynamic) egress IP isn't on
it -- every `create`/`poll`/`delete` instance-management API call had to be proxied through
`snc-ops-1` (188.245.0.102, already-whitelisted IPv4; its own env file holds the same API key) via
SSH, forcing `curl -4` specifically (that box prefers IPv6 by default, which is NOT allowlisted and
fails the same way). Direct SSH to the test box itself is unaffected by this -- only Vultr's own
management API is IP-gated.

## Phase 2 -- Test harness: how tasks get sent in and verified

**Status: done (2026-10-22), verified live.** The harness (kept outside the repo, under `d:\tmp`
on the dev machine and `/root` on the box) opens a real WebSocket tab, sets sw mode, sends one or
more user messages as consecutive turns in that tab, judges completion from the backend's own log
(not from the first `result` frame, which can belong to an internal turn), and sums token usage
per model for the tab. The box now runs Caroline as an ordinary user (`caroline`), not root, and
is logged into SW as the test account `test@navlink.net` (password reset 2026-10-22, in Notes).

Its first runs found real bugs, all fixed, pushed to `main`, verified on the box:

- `74a1029` sw mode needed a Claude subscription on top of SW (`chat_mode_eligible`); now SW only.
- `a4ebe6d` the 24h-dialogue refresh worker path broke in the 2026-10-17 mixin split (0 successes,
  741 failures in the live Windows log) -- every tab, both modes.
- `2236b53` Claude Code project-dir slug didn't match the CLI's rule (any non-ASCII-alphanumeric
  -> `-`); on Linux every transcript was looked up in a nonexistent directory.
- `e329e97` sw turns weren't persisted at all in a tab with no Claude session, so the model forgot
  the previous turn ("remember 4711" -> "which number?" took 4 min and answered wrongly) and replies
  came back in English; plus no translation into a guessed language on a tab's first turn.
- `2990817` Claude machinery kept running in sw tabs: a CLI started before the switch to sw,
  its failure nudge became a 75 s / ~145K-token phantom sw turn, and forced compaction of that CLI
  replaced the tab's session and lost sw history. Now a Claude-less tab starts in sw, switching to
  sw tears the CLI down, and none of that machinery touches sw tabs.

After the fixes: "remember 4711" -> "Понял." (18.8 s), "which number?" -> "4711" (15.7 s).

Still open, found by the same runs: per-tab cost of fast-lane and other auxiliary model calls is not
logged (no `tab_id`, no usage), so Phase 3 can't measure cost honestly yet; every small-model call
carries 255 tool definitions (~29K prompt tokens); a ready fast-lane answer reached the user 7.5 s
later than it was produced.

The chat window's own rendering is irrelevant to whether sw-mode can finish a task -- Caroline's
own tool use (already Linux-ported where Phase 0 closed the gaps) is what's actually under test,
not pixel-level clicking by the test harness. The harness:

- Creates a tab, sets `chat_mode="sw"` for it (`durability.py`'s `save_chat_mode`).
- Submits each test task as a real user turn over the backend's existing HTTP/WebSocket API --
  the same entry point the real chat UI uses, just driven directly (via a small script, not the
  rendered window).
- Waits for turn completion, then reads back: the final assistant reply, `caroline.log`'s
  structured events for that tab/turn (tool calls made, args, outcomes -- already logged via
  `log_event`), and whether an escalation to the full SDK occurred (the model's own
  `ESCALATION_SENTINEL` or the mechanical tool-loop safety check firing -- both already logged).
- Verifies the actual side effect, not just the reply text: re-reads the target mailbox for email,
  re-opens the produced file for documents, re-fetches the channel's own message history for
  messengers, actually runs the produced code for the coding category. A task only counts as a
  pass if the real-world effect is correct, not if Caroline merely claims it is.

Per task, records: pass/fail, whether sw-mode handled it alone or escalated, which tools were
called, and wall-clock time -- this is the actual signal the whole test exists to produce.

## Phase 2b -- SW memory on par with the Claude SDK

**Status: designed (2026-10-22, every point below agreed in chat), not implemented.**

The gap: an sw turn's context is the system prompt plus the last 12 lines of visible dialogue text
(`RECENT_DIALOGUE_WINDOW`). Tool calls and their results live only inside the turn
(`resolve_agentic` keeps them in its own copy of the message list) and are gone by the next one --
"now fix the second file too" doesn't know which files the previous turn touched. Fine for a chat
without tools, not for an agent. The Claude SDK replays the whole trajectory (`tool_use` /
`tool_result`) since the last compaction, kept cheap by prompt caching, and compacts it into a
summary when it grows. SW gets the same, in its own way:

1. **Every tool call goes to its own file**, in full: arguments and the complete result. Captured
   in Caroline's own executor wrapper (`_make_executor_fn`), which sees the result before
   `resolve_agentic` cuts it to 8000 characters -- no change to Camerlengo.
2. **A small-model microagent summarizes each turn's tool calls** once the turn ends: what was
   looked at, changed, found, with each item carrying the link to its call's file. Skipped for a
   turn that made no tool calls.
3. **SW keeps its own history file per tab session** (its own format, not Claude Code's): per
   turn, the user's message, the tool-call summary with links, the final answer. The Claude Code
   transcript keeps receiving the plain question/answer pairs exactly as now -- the narrator,
   language detection, the 24h-dialogue file and the permission guard read it.
4. **The next sw turn's context is built from that history** since the last compaction boundary,
   not from 12 lines. The model opens a call's file when it needs the full detail.
5. **A compaction microagent** compresses the older part once the history exceeds ~40K tokens:
   what's done, what was decided, what's still open, the user's standing instructions, the links
   that still matter. The last 6 turns always stay verbatim. Both numbers are starting values, to
   be tuned against measured cost.
6. Both microagents are Caroline's own local ones (`_run_local_microagent`, same as
   `content_summary`/`action_summary`), never deployed to Camerlengo.

Verification: multi-turn tool tasks on the box ("create file X with ..." -> "add a line there" with
no file name -> "what did you change?"), a long conversation that crosses a compaction and still
answers from before it, and token cost per turn before/after.

## Phase 3 -- Cost estimate (before committing to the full run)

**Status: not started.**

Per explicit instruction: this is one of the early testing phases, not an afterthought reported
after money is already spent. Once Phase 1 (box) and Phase 2 (harness) exist, run a small sample
through the real harness -- one task per category (4 total), on the real disposable box, through
real sw-mode turns -- and measure actual cost from the two real meters involved:

- **SW balance spent**, read directly from the SquirrelWisdom wallet before/after the sample (same
  balance check `account_state.py` already does elsewhere in this codebase), not estimated from
  token counts.
- **Vultr instance cost** for the time actually elapsed (box's hourly rate x wall-clock time for
  Phase 0 verification + Phase 1 standup + this sample).

Extrapolate: (sample's per-task average SW cost) x (full Phase 4 task count), plus the Vultr
instance's expected total runtime for the full battery at the same hourly rate. Report both
numbers back before Phase 5 (the full run) starts, so the actual spend is a known, confirmed
quantity rather than a guess made before anything ran.

## Phase 4 -- Representative tasks per category (final task list confirmed before the run, not frozen here)

**Status: not started.**

- **Email**: read an unread message and summarize it; draft and send a reply referencing a
  specific detail from the original.
- **Documents**: create a new document with specific structured content (e.g. a short memo with a
  heading and a bullet list); open an existing one and make a targeted edit.
- **Messengers**: read recent messages on whichever channels have real credentials at run time
  (as of Phase 0: Telegram, Ratatosk) and send a reply; for channels without live credentials, only
  the Phase-0 reachability check applies (tool is callable, fails on "not logged in", not on a
  crash).
- **Code**: write a small script to a scratch directory that does something verifiable (e.g.
  parses a CSV and prints a total), run it, and confirm the real output matches.

Each task is deliberately multi-step/multi-tool (matching the original ask: "сложные многоходовые
задачи, требующие разнообразных инструментов") rather than a single tool call, since the whole
point is exercising sw-mode's own tool-use loop, not just connectivity.

## Phase 5 -- Full run and report

**Status: not started.**

Run the complete Phase 4 task list through the Phase 2 harness on the Phase 1 box, now that Phase 3
has turned the cost from a guess into a confirmed number. Produce one report: per task, per
category -- pass/fail, escalated-or-not, tools called, wall-clock time -- plus the two actual-spend
totals (SW balance, Vultr hours) for this specific run, not just the Phase 3 estimate.

## Verification

- Phase 0: done -- see the "Status: done" section above for each fix's live-verified result.
- Phase 1: confirm `supervisor.py` reaches a healthy state (`GET /status` 200) on the fresh box
  with no manual intervention, same bar `docs/LINUX_PORT_PLAN.md` already used for its own Phase 5.
- Phase 2: a smoke-test task (one simple turn) through the harness, confirming the reply/log/
  side-effect readback pipeline itself works before Phase 3 relies on it for real cost numbers.
- Phase 3: the two actual-spend numbers (SW balance delta, Vultr elapsed cost) ARE the
  verification -- reported back before Phase 5 is allowed to start.
- Phase 5: the harness's own per-task pass/fail record IS the verification -- the full report
  described above.
