# ChatSession

One `ChatSession` exists per open tab (plus headless ones for reminders, the Akinakes bus and
Ratatosk). It is the Python port of the original Node backend's `runLoop` resilience layer and the
riskiest module in the project: most of its code is incident-driven and carries a dated comment
saying which incident. Read those comments before changing behaviour.

## Composition

`chat_session.py` holds only `__init__` (the state every subsystem shares) and the class
declaration. The behaviour is split into mixins under `chat_session_mixins/`, with imports and
constants factored into `chat_session_shared.py`:

| Mixin | Responsibility |
| --- | --- |
| `engine_lifecycle` | Start/restart of the engine process, options (system prompt, MCP servers, model override), skills |
| `turn_submission` | `submit()`, the merge window, `submit_or_try_small_model`, interim notes, fast lane |
| `small_model_turn` | Running a turn through the SW cascade |
| `ladder` | Model ladder for Claude/Codex tabs (start rung, verifier, step up) |
| `permission_guard` | `turn.permission.*` microagents before a send-type tool call |
| `reply_watch` | Whether an answer is expected after a message was sent; follow-up checks |
| `dialogue_transcript` | What the user was shown, delivery corrections, repeated-question cutting |
| `delivery_report` | End-of-turn check that the user received what the work produced |
| `narration_text`, `narration_voice` | Progress remarks (text) and continuous voice narration |
| `memory_replay` | Replaying unsaved memory after a restart |
| `compaction`, `compaction_recovery` | Forced/hourly/growth compaction and recovery of a bad summary |
| `retry_reconnect` | API retry timers, MCP reconnect |
| `status_conn` | Turn-pending state and connection status shown in the header |
| `stop_control` | Stop (hard kill of the process tree), stop notes |
| `watchdog` | Hang detection and the background supervision loop |

## Lifecycle of a turn

1. **Receive.** `main.py` stores the message in `pending-turn-<tab>.json` immediately and waits
   `USER_MESSAGE_MERGE_WINDOW_S` (5 s) so quick consecutive messages become one turn.
   With `FAST_LANE_DURING_MERGE_WINDOW` the cascade may start on the draft meanwhile.
2. **Submit.** `submit()` is the single choke point. It distinguishes real user turns from
   synthetic ones (internal nudges and reminders carry `_SYNTHETIC_TURN_MARKER` so history
   recovery never shows them as the user's bubble). Turns arriving during compaction or while the
   wallet is empty are queued and replayed. Pending interim notes are prefixed to the text.
   The memory advisor (`memory_advisor.py`, microagent `sw.memory.need`) runs only for real user
   messages: when the answer may depend on what Caroline already knows, the code runs the memory
   search and attaches the result to the message.
3. **Route.** A tab on the SW source goes to `_route_sw_turn` (the cascade). Claude and Codex tabs
   push the text into the engine's input stream. The start rung comes from `rung_advice.advise()`
   (microagent `turn.rung.advice`), which never blocks a turn for longer than its timeout.
4. **Run.** The engine yields messages; tool calls dispatch through `operations.dispatch()`.
   Send-type tools are gated by `permission_policy.py` (mechanical) plus the permission
   microagents (identify / guard / sanction). The action registry keeps up to five open actions
   per tab; an approved action skips the guard.
5. **Verify and deliver.** On a ladder tab the verifier decides whether the attempt finished the
   task; if not, the same session continues on the next rung. Each outbound block passes
   `reply.review`, translation and `turn.delivery.completeness`, then is written to the
   `dialogue_log`.
6. **Finish.** The pending-turn file is deleted; `delivery_report` and the post-turn checks run;
   `action_ledger` has recorded any outbound action as a fact; memory nudges (`memory_nudge.py`)
   fire every N plugin calls without a `save_info`.

## Resilience

* **Hang detection** is process-based (`process_activity.py`): CPU, RSS and IO of the CLI process,
  not "no SDK message". Constants (`chat_session_shared.py`): hang timeout 90 s, 15 min while a
  tool is in flight, absolute ceiling 15 min, watchdog tick 5 s, start-up timeout 5 min.
* **Restart budget:** 5 restarts per 10 minutes per tab, 60 s backoff, then an API retry timer
  (90 s) that is persisted across a process restart. Authentication failures escalate after 3
  retries.
* **Failure classification** (`failure_classification.py`) is a pure set of regexes mapping an
  error text to a kind (`authentication_failed`, `server_error`, limits, billing, ...) that picks
  the recovery action.
* **Supervised loops.** Watchdog, narration and dialogue refresh run under `supervise()` which logs
  any non-cancellation exception and restarts the loop; an opt-in heartbeat catches loops that
  must run forever but got stuck.
* **Event-loop watchdog** (`loop_watchdog.py`) runs in a thread and logs the stack of what blocked
  the loop.
* **Stop** is a kill switch: `process_kill.py` kills the whole process tree with no grace period
  (a plain `taskkill /T` misses children spawned during the kill).

## Durability

`durability.py` keeps three per-tab files in the workspace: `tab-session-<id>.json` (resume id per
answer source), `pending-turn-<id>.json` (a received but unanswered turn) and
`tab-continuity-<id>.json` (pointer to the archive of a session reset as unrecoverable). Closing
the window disposes the session but leaves the pending file; the next connect of that tab starts a
resume turn (`resuming_unfinished_turn`). The guard that resumes at most once per process is
re-armed on every disconnect, so it also works when only the window (not the backend) restarted.
`history.py` rebuilds the visible transcript from the CLI's own `.jsonl` under
`~/.claude/projects/<slug>/`; `transcript_journal.py` is an append-only journal of every chat
entry so a lost browser localStorage blob can no longer erase the day.

## Context management

Claude's transcript is append-only and `/compact` never truncates it, so several layers keep
context bounded:

* **Forced compaction** — hourly and on growth (`FORCED_COMPACTION_GROWTH_TOKENS_THRESHOLD`
  400 000 tokens, minimum interval 5 min, minimum context 150 000 tokens, 30 min pause after a
  limit). A flush-memory turn precedes it when the tab has unsaved tool work.
* **Compaction guard** (`compaction_guard.py`) — a compaction whose "summary" is a sentinel such as
  `[[NO_UPDATE]]` or a greeting is detected and recovered instead of being logged as success.
* **Fallback compaction** (`compaction.py`) — local ageing-out of old context with no model call,
  used when native compaction leaves the context too large.
* **Caps** (`context_cap.py`, `pdf_pages.py`) — a tool result may never hand the model an unbounded
  amount of text; PDFs are read page by page, never as raw document bytes.
* **Archive** (`archive_chunks.py`, `archive_prune.py`, `transcript_rotate.py`) — dehydrated
  transcripts are split into addressable chunk files with an index; redundant full copies are
  removed; huge transcripts are rotated at the last `compact_boundary`.
* **Token cost.** Every request re-sends the base prompt, so tool groups and short standing
  instructions exist to keep it small (see [ENGINES.md](ENGINES.md)).

## Background work: pseudo-tabs

A fired reminder or a finished sub-agent is handled in its own invisible session
(`pseudo_tab.py`), one per activity, so the main model is not occupied by background chores and
only needs to know they exist. `background_context.py` hands such a session the conversation it
works for (it once concluded the user never asked for a task because its dialogue file was empty).
`agent_registry.py` persists which agents a tab launched, because compaction can summarise the
launch away and a backend restart silently kills every agent living in the CLI process;
`agent_definitions.py` defines the agent type Caroline launches (a sub-agent does not inherit the
main agent's appended system prompt, so the rules are repeated in its own definition).
