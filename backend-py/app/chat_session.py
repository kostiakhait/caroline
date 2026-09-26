"""One ChatSession per open tab -- the Python equivalent of server.ts's
ChatSession class. Ports the resilience/session-management layer (hang
detection, restart budget, dehydration/compaction, the failure-
classification gauntlet, the continuity-archive pointer, durability across
a full app restart) near-verbatim from server.ts's runLoop -- this is
explicitly the riskiest single piece of the whole migration, hard-won,
incident-driven logic, not re-derived.

Known, documented gaps vs. the original (tracked, not silently dropped):
- policies.ts's ~24 system-prompt instruction-builders are handled
  DIFFERENTLY here than the original, per explicit instruction
  (2026-09-09): only genuinely must-never-be-missed, tool-agnostic rules
  are unconditionally appended to every turn's system prompt (see
  policies.ALWAYS_ON_INSTRUCTIONS, 11 functions); everything tool-specific
  moved into that tool's own plugin (Plugin.usage_instructions) and is
  fetched by the model ON DEMAND via the generic get_tool_instructions
  tool (app/operations.py) -- never auto-injected. The original instead
  hardcoded all ~24 into every turn unconditionally (server.ts:1587-1616).
- No CLI-subprocess-pid reaping (Windows process-tree inspection -- the
  Python SDK's own subprocess lifecycle may already cover this; revisit
  if orphaned processes are observed live).
- (RESOLVED 2026-09-09) language detection now uses the Resolve-based
  redesign (resolve-based-language-detection.md), implemented directly here
  and in policies.py/voice_api.py -- see current_language_name/
  refresh_language_in_background below, and languageHintInstruction's TS
  twin in policies.ts (also implemented the same day).
- (RESOLVED 2026-09-09) persona.ts's personaSystemPromptAppend() is now
  ported and wired in (app/persona.py) -- was a real, live-confirmed gap,
  not hypothetical: without it, the model has no fixed identity/gender for
  itself and drifts, reproduced live as Caroline using masculine
  self-referential verbs in Russian before this fix.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TERMINAL_TASK_STATUSES,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from app import win_subprocess_patch

# Must run before the SDK's own transport ever spawns claude.exe (the very
# first ClaudeSDKClient.connect() call below does) -- see
# win_subprocess_patch.py's own docstring for why this is needed at all
# (we run under pythonw.exe, no console of its own; the vendored SDK's own
# subprocess spawn doesn't pass CREATE_NO_WINDOW, so every fresh claude.exe
# -- and dehydration forces a fresh one after EVERY turn -- flashed/held
# open a visible console window without this).
win_subprocess_patch.apply()

from app.durability import (
    _sanitize_tab_id,
    claude_project_dir,
    clear_pending_turn,
    clear_tab_continuity_archive,
    clear_tab_session_id,
    compute_foreign_tool_overlap,
    dehydrated_dir,
    find_most_recent_claude_session_id,
    load_chat_mode,
    load_discovered_foreign_tool_overlap,
    load_tab_continuity_archive,
    load_tab_session_id,
    save_discovered_foreign_tool_overlap,
    save_pending_turn,
    save_tab_continuity_archive,
    save_tab_session_id,
    session_transcript_path,
    openai_transcripts_dir,
)
from app.engines.base import AgentEngine
from app.engines.claude_engine import ClaudeEngine
from app.engines.codex_engine import CodexEngine
from app.openai_mode import build_codex_options, openai_available
from app.transcript_rotate import rotate_transcript
from app.history import _ATTACHMENT_NOTE_PREFIXES, _HISTORY_STAMP_PATTERN, iter_entries_reversed, iter_lines_reversed, read_last_context_tokens
from app.failure_classification import (
    CC_CLI_LIMIT_PATTERN,
    CLASSIFIER_REFUSAL_PATTERN,
    NOT_LOGGED_IN_PATTERN,
    OVERSIZED_MESSAGE_PATTERN,
    PROMPT_TOO_LONG_PATTERN,
    SESSION_NOT_FOUND_PATTERN,
    TOOL_CONCURRENCY_ERROR_PATTERN,
    detect_balance_exhaustion,
    extract_classifier_refusal_category,
)
from app.logging_setup import log_event
from app.login_api import is_logged_in
from app.plugins.loader import build_mcp_servers
from app.small_model_engine import run_small_model_turn
from app.task_supervisor import supervise
from app.persona import get_persona, get_persona_gender, persona_system_prompt_append
from app import agent_registry
from app.agent_definitions import caroline_agents
from app.policies import (
    ALWAYS_ON_INSTRUCTIONS, continuity_pointer_instruction, language_hint_instruction, recent_dialogue_history_instruction,
    running_agents_pointer_instruction,
)
from app.operations import REGISTRY
from app.pdf_pages import extract_pdf_page_texts
from app.process_activity import ProcessActivityMonitor
from app.session_context import set_cli_pid_sink, set_inject_proactive, set_send, set_tab_id
from app.plugins.sw_api import get_funds_exhausted_reason
from app.sw_gate import require_sw_or_prompt
from app.subscription_mode import (
    build_options_env,
    get_model_override,
    resolve_mode,
)
from app.wire import message_to_wire
from app.workspace_dir import WORKSPACE_DIR

SendFn = Callable[[dict[str, Any]], Awaitable[None]]
PRIMARY_TAB_ID = "1"

# --- constants, matching server.ts's exact values ---------------------------
HANG_TIMEOUT_MS = 90_000
STARTUP_TIMEOUT_MS = 5 * 60_000
WATCHDOG_INTERVAL_MS = 5_000
HANG_ESCALATION_GRACE_MS = 20_000
# Bug fix (2026-09-15), per explicit instruction: "90-секундный таймаут
# ЗАВИСАНИЯ применим ТОЛЬКО если не идет РЕАЛЬНОЙ работы" -- the plain
# 90s/300s hang timeout was firing against turns that were NOT actually
# hung, just doing real, legitimately slow work (a native CLI tool --
# Bash, browser evaluate, a large fetch/decode -- in flight, no new SDK
# message possible until it resolves): confirmed live via tab 4's own
# transcript (2026-09-15) as a genuine "Groundhog Day" loop -- the model
# re-verifying the same environment state from scratch every 2-4 minutes
# because this exact timer kept force-killing real, in-progress work
# before it could finish. A tool call the model just issued
# (last_tool_use_started_at) is real work by definition -- the CLI
# cannot produce another SDK message until that call returns, so silence
# during it is expected, not a hang. Give real work a much longer leash
# instead of none at all: still eventually force-closes a tool call that
# is ACTUALLY stuck forever (no legitimate tool in this app should ever
# run this long), just not at the same threshold used for genuine
# dead-air silence (no tool in flight at all -- nothing legitimate
# explains that lasting past HANG_TIMEOUT_MS).
HANG_TIMEOUT_WITH_TOOL_IN_FLIGHT_MS = 15 * 60_000
# Bug fix (2026-09-16), per explicit instruction: the process-activity
# signal (see process_activity.py) has no ceiling of its own -- a process
# showing SOME CPU/RSS/IO movement at least once every <90s is "alive" by
# that definition forever, however long the actual turn has gone
# unanswered. Confirmed live: a real turn sat pending 15.5 minutes with
# hang_count staying 0 the whole time (the process apparently kept
# showing just enough activity, likely its own periodic internal retries,
# to never trip the 90s idle check). This is a hard backstop ON TOP of
# the activity signal, not a replacement for it -- checked against
# turn_pending_since (when the CURRENT turn actually started), completely
# independent of whatever the activity monitor says.
HANG_ABSOLUTE_CEILING_MS = 15 * 60_000
# Bug fix (2026-09-14/15), per explicit instruction ("надежный рубильник",
# then "Кнопка стоп это абсолютный рубильник... Сразу по нажатии"):
# confirmed live, twice, that a user-initiated Stop could leave a turn
# permanently stuck -- client.interrupt() alone is a soft ask the CLI/SDK
# isn't guaranteed to honor (same underlying flakiness _check_hang's own
# escalation exists to work around for AUTOMATIC hang detection, see
# HANG_ESCALATION_GRACE_MS above). Originally tried interrupt() first and
# only escalated to a hard client.disconnect() after a grace period --
# replaced (see stop()/_force_stop_client's own comments) with an
# immediate hard kill, no grace period, since the polite path routinely
# didn't work anyway and the grace period was just a guaranteed delay.
# This timeout is now only an upper bound on disconnect() ITSELF (defense
# in depth against the SAME SDK-level flakiness, documented elsewhere in
# this codebase, e.g. subprocess_cli.py's close() bug, ever making the
# "hard kill" step itself hang, which would defeat the entire point of a
# reliable kill switch) -- not a wait-and-see period before acting.
STOP_ESCALATION_DISCONNECT_TIMEOUT_S = 10.0
MAX_RESTARTS_PER_WINDOW = 5
RESTART_WINDOW_MS = 10 * 60_000
RESTART_BACKOFF_MS = 60_000
API_RETRY_INTERVAL_MS = 90_000
# Per explicit instruction (2026-09-11): deliberately the SAME 90s number as
# API_RETRY_INTERVAL_MS, but a conceptually different constant -- that one
# is "keep retrying forever because the operation never even started" (a
# real rejection: no balance, or an in-flight rate-limit rejection); this
# one is "the operation genuinely completed, just check ONCE more for
# unfinished work, then stop regardless of the answer" (see
# _schedule_one_shot_followup_check). Kept separate so the two can diverge
# later without conflating what they mean.
ONE_SHOT_FOLLOWUP_CHECK_DELAY_MS = 90_000
MCP_RECONNECT_INTERVAL_MS = 15_000
# Per explicit instruction (2026-09-09): if 90s pass after a REAL user
# message with nothing sent back to them yet, nudge the model to continue/
# answer -- distinct from hang-detection (which guards against a dead
# transport) this guards against a live transport where the model itself
# went quiet without ever replying (finished a turn with no text, lost
# track mid-task, etc.). Deliberately does NOT help a fully hung/
# never-initialized query() -- a nudge queued behind a stuck input stream
# is just as stuck as the original message until hang-detection's own,
# separate recovery kicks in.
SILENT_USER_WAIT_NUDGE_MS = 90_000

# How often (at minimum) the user must see SOME comment from Caroline while
# a real turn of hers is still running -- see _check_progress_narration's
# own doc comment for why this can't be done by injecting into her own live
# session mid-turn, and generate_progress_comment (voice_api.py) for the
# actual cosmetic-comment mechanism this drives.
PROGRESS_NARRATION_INTERVAL_MS = 60_000

# Bug fix (2026-09-14): see consecutive_narration_count's own __init__
# comment for the incident this caps -- a turn genuinely stuck for many
# minutes doesn't need a fresh paraphrase of the same stale context every
# single minute; after this many in a row with no real progress, stop
# narrating until something real actually happens.
MAX_CONSECUTIVE_NARRATION_COMMENTS = 3

# Bug fix (2026-09-15), per explicit instruction: see
# _check_progress_narration's own retry-loop comment -- the small model
# backing generate_progress_comment can fail its own output contract
# several times in a row (confirmed live: 3 consecutive failures before a
# 4th attempt succeeded), each one previously costing most of a minute of
# silence even though the tick itself fired exactly on schedule. Flat, no
# backoff (matches this codebase's own no-exponential-backoff convention).
NARRATION_GENERATION_RETRY_ATTEMPTS = 3

# Bug fix (2026-09-22), per explicit instruction ("Нарратор должен
# срабатывать КАЖДУЮ МИНУТУ"): confirmed live -- a single narration
# generate+translate round-trip took 71s end to end (each leg using the
# default 30s-per-attempt/3-attempt budget every OTHER, real user-facing
# SW API call gets), so the EFFECTIVE gap between comments was "60s wait +
# however long that chain happened to take", not a clean 60s. Narration is
# cosmetic filler under a 60s promise, not worth that patience -- a short,
# narration-specific per-attempt timeout (forwarded to voice_api.py's
# generate_progress_comment/translate_text, which forward it to sw_api.
# py's _post_json) makes a slow/hung attempt fail fast so the OUTER retry
# loop above gets a real chance to try again within the same minute.
NARRATION_NETWORK_TIMEOUT_S = 6.0

# Bug fix (2026-09-22), same instruction: _check_progress_narration used
# to claim the 60s interval (last_visible_output_at) BEFORE attempting
# generation, and only on a genuine success -- meaning a run where ALL
# NARRATION_GENERATION_RETRY_ATTEMPTS failed left last_visible_output_at
# untouched, so the interval check passed again on literally the NEXT 5s
# watchdog tick, hammering the SW API every 5s during an outage instead of
# backing off at all. This is the cooldown a fully-failed attempt gets
# instead -- long enough to not hammer, short enough that the user isn't
# left with a full extra silent minute on top of the one that already
# produced nothing.
NARRATION_FAILURE_RETRY_S = 15.0

# Per explicit instruction (2026-09-13/2026-09-14): was a process-wide kill
# switch (SMALL_MODEL_ENABLED, always False) while the small-model path's
# reliability was still being diagnosed -- gpt-5.1 repeatedly writing out a
# plan with zero/incomplete tool calls, a flat wall-clock turn timeout
# killing turns that were making genuine slow progress, an orphaned worker
# thread still dispatching real tool calls (real IMAP logins) after its
# caller had already given up on it. All three fixed and confirmed live (a
# real 6-mailbox check completed cleanly in ~560s with the stall-based
# watchdog, no premature cutoff, no orphaned work) -- see small_model_engine.
# py's own STALL_TIMEOUT_S/ABSOLUTE_TURN_TIMEOUT_S/_TurnStalled comments.
# Superseded by a real, per-tab, user-facing Settings toggle instead of one
# global flag: each tab independently persists "claude" (default) or "sw"
# via durability.py's load_chat_mode/save_chat_mode, and "sw" can only be
# SET (main.py's chat_mode_set control op) when subscription_mode.py's
# chat_mode_eligible() holds -- both a real Claude subscription AND a PAID
# SquirrelWisdom balance. Nothing changes for anyone who never opens that
# toggle: every tab still starts on "claude", identical to this flag's old
# permanent False.

# How many recent user-visible dialogue lines (_read_recent_dialogue_lines)
# feed progress narration, which genuinely needs real back-and-forth
# (both speakers) to have something to react to.
RECENT_DIALOGUE_WINDOW = 12

# Bug fix (2026-09-14), per explicit instruction: language detection needs
# the user's own last N messages specifically -- "независимо от остального"
# -- not a fixed-size window of mixed dialogue lines the way narration
# above uses. A mixed window can dilute down to zero real user lines when a
# stretch of history is heavy on Caroline's own turns or synthetic/service
# text (confirmed live, 2026-09-14: after filtering out the CLI's own
# auto-compaction continuation preamble, one tab's last 80 mixed lines held
# ZERO real user messages). _read_recent_user_lines (below) scans back as
# far as it needs to in order to find this many real user lines, rather
# than being capped by an unrelated total-line budget.
LANGUAGE_DETECTION_USER_LINE_COUNT = 5

# Per explicit instruction (2026-09-14): a rolling window, in real hours,
# not messages -- see recent_dialogue_history_instruction's own docstring
# (policies.py) and _write_recent_24h_dialogue_file (below) for the full
# feature this backs.
RECENT_HISTORY_FILE_WINDOW_HOURS = 24

# A rebuilt 24h-dialogue file younger than this is reused as-is (see
# ChatSession._refresh_recent_24h_dialogue_async) -- a restart storm would
# otherwise re-parse the same transcript once per restart per tab. The hard
# timeout only exists so a wedged worker process can never linger forever.
RECENT_24H_REFRESH_MIN_AGE_S = 120
# A resumed real user question must end with a VISIBLE answer; a silent [[NO_UPDATE]] is re-asked
# at most this many times (flat, then stops -- see the result handler).
RESUMED_ANSWER_MAX_NUDGES = 3
# AssistantMessage.error kinds that are NEVER shown as a chat bubble -- the raw provider
# text stays in the log, the user gets a short status instead (see the branch in
# _run_loop). billing_error/rate_limit have their own handling; invalid_request is left
# alone deliberately: the classifier-refusal explanation and prompt-too-long recovery
# key off that text.
ENGINE_ERROR_SUPPRESSED_KINDS = ("authentication_failed", "server_error", "unknown")
RECENT_24H_REFRESH_TIMEOUT_S = 600
# At most one refresh worker process at a time across every tab.
_RECENT_24H_REFRESH_SLOT = asyncio.Semaphore(1)

# Per explicit instruction (2026-09-10): a regular, unconditional safety net
# independent of our own turn_pending bookkeeping -- confirmed live that
# bookkeeping itself can be wrong (see hang_interrupt_result_pending's own
# comment), silently dropping a real task with nothing visible to the user.
#
# Redesigned (2026-09-13), per explicit correction: this used to be a
# periodic timer (every IDLE_TASK_CHECK_INTERVAL_MS, independent of whether
# anything had actually happened) -- the user rejected that outright as
# needlessly expensive ("каждые три минуты вызывать модель просто так - это
# слишком дорого"). What was actually asked for is a check tied to the end
# of a real turn, not a clock -- see _fire_post_turn_completion_check(),
# called directly from the turn_pending setter's True->False edge, not from
# the watchdog loop at all anymore.

# Forced compaction (2026-09-11), per explicit instruction, after confirming
# live that native auto-compaction -- correctly wired (see
# _ensure_settings_file/_pre_compact_hook) -- has never actually fired even
# once in real use (0 pre_compact_archived events across the whole log,
# while one resumed tab's on-disk transcript grew to 24MB+). Most likely
# cause: the CLI's own "how much context has accumulated" tracking doesn't
# survive this engine's own frequent restarts (rate-limit retries, hang
# recovery, app relaunches), even though the on-disk RESUMED transcript
# keeps growing across every one of them -- so it may never see a long
# enough uninterrupted stretch to cross its own threshold. Rather than trust
# it to eventually notice, force it: once per process start (see
# needs_startup_compaction, set by main.py per tab), every hour, and
# whenever the on-disk session file has grown past this many bytes since
# the last forced compaction -- by sending the same "/compact" a human
# would type (live-confirmed this works through THIS engine's own
# connect()+generator wiring, content-as-block-list included, not just the
# SDK's query(str) convenience path).
FORCED_COMPACTION_HOURLY_MS = 3_600_000
# Bug fix (2026-09-15): replaced the old byte-based growth threshold (see
# tokens_at_last_forced_compaction's own __init__ comment for why bytes was
# the wrong signal) -- this is real context tokens, checked against
# last_known_context_tokens - tokens_at_last_forced_compaction. Own choice
# of value, not a measured constant: the one real compaction observed live
# fired at pre_tokens=69422 and left post_tokens=10158, so 60k of NEW
# growth since the last compaction is a reasonably generous margin below
# that (compacts again well before context gets that large again) without
# re-compacting on every small turn.
FORCED_COMPACTION_GROWTH_TOKENS_THRESHOLD = 60_000
# Minimum gap between two forced compactions on the same tab, regardless of
# which trigger fires -- keeps the three triggers from stacking (e.g. the
# hourly clock and the growth threshold both crossing within the same
# watchdog tick) into back-to-back /compact calls.
FORCED_COMPACTION_MIN_INTERVAL_MS = 300_000
# Per explicit instruction (2026-09-20), after a real, measured outage (see
# _check_forced_compaction / _note_compaction_hit_limit): a forced compaction
# that itself hits the usage cap ("You've hit your session limit · resets
# 9pm") used to flip the tab limited -> connected within a millisecond (the
# error's own trailing ResultMessage), which re-armed the post-limit retry
# with no cooldown -- a compaction every ~5 s for as long as the cap lasted
# (1,464 in a single hour), each one copying the whole transcript to disk
# (892 GB by the time it was found). After such a failure no forced
# compaction is attempted for this long. FLAT, never growing (standing rule:
# no exponential backoff anywhere) -- own choice of value, not a measured
# constant; the cap resets on the order of hours, so this is a "check again
# a couple of times per episode" cadence, not a tight loop.
FORCED_COMPACTION_LIMIT_PAUSE_MS = 30 * 60_000
# A forced compaction of a context this small has nothing to compact --
# measured live: the hourly/startup compactions on idle tabs all ran with
# 3.5K-7K tokens in context (5.7K -> 6.0K "after"), while every compaction
# that did real work started at 52K-99K. This sits clearly between the two.
# Own choice of value, not a specified one.
FORCED_COMPACTION_MIN_CONTEXT_TOKENS = 20_000

# How many consecutive "authentication_failed" system/api_retry messages on
# one connection before forcing a clean restart (re-resolves the mode and
# rebuilds Options.env) instead of retrying the same doomed request forever.
# Own choice, not a specified value.
AUTH_RETRY_ESCALATION_THRESHOLD = 3

CONTINUE_OR_SILENT_NUDGE_TEMPLATE = (
    "Check whether you actually finished what you were doing -- having called a tool is not the same thing as "
    "the work being done; a tool call only counts once you've checked its real result and confirmed it matches "
    "what was actually asked for, not just that the call was made. In particular: if your last reply described "
    "a plan (\"I'll do X, then Y\", \"let me go through them\", anything framed as about to happen) without you "
    "actually carrying every step of it out to the end in that same reply, the task is NOT done and the work is "
    "NOT finished, no matter how complete or confident that reply sounded -- continue it now, for real, using "
    "your tools, rather than repeating or restating the plan. If anything else is unfinished, unverified, or "
    "was left as a placeholder/stub rather than the real thing, continue that too. If everything genuinely is "
    "finished and verified, do nothing and reply with exactly [[NO_UPDATE]], with no explanation. Reply in "
    "{language}."
)

# Per explicit instruction (2026-09-22), after a real incident: on the OpenAI
# engine specifically -- confirmed live, and specifically NOT observed on
# Claude, so this is deliberately not in ALWAYS_ON_INSTRUCTIONS -- the model
# narrated a plausible, confident-sounding "I checked X, here's what I found"
# without ever actually calling a real tool, when a tool call had in fact
# silently failed underneath it (root cause: a missing companion binary,
# since fixed -- see CodexInstaller.cs -- but the underlying tendency to
# paper over a failed/skipped tool call with a fabricated-sounding result is
# a real, separate risk worth guarding against regardless).
OPENAI_TOOL_HONESTY_INSTRUCTION = (
    "Before reporting that you checked, read, listed, opened, or otherwise looked at something real (a file, an "
    "inbox, a screenshot, a search result, anything outside your own reasoning), confirm to yourself that a real "
    "tool call actually ran and actually returned that content -- never describe a result you did not receive "
    "from an actual tool call, even if it sounds plausible or is what you'd expect to find. If a tool call "
    "errored, timed out, or wasn't available, say so plainly instead of substituting a made-up-sounding answer."
)

# Fires once per backend-process lifetime, the moment the primary tab's
# very first turn is about to run -- Caroline must never come back up
# silently: every fresh launch or restart she should proactively say
# she's back and ready, in character (the persona system prompt -- not
# yet ported, see this module's own docstring -- already carries her
# personality/gender-agreement rules; this only supplies the language and
# the occasion, not scripted wording).
STARTUP_GREETING_NUDGE_TEMPLATE = (
    "You just started up (or restarted). Proactively greet the user in your own voice and personality -- "
    "let them know you're back online and ready to work. Keep it brief, and don't explain that this is a "
    "startup message. Greet them in {language}."
)

# Bug fix (2026-09-10): confirmed live -- this and the rate-limit text
# below were Russian, but system_notice/status-bar text is app UI chrome,
# not conversation -- it must stay in one fixed language (English) like
# the rest of the app's chrome, regardless of what language the user is
# actually talking to Caroline in.
BALANCE_EXHAUSTED_MESSAGE = (
    "Couldn't reply -- your Anthropic account is out of credits/quota. Top up at console.anthropic.com's "
    "Billing section. I'll answer automatically once it's available again."
)

# --- detect_recent_language's synthetic-text filter (ported verbatim from
# server.ts's already-fixed, 2026-09-09 version) ----------------------------
# _HISTORY_STAMP_PATTERN itself now lives in history.py (2026-09-10, see its
# own comment there) -- imported below alongside the other history helpers.
# Best-effort, not exhaustive -- new synthetic wrapper shapes keep turning
# up. Confirmed live (2026-09-09): STARTUP_GREETING_NUDGE_TEMPLATE/
# CONTINUE_OR_SILENT_NUDGE_TEMPLATE's own injected instruction text (always
# English, a directive TO the model about what language to reply in, not
# something the user actually wrote) was slipping through unfiltered --
# repeated backend restarts during live testing filled recent history with
# these English nudges, refresh_language_in_background sampled them as if
# they were the user's own words, and mis-detected/persisted "English" for
# a conversation that had genuinely been in Russian the whole time. The
# original TS (server.ts's SYNTHETIC_HISTORY_TEXT_PATTERNS) has this exact
# same gap -- not something introduced by this port.
_SYNTHETIC_HISTORY_TEXT_PATTERNS = [
    re.compile(r"^API Error:", re.IGNORECASE),
    re.compile(r"^\[System note:", re.IGNORECASE),
    re.compile(r"^\[Caroline was restarted", re.IGNORECASE),
    re.compile(r"^Continue from where you left off\.?$", re.IGNORECASE),
    re.compile(r"^Continue any unfinished work, if there is any\.", re.IGNORECASE),
    re.compile(r"^You just started up \(or restarted\)\.", re.IGNORECASE),
    # Bug fix (2026-09-10): confirmed live -- this used to be the narrow
    # "^\[Internal: automatic recheck" (one specific nudge's own text).
    # Two OTHER "[Internal: ...]" nudges (the 90s silent-user-wait nudge,
    # and the own-Anthropic-recovery nudge added the same day) weren't
    # covered at all, so their English text leaked into
    # refresh_language_in_background's sampling as if it were real recent
    # conversation -- confirmed live as the actual cause of language
    # detection flapping to/getting stuck on English mid-conversation.
    # Broadened to the whole "[Internal: ...]" convention so this can't
    # recur for whatever internal nudge gets added next either.
    re.compile(r"^\[Internal:", re.IGNORECASE),
    re.compile(r"^\[The user just stopped what you were doing", re.IGNORECASE),
    re.compile(r"^No response requested\.?$", re.IGNORECASE),
    re.compile(r"^<"),  # XML/HTML-ish wrapped system content
    # Added 2026-09-10 specifically as a bridge for entries already on disk
    # from before _SYNTHETIC_TURN_MARKER existed (_on_reminder_due's own
    # wrapper, main.py's app-closing backup nudge) -- the structural marker
    # covers these going forward without needing a list entry per nudge;
    # this pair stays only so TODAY's already-written history (which the
    # marker can't retroactively tag) doesn't keep leaking into the sample
    # until it ages out of the window on its own.
    re.compile(r"^⏰ Reminder due"),  # "⏰ Reminder due" (_on_reminder_due)
    re.compile(r"^The app is closing right now\.", re.IGNORECASE),
    # Bug fix (2026-09-14), confirmed live: this one isn't one of Caroline's
    # OWN injected nudges at all (so _SYNTHETIC_TURN_MARKER never tags it --
    # that tagging only happens at submit()'s own is_real_user=False path,
    # which this never goes through), it's the Claude Code CLI's OWN native
    # auto-compaction/session-resume mechanism writing its fixed English
    # continuation-summary preamble directly into the transcript with
    # role="user" every time a session resumes after being compacted.
    # Confirmed live: tab 1's last 22 "User:" lines were 21 copies of this
    # (repeated resumes) and exactly ONE real message -- the English
    # boilerplate completely dominated refresh_language_in_background's
    # sample and kept mis-detecting an all-Russian tab as English no matter
    # how many real Russian messages the user actually sent, since this
    # gets re-added on every single resume.
    re.compile(r"^This session is being continued from a previous conversation", re.IGNORECASE),
]

# Bug fix (2026-09-25), confirmed live: Caroline's own auto-generated attachment
# note ("[This image is also saved at <path> -- use that path...]", always English --
# see chat_session.py's _save_attachment_to_uploads) is already recognized as
# not-the-user's-own-words by history.py's _extract_attachment_note (used to keep it
# out of the rendered chat transcript), but this module's OWN "is this real user
# text" check never knew about it -- so it sailed straight into
# refresh_language_in_background's sample as if Konstantin had typed it, and a
# Russian tab's most recent "real" user line turned out to be this English
# boilerplate, contributing to persisting the wrong language for the whole tab.
# Same list, same judgment, both places now.
_ATTACHMENT_NOTE_PREFIX_TUPLE = tuple(_ATTACHMENT_NOTE_PREFIXES)


# Bug fix (2026-09-10): the patterns below are a blocklist of specific known
# nudge wordings -- confirmed live tonight this keeps missing new synthetic
# text as new nudges get added (most recently: the hourly vault-backup
# reminder's own note text, never added to this list at all) and silently
# re-poisons language detection each time. Structural fix instead of one
# more pattern: submit() tags EVERY turn it knows isn't from a real user
# (is_real_user=False -- inject_proactive() and the few direct internal
# submit() call sites) with this one fixed, permanent marker, at the single
# choke point that already knows the answer -- so any FUTURE proactive
# nudge is automatically covered, with nothing for its author to remember
# to add here (the blocklist patterns stay too, as a belt-and-suspenders
# fallback for OLD transcript entries already on disk from before this
# fix, written without the marker). U+2063 (invisible separator) means
# this never visibly renders as odd text in the rare case it ever leaked
# somewhere unfiltered.
_SYNTHETIC_TURN_MARKER = "⁣[[caroline-internal-turn]]⁣"

# Backward-compat only (2026-09-22): a same-day, since-reverted experiment
# briefly had submit() prepend a "last hour of dialogue" block directly
# into a real user message's own wire_text (per explicit instruction,
# reverted the same night once it turned out to be a real contributor to
# --append-system-prompt's own command-line-length overflow -- see
# recent_dialogue_history_instruction's own docstring for the actual,
# pointer-only fix that replaced it). Nothing generates this block anymore,
# but any turn saved to disk while the experiment was live still has it
# baked into its own transcript entry -- strips it back out (same STRIP
# shape as _HISTORY_STAMP_PATTERN: removes a prefix, keeps the real text
# after it) so those old entries don't show up duplicated/nested in a
# dialogue-window read. Safe to delete once no session transcript from
# that window is recent enough to matter anymore.
_INLINE_HOUR_CONTEXT_BLOCK_PATTERN = re.compile(
    r"^\[Context -- the real conversation between you and this user over the last hour,.*?\n\]\n\n", re.DOTALL,
)


def _is_synthetic_history_text(raw_text: str) -> bool:
    text = _HISTORY_STAMP_PATTERN.sub("", raw_text).strip()
    # Bug fix (2026-09-10): confirmed live -- a text block that is ENTIRELY
    # the "[Sent: ...]" timestamp stamp (no real content after it, e.g. the
    # stamp _push_message always prepends as its own separate content
    # block) stripped down to "" here, and "" matches none of the patterns
    # below -- so this function said "not synthetic" for a block that was
    # nothing BUT a machine-generated timestamp. Every submitted turn
    # (real or proactive) produces one of these, and they were silently
    # counted as real conversational content by every caller of this
    # function (refresh_language_in_background's sampling, most visibly --
    # confirmed live it dominated the "last 5" sample with pure-English
    # weekday/month/timezone text and kept mis-detecting an all-Russian
    # conversation as English).
    if not text:
        return True
    if text.startswith(_SYNTHETIC_TURN_MARKER):
        return True
    if text.startswith(_ATTACHMENT_NOTE_PREFIX_TUPLE):
        return True
    return any(p.match(text) for p in _SYNTHETIC_HISTORY_TEXT_PATTERNS)


_NO_UPDATE_SENTINEL = "[[NO_UPDATE]]"

# Bug fix (2026-09-15), confirmed live -- anomaly report: a "No response
# requested." bubble appeared in the real chat window, styled exactly like
# a genuine Caroline reply, then even got wire-translated into Russian
# ("Ответа не требуется.") and shown to the user as if she'd actually said
# something. Root cause: CONTINUE_OR_SILENT_NUDGE_TEMPLATE instructs the
# model to reply with EXACTLY the [[NO_UPDATE]] sentinel when it has
# nothing to add, but the model doesn't always comply with that literal
# wording -- it sometimes writes a natural-language paraphrase instead.
# _SYNTHETIC_HISTORY_TEXT_PATTERNS already recognized this exact wording
# (filtered from language-detection history sampling, confirming it's a
# known/recurring shape), but nothing stripped it from the WIRE the user
# actually sees -- only the literal sentinel was (_strip_no_update_from_
# wire below). Treat a whole assistant text block that's ENTIRELY one of
# these known "I have nothing to say" paraphrases the same as the
# sentinel itself: never user-visible, whatever the model worded it as.
# Whole-text match (not substring, unlike the sentinel check) -- these are
# ordinary short sentences that could theoretically appear as a genuine
# fragment inside a longer real reply, so only suppress a block that is
# NOTHING BUT one of these.
_SILENT_REPLY_PARAPHRASE_PATTERNS = [
    re.compile(r"^no response (is )?(requested|needed|required)\.?$", re.IGNORECASE),
    re.compile(r"^nothing (further |else )?to (add|report|update|say)\.?$", re.IGNORECASE),
    re.compile(r"^no (further )?(update|action) (is )?(needed|required)\.?$", re.IGNORECASE),
]


def _is_silent_reply_paraphrase(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return any(p.match(stripped) for p in _SILENT_REPLY_PARAPHRASE_PATTERNS)


def _strip_no_update_from_wire(wire: dict[str, Any]) -> dict[str, Any] | None:
    """Per explicit instruction (2026-09-10): NOTHING containing the
    [[NO_UPDATE]] sentinel (see no_update_sentinel_instruction /
    CONTINUE_OR_SILENT_NUDGE_TEMPLATE) may ever reach the user-visible
    dialog -- filter it out server-side here, not only in chat.js, so an
    old/cached client can't leak it either. Substring match, not exact
    equality: the model doesn't always reply with ONLY the sentinel.
    Also strips a block that's nothing but a known non-compliant
    paraphrase of the same "nothing to say" outcome (see
    _SILENT_REPLY_PARAPHRASE_PATTERNS' own comment, 2026-09-15).
    Returns the wire with offending text blocks removed, or None if that
    empties an assistant message of everything worth showing."""
    kind = wire.get("type")
    if kind == "assistant":
        content = wire.get("message", {}).get("content", [])
        kept = [
            b for b in content
            if not (isinstance(b, dict) and b.get("type") == "text"
                    and isinstance(b.get("text"), str)
                    and (_NO_UPDATE_SENTINEL in b["text"] or _is_silent_reply_paraphrase(b["text"])))
        ]
        if len(kept) == len(content):
            return wire
        if not any(isinstance(b, dict) and b.get("type") in ("text", "tool_use") for b in kept):
            return None  # nothing left the user should see
        wire["message"]["content"] = kept
        return wire
    if kind == "result":
        result_text = wire.get("result")
        if isinstance(result_text, str) and (_NO_UPDATE_SENTINEL in result_text or _is_silent_reply_paraphrase(result_text)):
            wire["result"] = ""
        return wire
    return wire


def _last_language_path(tab_id: str) -> Path:
    # Bug fix (2026-09-10): this used to be ONE file shared by every open
    # tab -- confirmed live, with 3 tabs open, whichever tab's background
    # language-refresh finished last silently overwrote the language for
    # ALL of them, including tabs having a completely unrelated
    # conversation in a different language. Same per-tab-file pattern as
    # tab-session-<id>.json etc. in durability.py.
    return Path(WORKSPACE_DIR) / f"last-language-{_sanitize_tab_id(tab_id)}.json"


def _load_persisted_language(tab_id: str) -> str | None:
    try:
        data = json.loads(_last_language_path(tab_id).read_text(encoding="utf-8"))
        lang = data.get("lang")
        return lang.strip() if isinstance(lang, str) and lang.strip() else None
    except Exception:
        return None


def _save_persisted_language(tab_id: str, lang: str) -> None:
    try:
        _last_language_path(tab_id).write_text(json.dumps({"lang": lang}, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        log_event("engine", "save_persisted_language_failed", tab_id=tab_id, error=str(exc))


# --- attachments -------------------------------------------------------
# Images/PDFs go inline as content blocks (immediate vision/document
# reading, no extra tool round-trip) -- but EVERY attachment also gets
# saved to workspace/uploads/ under a uuid-prefixed name, so there's
# always a stable, collision-free file to point the model at for anything
# beyond just looking (saving it somewhere permanent, attaching it to an
# email, etc.) -- a received attachment's bytes are otherwise only ever
# reachable via pending-turn-<tabId>.json, an internal crash-recovery file
# that gets overwritten on every subsequent submit().
_SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# How much extracted PDF page text an attachment is allowed to inline
# directly into the turn before being cut off in favor of pointing the
# model at read_document_pages instead (2026-09-15, see
# _attachment_to_blocks' own comment) -- deliberately smaller than
# RECENT_CONTENT_BUDGET_BYTES's 50KB (dehydration's OWN budget for
# recently-added tool content, a different concern): this is TEXT the
# model is guaranteed to actually see in full on this very turn, not
# content that ages out gracefully later.
_ATTACHMENT_PDF_INLINE_CHAR_BUDGET = 40_000


def _uploads_dir() -> Path:
    return Path(WORKSPACE_DIR) / "uploads"


def _save_attachment_to_uploads(attachment: dict[str, Any]) -> str:
    directory = _uploads_dir()
    directory.mkdir(parents=True, exist_ok=True)
    saved_path = directory / f"{uuid_mod.uuid4()}-{attachment.get('name', 'attachment')}"
    saved_path.write_bytes(base64.b64decode(attachment["dataBase64"]))
    return str(saved_path)


def _attachment_to_blocks(attachment: dict[str, Any]) -> list[dict[str, Any]]:
    mime_type = attachment.get("mimeType")
    if mime_type in _SUPPORTED_IMAGE_TYPES:
        saved_path = _save_attachment_to_uploads(attachment)
        return [
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": attachment["dataBase64"]}},
            {
                "type": "text",
                "text": f"[This image is also saved at {saved_path} -- use that path (e.g. to copy/move it somewhere "
                "permanent) instead of reading backend-internal files like pending-turn-*.json for attachment "
                "bytes; those are ephemeral crash-recovery state, get overwritten by the next message, and are "
                "not a reliable way to retrieve what you were just sent.]",
            },
        ]
    if mime_type == "application/pdf":
        saved_path = _save_attachment_to_uploads(attachment)
        # Bug fix (2026-09-15), per explicit instruction: "Большие
        # многостраничные документы должны анализировать по частям...
        # Никогда документ целиком" -- this used to send the whole PDF as
        # one raw "document" content block (the API/CLI then decides how
        # much of it to actually look at, with zero page-level control on
        # our side -- the same failure shape as the 41-image context-bloat
        # incident this same day, for documents instead of images). Parse
        # per-page via app/pdf_pages.py and hand the model TEXT ONLY, page
        # by page, capped inline -- a document too large to fit the cap
        # gets its first pages inline plus a pointer at read_document_pages
        # (files_plugin.py) for the rest, so a huge document still never
        # arrives in context all at once.
        try:
            page_texts, total_pages = extract_pdf_page_texts(base64.b64decode(attachment["dataBase64"]))
        except Exception as exc:
            return [{
                "type": "text",
                "text": f"[Attached PDF saved to {saved_path} -- could not extract its page text ({exc}); "
                f"use read_document_pages(path=\"{saved_path}\") if you need its content.]",
            }]
        included: list[str] = []
        included_chars = 0
        for i, text in enumerate(page_texts):
            block = f"--- Page {i + 1} ---\n{text or '[no extractable text on this page]'}"
            if included and included_chars + len(block) > _ATTACHMENT_PDF_INLINE_CHAR_BUDGET:
                break
            included.append(block)
            included_chars += len(block)
        header = (
            f"[Attached PDF, {total_pages} page(s), also saved at {saved_path} -- parsed page-by-page, "
            "text only, never sent to you as raw document bytes.]"
        )
        if len(included) < total_pages:
            header += (
                f" Showing pages 1-{len(included)} inline; the rest wasn't included to avoid dumping the "
                f"whole document into context at once -- call read_document_pages(path=\"{saved_path}\", "
                f"page_start={len(included) + 1}) to continue reading further pages, a range at a time, "
                "if you actually need them."
            )
        return [{"type": "text", "text": header + "\n\n" + "\n\n".join(included)}]
    saved_path = _save_attachment_to_uploads(attachment)
    return [{"type": "text", "text": f"[Attached file saved to {saved_path} -- read it if relevant to the request.]"}]


def _usable_dialogue_lines(entries: list[dict[str, Any]], min_ts_ms: float | None = None) -> list[str]:
    """Turns _extract_entries_from_jsonl's raw entries into clean
    "Speaker: text" lines -- real, user-visible conversation only. Drops
    anything synthetic/service (_is_synthetic_history_text, which now
    covers both the old wording-blocklist and the new structural
    _SYNTHETIC_TURN_MARKER tag) and bare bracketed placeholder lines (a
    lone "[<timestamp>]" with nothing else, a compaction stub). min_ts_ms
    (added 2026-09-14, for _write_recent_24h_dialogue_file) optionally
    drops any entry older than that epoch-ms cutoff -- entries missing a
    real timestamp fall back to "now" (see _extract_entries_from_jsonl),
    so they're never wrongly dropped as too old."""
    out: list[str] = []
    for entry in entries:
        if min_ts_ms is not None and entry.get("ts", float("inf")) < min_ts_ms:
            continue
        raw_text = str(entry.get("text") or "")
        # Strip the "[Sent: ...]" stamp, then (see its own comment) the
        # RECENT_HOUR_INLINE_WINDOW_HOURS context block submit() may have
        # prepended -- a real user turn's OWN words always follow either,
        # never something to lose, just not something to re-inline into a
        # LATER window's own read of this same turn.
        clean_text = _INLINE_HOUR_CONTEXT_BLOCK_PATTERN.sub("", _HISTORY_STAMP_PATTERN.sub("", raw_text)).strip()
        if not clean_text or _is_synthetic_history_text(raw_text):
            continue
        if clean_text.startswith("[") and clean_text.endswith("]") and "\n" not in clean_text:
            continue
        speaker = "User" if entry.get("role") == "user" else "Caroline"
        out.append(f"{speaker}: {clean_text}")
    return out


def _recent_usable_lines(
    path: Path, *, limit: int | None = None, min_ts_ms: float | None = None, user_only: bool = False,
) -> list[str]:
    """The newest usable dialogue lines of a transcript, oldest-first --
    exactly what `_usable_dialogue_lines(<every entry of the whole file>)`
    (then a [-limit:] slice, then optionally "User: " lines only) used to
    compute by parsing ALL of it (see history.py's tail-reading comment for
    the outage that made that unacceptable). Walks the transcript newest ->
    oldest via history.iter_entries_reversed and stops as soon as it has
    `limit` lines, or (with min_ts_ms) once it's past everything that new --
    so the cost follows how much is asked for, never how big the file is.
    Filtering is per-entry (_usable_dialogue_lines has no cross-entry
    context), which is what makes stopping early equivalent. user_only
    returns the text WITHOUT its "User: " prefix, same as before."""
    out: list[str] = []
    for entry in iter_entries_reversed(path, str(path), min_ts_ms=min_ts_ms):
        usable = _usable_dialogue_lines([entry], min_ts_ms)
        if not usable:
            continue
        line = usable[0]
        if user_only:
            if not line.startswith("User: "):
                continue
            line = line[len("User: "):]
        out.append(line)
        if limit is not None and len(out) >= limit:
            break
    out.reverse()
    return out


def _recent_usable_lines_across_sessions(paths: list[Path], min_ts_ms: float | None) -> list[str]:
    """Like _recent_usable_lines over several transcripts at once (the
    different engines a tab has used), merged oldest-first by each entry's own
    timestamp -- so a conversation continues seamlessly across an engine switch."""
    items: list[tuple[float, str]] = []
    for path in paths:
        for entry in iter_entries_reversed(path, str(path), min_ts_ms=min_ts_ms):
            usable = _usable_dialogue_lines([entry], min_ts_ms)
            if usable:
                items.append((entry["ts"], usable[0]))
    items.sort(key=lambda item: item[0])
    return [line for _, line in items]


def _read_recent_dialogue_lines(
    session_id: str | None, tab_id: str, workspace_dir: str, limit: int, min_ts_ms: float | None = None,
) -> list[str]:
    """Bug fix (2026-09-10): the real, single source of "what the user
    actually saw" -- replaces two independently-maintained readers
    (refresh_language_in_background's own hand-rolled block walker, and
    _gather_recent_dialogue_for_narration's private _usable_lines) that had
    already drifted apart (the narration one had already learned the
    NO_UPDATE/synthetic-text lessons; language detection hadn't, and
    confirmed live that gap let internal noise -- bare "[Sent: ...]"
    timestamp stamps, an hourly reminder's own English wording -- dominate
    a too-small 5-message sample and silently kept mis-detecting an
    entirely-Russian conversation as English). Widened to a real, much
    larger window (2026-09-10, per explicit instruction) since narrow
    sampling was itself part of the problem. Falls back to this tab's own
    continuity-archive file (the pre-compaction transcript the PreCompact
    hook saved, per-tab, never shared) when the live session file alone is
    too thin, e.g. right after a native auto-compaction.

    Bug fix (2026-09-16), per explicit instruction: "нарратор должен
    комментировать только текущую задачу, а не весь предыдущий диалог" --
    min_ts_ms (was already accepted by _usable_dialogue_lines itself, just
    never threaded through this wrapper) lets a caller exclude anything
    from BEFORE a given wall-clock cutoff -- see
    _gather_recent_dialogue_for_narration's own use of this, passing the
    current real user turn's own start time, so an old already-finished
    task never bleeds into what the narrator reacts to. None (the
    default) keeps every other caller's existing behavior unchanged."""
    lines: list[str] = []
    if session_id:
        path = session_transcript_path(workspace_dir, session_id)
        try:
            lines = _recent_usable_lines(path, limit=limit, min_ts_ms=min_ts_ms)
        except Exception as exc:
            log_event("engine", "recent_dialogue_read_failed", tab_id=tab_id, error=str(exc))
    if len(lines) < limit:
        archive_path = load_tab_continuity_archive(workspace_dir, tab_id)
        if archive_path:
            try:
                lines = _recent_usable_lines(Path(archive_path), limit=limit, min_ts_ms=min_ts_ms) + lines
            except Exception as exc:
                log_event("engine", "recent_dialogue_archive_read_failed", tab_id=tab_id, path=archive_path, error=str(exc))
    return lines[-limit:]


def _read_recent_user_lines(session_id: str | None, tab_id: str, workspace_dir: str, count: int) -> list[str]:
    """Per explicit instruction (2026-09-14, see LANGUAGE_DETECTION_USER_
    LINE_COUNT's own docstring): the last `count` real user messages
    specifically, independent of how much Caroline/synthetic text sits
    between them -- NOT _read_recent_dialogue_lines's fixed mixed-line
    window, which a Caroline-heavy or synthetic-heavy stretch of history
    can dilute down to zero real user lines. Reuses the exact same sources
    (session file, then the tab's own continuity archive if that alone
    isn't enough) and the exact same synthetic-text filtering
    (_usable_dialogue_lines/_is_synthetic_history_text) -- only the
    windowing differs: filter to the user's own lines FIRST, THEN take the
    last `count`, so the scan naturally reaches back as far as it needs to
    instead of being capped by an unrelated total-line budget."""

    user_lines: list[str] = []
    if session_id:
        path = session_transcript_path(workspace_dir, session_id)
        try:
            user_lines = _recent_usable_lines(path, limit=count, user_only=True)
        except Exception as exc:
            log_event("engine", "recent_user_lines_read_failed", tab_id=tab_id, error=str(exc))
    if len(user_lines) < count:
        archive_path = load_tab_continuity_archive(workspace_dir, tab_id)
        if archive_path:
            try:
                user_lines = _recent_usable_lines(Path(archive_path), limit=count, user_only=True) + user_lines
            except Exception as exc:
                log_event("engine", "recent_user_lines_archive_read_failed", tab_id=tab_id, path=archive_path, error=str(exc))
    return user_lines[-count:]


def _gather_recent_usable_lines(session_id: str | None, tab_id: str, workspace_dir: str, cutoff_ms: float) -> list[str]:
    """Used by _write_recent_24h_dialogue_file -- every engine this tab has
    used, not just the current one, plus the tab's own continuity archive
    for anything a compaction already aged out of the live file within the
    window. See _write_recent_24h_dialogue_file's own docstring for why
    switching a tab between Claude and OpenAI must not make the new engine
    start blind."""
    lines: list[str] = []
    session_ids = [session_id, *(load_tab_session_id(workspace_dir, tab_id, kind) for kind in ("claude", "openai"))]
    paths: list[Path] = []
    for sid in dict.fromkeys(i for i in session_ids if i):
        path = session_transcript_path(workspace_dir, sid)
        if path.exists() and path not in paths:
            paths.append(path)
    try:
        lines = _recent_usable_lines_across_sessions(paths, cutoff_ms)
    except Exception as exc:
        log_event("engine", "recent_lines_read_failed", tab_id=tab_id, error=str(exc))
    archive_path = load_tab_continuity_archive(workspace_dir, tab_id)
    if archive_path:
        try:
            lines = _recent_usable_lines(Path(archive_path), min_ts_ms=cutoff_ms) + lines
        except Exception as exc:
            log_event("engine", "recent_lines_archive_read_failed", tab_id=tab_id, path=archive_path, error=str(exc))
    return lines


def _recent_24h_dialogue_path(workspace_dir: str, tab_id: str) -> Path:
    return Path(workspace_dir) / f"recent-24h-dialogue-{_sanitize_tab_id(tab_id)}.txt"


def _write_recent_24h_dialogue_file(session_id: str | None, tab_id: str, workspace_dir: str) -> str:
    """Per explicit instruction (2026-09-14): see recent_dialogue_history_
    instruction's own docstring (policies.py) for the full feature this
    backs. Gathers real dialogue (_usable_dialogue_lines -- both speakers,
    real content, synthetic/service text already dropped) from the last
    RECENT_HISTORY_FILE_WINDOW_HOURS, from the same two sources
    _read_recent_dialogue_lines/_read_recent_user_lines already draw from
    (the live session file, plus this tab's own continuity archive for
    anything a compaction already aged out of the live file within the
    window). Returns the file's own path unconditionally (even on a
    read/write failure -- an empty or stale file is still a valid, if
    unhelpful, thing to point the model at; a missing return value would
    just make the pointer instruction silently vanish instead).

    Bug fix (2026-09-20), confirmed live: this used to read and parse the
    ENTIRE session transcript synchronously, on the asyncio event loop --
    an outage once sessions reached hundreds of MB (see history.py's
    tail-reading comment). Now (1) reads only the last 24 hours (cost
    follows the window, not the file size) and (2) is meant to be called
    OFF the event loop -- ChatSession._schedule_recent_24h_dialogue_refresh
    runs it in a worker thread; the path it returns never changes for a
    tab, so the system prompt only ever needs the path, not this call's
    completion. The file is written atomically (temp file + rename) so the
    model can never read a half-written one while a refresh is in flight."""
    out_path = _recent_24h_dialogue_path(workspace_dir, tab_id)
    cutoff_ms = (time.time() - RECENT_HISTORY_FILE_WINDOW_HOURS * 3600) * 1000
    lines = _gather_recent_usable_lines(session_id, tab_id, workspace_dir, cutoff_ms)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(lines) if lines else "(No real messages between you and this user in the last 24 hours.)"
        tmp_path = out_path.with_name(out_path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp_path.write_text(body, encoding="utf-8")
        os.replace(tmp_path, out_path)
    except Exception as exc:
        log_event("engine", "recent_24h_dialogue_write_failed", tab_id=tab_id, error=str(exc))
    return str(out_path)


def _append_small_model_turn_to_session(workspace_dir: str, tab_id: str, session_id: str | None, user_text: str, assistant_text: str) -> None:
    """Bug fix (2026-09-12), per explicit instruction: the small-model
    primary path (small_model_engine.py) never talks to the Claude Code CLI
    at all, so its own Q&A never reached the ONE place
    _read_recent_dialogue_lines (above) actually reads from -- the on-disk
    session .jsonl. Confirmed live: a tab that answered via the small model
    once, then got asked about "our dialogue" again, could only see
    whatever the last real SDK turn had written, not its own most recent
    reply. Both engines answer the SAME conversation and get used
    interchangeably turn by turn, so both must write to the SAME durable
    history, not two that silently diverge.

    Appends a real user+assistant entry pair in the exact shape Claude
    Code's own CLI writes (type/uuid/parentUuid/timestamp/sessionId/
    message), chained onto whatever the last entry in the file already
    was -- so a LATER full-SDK turn that resumes this same session_id sees
    an unbroken, valid history, and _read_recent_dialogue_lines picks this
    up immediately via the exact same parser (_extract_entries_from_jsonl)
    a real CLI-written entry would produce.

    No-op (logged, not raised) if this tab has never run a single SDK turn
    yet -- session_id is None, so there is no session FILE to append onto
    (inventing one here risks a format Claude Code itself might not accept
    on a future resume -- Claude Code's own CLI, not Caroline, owns minting
    a new session id and creating that file in the first place). Once any
    real SDK turn has run once for this tab, every small-model turn after
    that appends correctly from then on."""
    if not session_id:
        log_event("engine", "small_model_turn_not_persisted_no_session", tab_id=tab_id)
        return
    path = claude_project_dir(workspace_dir) / f"{session_id}.jsonl"
    if not path.exists():
        log_event("engine", "small_model_turn_not_persisted_no_session_file", tab_id=tab_id, path=str(path))
        return
    try:
        last_uuid: str | None = None
        # Only the LAST line matters -- read it from the end of the file
        # (history.iter_lines_reversed), never the whole transcript (which
        # can be hundreds of MB; see history.py's tail-reading comment).
        for raw_line in iter_lines_reversed(path):
            if not raw_line.strip():
                continue
            try:
                last_uuid = json.loads(raw_line).get("uuid")
            except Exception:
                last_uuid = None
            break
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        user_uuid = str(uuid_mod.uuid4())
        assistant_uuid = str(uuid_mod.uuid4())
        user_entry = {
            "type": "user", "uuid": user_uuid, "parentUuid": last_uuid,
            "timestamp": now_iso, "sessionId": session_id,
            "message": {"role": "user", "content": user_text},
        }
        assistant_entry = {
            "type": "assistant", "uuid": assistant_uuid, "parentUuid": user_uuid,
            "timestamp": now_iso, "sessionId": session_id,
            "message": {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]},
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(user_entry, ensure_ascii=False) + "\n")
            f.write(json.dumps(assistant_entry, ensure_ascii=False) + "\n")
        log_event("engine", "small_model_turn_persisted", tab_id=tab_id, session_id=session_id)
    except Exception as exc:
        log_event("engine", "small_model_turn_persist_failed", tab_id=tab_id, error=str(exc))


def current_language_name(tab_id: str) -> str:
    """Redesign (2026-09-09, see the resolve-based-language-detection plan):
    synchronous, instant, no network call -- just whatever was last
    actually resolved (see refresh_language_in_background), or "English" if
    nothing has been persisted yet. Deliberately replaces the old
    detect_recent_language(), which raced a real API call against a
    3-second timeout and lost that race 100% of the time under real load
    on the TS side (confirmed live, 2026-09-09) -- ported here directly
    rather than porting that same bug first. Per-tab (2026-09-10 fix, see
    _last_language_path's own docstring) -- one tab's language never
    leaks into another's."""
    return _load_persisted_language(tab_id) or "English"


def refresh_language_in_background(session_id: str | None, tab_id: str) -> None:
    """Fire-and-forget: gathers the last LANGUAGE_DETECTION_USER_LINE_COUNT
    real user messages (_read_recent_user_lines -- see its own docstring
    for why this is a dedicated user-only reader, not
    _read_recent_dialogue_lines's mixed-speaker window) and asks
    resolve_user_language (Camerlengo's ai:resolve, NOT ai:detectLanguage)
    what language the user is actually writing in. Never awaited by any
    caller and carries no timeout of its own beyond resolve_user_language's
    own leak-prevention ceiling -- whatever it manages to persist simply
    becomes visible on the NEXT query() construction via
    language_hint_instruction/current_language_name, for THIS SAME tab
    only.

    History (why this isn't _read_recent_dialogue_lines anymore, 2026-09-14):
    that shared reader used to feed this function a fixed-size window of
    MIXED "User: "/"Caroline: " lines, and this function filtered to the
    user's own lines only AFTER that window was already cut -- correct in
    principle (Caroline's own output no longer skews detection, confirmed
    live), but the fixed total-line window itself could still dilute down
    to zero (or too few) real user lines whenever a stretch of history was
    heavy on Caroline's own turns or synthetic/service text (confirmed
    live: one tab's last 80 mixed lines held zero usable user lines after
    filtering out the Claude Code CLI's own auto-compaction continuation
    preamble, which -- unlike Caroline's own nudges -- isn't tagged by
    _SYNTHETIC_TURN_MARKER and had been silently dominating the sample).
    Per explicit instruction: take the user's last N messages specifically,
    independent of everything else -- _read_recent_user_lines scans back as
    far as it needs to for that, instead of being capped by an unrelated
    total-line budget shared with narration's own, differently-shaped need
    (real back-and-forth, both speakers)."""
    from app.plugins.voice_api import resolve_user_language

    async def _run() -> None:
        try:
            user_lines = _read_recent_user_lines(session_id, tab_id, WORKSPACE_DIR, LANGUAGE_DETECTION_USER_LINE_COUNT)
            if not user_lines:
                return
            name = await resolve_user_language("\n".join(user_lines))
            if name:
                log_event("engine", "language_resolved", tab_id=tab_id, language=name)
                _save_persisted_language(tab_id, name)
        except Exception as exc:
            log_event("engine", "refresh_language_in_background_failed", error=str(exc))

    asyncio.ensure_future(_run())


# Bug fix (2026-09-10): confirmed live -- ClaudeAgentOptions.settings is a
# PATH TO A SETTINGS JSON FILE ("Equivalent to the --settings CLI flag"),
# not raw JSON content. The previous code passed
# json.dumps({"autoCompactEnabled": True}) directly as this value, which
# the CLI would have tried to open as a literal filename -- so
# autoCompactEnabled was never actually communicated to the CLI at all,
# in either direction, ever (this exact same bug already existed
# pre-Stage-B, where the intent was the opposite: explicitly disabling it
# for non-own-anthropic sources). Confirmed via logs: the PreCompact hook
# never fired even once after this session's own native-compaction
# rollout, while one real tab's session file grew unbounded to 6.5MB+.
# Real fix: write an actual settings.json file once and pass its real
# path. Content is static, so this only needs to happen once per
# workspace, not per query() -- re-checked cheaply every call in case the
# file is ever missing (a fresh install, a cleared workspace).
_SETTINGS_FILE_NAME = "caroline-settings.json"
_SETTINGS_FILE_CONTENT = json.dumps({"autoCompactEnabled": True})

# Bug fix (2026-09-14), per explicit instruction: "caroline-browser" is a
# leftover from the old Node backend (workspace.ts's ensureWorkspace()) --
# a real, separate Playwright/Node browser process, still registered as a
# user-scope MCP server in ~/.claude.json (never removed during the Python
# rewrite), so the `claude` CLI subprocess this class spawns picks it up
# automatically regardless of what this backend's own mcp_servers dict
# contains. A hard, hardcoded disallowed_tools exclusion (one exact tool
# name per tool caroline-browser exposes) used to live here for this one
# specific external server.
#
# REMOVED (2026-09-18), per an emphatic, direct architectural correction:
# hardcoding a specific external server's tool names into this file is
# EXACTLY the anti-pattern this correction was about -- Caroline is a
# product installed on many different machines, each with its own,
# potentially completely different set of independently-registered MCP
# servers; caroline-browser being the one that happened to cause trouble
# on THIS machine doesn't mean it exists, or is the only offender, on any
# other install. The general fix now lives entirely in policies.py's
# prefer_own_backend_tools_instruction + operations.py's
# describe_own_backend (built fresh every turn from this install's own
# actual plugin set, never a fixed list) -- prompt-level only, no
# server/tool name hardcoded anywhere. This is a deliberate trade-off,
# stated explicitly rather than silently: SDK-enforced disallowed_tools
# is stronger than prompting alone (confirmed live, 2026-09-13, that
# prompting alone previously wasn't enough for this exact case) -- but a
# hardcoded, install-specific enforcement list was explicitly rejected as
# worse than that risk, not weighed as safe to keep alongside the general
# fix.


def clear_tab_disk_state(workspace_dir: str, tab_id: str) -> None:
    """Per explicit instruction (2026-09-17): the on-disk half of a full,
    deliberate, user-confirmed tab wipe (see ChatSession.clear_tab for the
    in-memory/live-client half) -- deletes the real session transcript
    file itself, not just the pointer to it (unlike
    _reset_unrecoverable_session, which archives the old transcript and
    keeps it reachable, this is a genuine delete with nothing kept). A
    free function (not a ChatSession method) so main.py's control handler
    can call it even when no live ChatSession exists yet for this tab
    (the tab was never opened this process lifetime) -- the durability
    files are addressed by workspace_dir+tab_id alone, no live object
    needed."""
    old_session_id = load_tab_session_id(workspace_dir, tab_id)
    if old_session_id:
        try:
            (claude_project_dir(workspace_dir) / f"{old_session_id}.jsonl").unlink(missing_ok=True)
        except Exception as exc:
            log_event("engine", "clear_tab_delete_session_file_failed", tab_id=tab_id, error=str(exc))
    openai_thread_id = load_tab_session_id(workspace_dir, tab_id, "openai")
    if openai_thread_id:
        try:
            (openai_transcripts_dir(workspace_dir) / f"{openai_thread_id}.jsonl").unlink(missing_ok=True)
        except Exception as exc:
            log_event("engine", "clear_tab_delete_openai_transcript_failed", tab_id=tab_id, error=str(exc))
    clear_tab_session_id(workspace_dir, tab_id, engine=None)
    clear_tab_continuity_archive(workspace_dir, tab_id)
    clear_pending_turn(workspace_dir, tab_id)
    log_event("engine", "clear_tab_disk_state_done", tab_id=tab_id, old_session_id=old_session_id)


def _ensure_settings_file(workspace_dir: str) -> str:
    path = Path(workspace_dir) / _SETTINGS_FILE_NAME
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != _SETTINGS_FILE_CONTENT:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_SETTINGS_FILE_CONTENT, encoding="utf-8")
    except Exception as exc:
        log_event("engine", "settings_file_write_failed", error=str(exc))
    return str(path)


@dataclass
class ToolOutcome:
    """Ground truth for one tool this real-user episode has called, keyed by
    tool NAME (not call id -- see turn_tool_outcomes' own comment for why):
    what the tool actually returned, independent of whatever the model's own
    closing narration claims. Populated purely by reading ToolUseBlock/
    ToolResultBlock pairs already on the wire (see the top of _run_loop's
    per-message handling) -- works identically for Claude and OpenAI, since
    both engines produce the exact same SDK message shapes here."""

    name: str
    is_error: bool
    result_preview: str


def _format_exception_chain(exc: BaseException) -> str:
    """Per explicit instruction (2026-09-23), after a real incident: a
    spawn failure logged only as `str(exc)` on a wrapped SDK exception
    (e.g. CLINotFoundError -- "Claude Code not found at: <path>") hid the
    ACTUAL underlying OS-level error (a plain FileNotFoundError from
    anyio.open_process, itself possibly wrapping a real Windows error
    code/errno) that would have said WHY the spawn failed, not just THAT
    it failed. `raise CLINotFoundError(...) from e` keeps that original
    exception reachable via __cause__ -- this walks the whole chain
    (__cause__ first, since that's an explicit "caused by", then
    __context__ for an implicit one) and renders every link with its own
    type name, message, and errno/winerror if it has one, so a bare
    top-level message never again hides the actual, actionable cause. Used
    anywhere an exception gets logged as context for a real failure (not
    every single try/except in this file -- see this function's own call
    sites for which ones actually matter for diagnosing a live incident)."""
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        detail = f"{type(current).__name__}: {current}"
        errno = getattr(current, "errno", None)
        winerror = getattr(current, "winerror", None)
        if errno is not None or winerror is not None:
            detail += f" (errno={errno}, winerror={winerror})"
        parts.append(detail)
        current = current.__cause__ or current.__context__
    return " <- caused by: ".join(parts)


class ChatSession:
    def __init__(self, tab_id: str, workspace_dir: str, send: SendFn) -> None:
        self.tab_id = tab_id
        self.workspace_dir = workspace_dir
        self.send = send

        self.client: AgentEngine | None = None
        # Which engine the current/next query() runs on: "openai" only while the
        # tab is switched to OpenAI AND it is actually usable, else "claude".
        self.engine_kind: str = "claude"
        self.engine_switch_pending = False
        self.ended = False
        self.user_stop_requested = False

        # queue / turn plumbing
        self.queue: list[dict[str, Any]] = []
        self._queue_event = asyncio.Event()
        self.turn_is_voice = False
        self.classifier_refusal_retry_count = 0
        # Backing field for the turn_pending property (defined below,
        # outside __init__) -- set directly here, not via self.turn_pending
        # = False, so construction doesn't trigger a premature status
        # publish before start() has even run.
        self._turn_pending = False
        self.turn_pending_since: float | None = None
        self._last_known_funds_exhausted_reason: str | None = None
        self.pending_user_text: str | None = None
        self.pending_is_real_user: bool = False
        self.pending_attachments: list[Any] = []

        # Per explicit instruction (2026-09-13), after a real incident: a
        # PROACTIVE turn (a scheduled mailbox check, here) can complete
        # with genuinely EMPTY visible content -- confirmed live via the
        # raw session transcript, a real final assistant message with
        # stop_reason="end_turn" and real spent output tokens, but its
        # content array held only an empty thinking block, no text at all
        # (not even a [[NO_UPDATE]] sentinel -- that's a real text block,
        # just client-suppressed, and would count as "visible" here).
        # _fire_post_turn_completion_check() previously only ever fired for
        # REAL user turns (see its own docstring for why proactive turns
        # were excluded -- most proactive completions are LEGITIMATELY
        # silent via NO_UPDATE, and re-nudging every single one would be
        # wasteful). turn_saw_any_visible_text closes exactly the gap
        # between "legitimately silent" and "silently lost real content":
        # reset per-turn in submit(), set True the moment any assistant
        # wire message carries real text (NO_UPDATE included) -- see the
        # wire-send site's own comment.
        self.turn_saw_any_visible_text: bool = False
        # One-shot guard so the completion-check's OWN reply (itself a
        # proactive turn, submitted via inject_proactive) can't chain into
        # firing this same check again if IT also happens to come back
        # empty -- fires at most once per originating turn, never an
        # infinite loop of self-nudges.
        self._awaiting_post_turn_check_reply: bool = False

        # Small-model primary path (2026-09-12, see small_model_engine.py's
        # own module docstring) -- an alternative to the SDK path above for
        # simple, tool-using turns, tried first via
        # submit_or_try_small_model() when eligible. small_model_active is
        # the ONLY thing that distinguishes "a turn is running" here from a
        # normal SDK turn (both set turn_pending True) -- every other piece
        # of code that branches on which engine is live checks this flag.
        self.small_model_active: bool = False
        # The live, in-memory exchange for the turn CURRENTLY running
        # through the small model -- set by run_small_model_turn's own
        # on_live_dialogue_update callback. Nothing this path does ever
        # touches the on-disk Claude Code .jsonl transcript (it never talks
        # to the CLI at all), so _gather_recent_dialogue_for_narration must
        # prefer this over its usual disk read while a small-model turn is
        # active, or the narrator would see nothing happening.
        self.small_model_live_dialogue: list[str] | None = None
        # New user_message text that arrived WHILE a small-model turn was
        # already running -- per explicit instruction, this is live context
        # fed into the SAME turn (via get_new_user_comments, polled once per
        # resolve_agentic() iteration), never a fresh submit() and never an
        # implicit cancellation of the turn in progress.
        self.small_model_pending_comments: list[str] = []
        self._small_model_task: asyncio.Task[None] | None = None

        # Forced compaction (see FORCED_COMPACTION_HOURLY_MS's own comment).
        # needs_startup_compaction is set True by main.py, once per tab_id
        # per PROCESS lifetime (a module-level set there, mirroring how the
        # startup greeting/pending-turn-crash-recovery already track "once
        # per process, not per reconnect") -- this ChatSession instance
        # itself gets recreated on every WS reconnect, so instance state
        # alone can't carry that scope.
        self.needs_startup_compaction: bool = False
        self.last_forced_compaction_at: float | None = None
        self.size_at_last_forced_compaction: int | None = None
        # Bug fix (2026-09-15), per explicit instruction -- confirmed live
        # via a direct test (see the "гипотеза B" investigation): the
        # on-disk .jsonl transcript is append-only -- a successful /compact
        # cuts real API-context tokens drastically (confirmed: 69422 ->
        # 10158, a real compact_boundary system message) but the FILE only
        # ever grows (never shrinks), so size_at_last_forced_compaction/
        # FORCED_COMPACTION_GROWTH_BYTES_THRESHOLD above were comparing
        # against a baseline that can never reflect what compaction
        # actually accomplished -- the growth trigger was watching the
        # wrong signal entirely. Track real context tokens instead:
        # tokens_at_last_forced_compaction is the authoritative baseline,
        # set from a compact_boundary system message's own
        # compact_metadata.post_tokens (the CLI's own count of what's left
        # right after compacting -- see the SystemMessage handling below);
        # last_known_context_tokens is updated from every REAL (non-fake)
        # ResultMessage's own usage field, the live "how much is actually
        # in context right now" signal Claude Code already reports on
        # every turn. Both None until the first real value arrives --
        # _check_forced_compaction's growth branch simply doesn't fire
        # until then (same graceful-startup shape as last_forced_
        # compaction_at being None), which is fine: "startup" already
        # forces an initial compaction every process start regardless.
        self.tokens_at_last_forced_compaction: int | None = None
        self.last_known_context_tokens: int | None = None
        # Per explicit instruction (2026-09-15): set by _apply_conn_state
        # whenever conn_state leaves "limited" -- see its own comment for
        # the incident (a forced-compaction attempt that itself hit the
        # usage cap left forced_compaction_result_pending stuck forever,
        # silently blocking every future compaction attempt even once the
        # cap reset). Consumed by _check_forced_compaction, which bypasses
        # its normal cooldown for this specific reason -- the point is to
        # retry what the cap just interrupted, not wait for the next
        # regular cycle.
        self.needs_post_limit_compaction_check: bool = False
        # time.monotonic() before which no forced compaction is attempted --
        # set when a forced compaction itself hits the usage cap (see
        # FORCED_COMPACTION_LIMIT_PAUSE_MS / _note_compaction_hit_limit).
        self.forced_compaction_blocked_until: float = 0.0
        # Same "this ResultMessage/whatever precedes it isn't real, don't
        # show it or let it touch turn state" shape as
        # hang_interrupt_result_pending/ignore_next_result_recovery -- see
        # result_is_fake's own computation in the message loop.
        self.forced_compaction_result_pending: bool = False
        # Bug fix (2026-09-18), per explicit instruction after a real
        # incident: a real user message submitted WHILE a forced compaction
        # is still in flight for this tab used to race straight into the
        # live client's input stream -- confirmed live, the CLI's own
        # compact boundary landed BEFORE that message got processed, and
        # the reply that eventually came back claimed the user's own
        # phrase "got cut off" (it genuinely had -- the compacted context
        # didn't include it). submit() now queues here instead of pushing
        # to the client whenever forced_compaction_result_pending is True;
        # _drain_compaction_queue() replays every queued item, in order,
        # through the normal submit() path (so post_turn_completion_check/
        # narration/durability all apply exactly as they would for a live
        # submit -- nothing special-cased) the moment compaction actually
        # finishes.
        self.compaction_queued_turns: list[dict[str, Any]] = []

        # activity / hang tracking
        self.last_activity = time.monotonic()
        self.last_user_activity = time.monotonic()
        self.has_seen_init = False
        self.hang_interrupted_at: float | None = None
        self.hang_count = 0
        # Bug fix (2026-09-10): confirmed live -- interrupt() (called by
        # _check_hang on a genuine hang) is a soft ask to the CLI, not a
        # hard kill -- the CLI still sends a final ResultMessage for the
        # turn it just aborted, structurally identical to one for a turn
        # that finished normally. Treating it as real completion wiped
        # turn_pending/pending_user_text AND reset silent_turn=True before
        # _handle_failure's own replay (which fires right after, once the
        # stream then actually ends) ever got a chance to run -- confirmed
        # live: the watchdog correctly caught a genuinely slow tool call,
        # interrupted it, and the replay then genuinely succeeded (a real
        # GitLab repo really did get created a few tool calls later), but
        # the success reply never reached the user at all, silently
        # swallowed by silent_turn still being True from the misclassified
        # ResultMessage. Set right before calling interrupt(); the very
        # next ResultMessage this flag is armed for skips the normal
        # turn-pending-clear/silent_turn-reset entirely (same shape as
        # ignore_next_result_recovery, a different trigger).
        self.hang_interrupt_result_pending = False
        # What tool call (if any) was actually in flight when a hang got
        # detected -- captured so _handle_failure's replay nudge can tell
        # the model specifically what got force-interrupted (per explicit
        # instruction, 2026-09-10) instead of a generic "something failed"
        # note, so it can try a different approach instead of blindly
        # repeating the same slow/stuck call.
        self.last_tool_use_name: str | None = None
        self.last_tool_use_started_at: float | None = None
        self.hang_interrupted_tool_name: str | None = None
        self.hang_interrupted_tool_elapsed_s: float | None = None

        # Bug fix (2026-09-15, "Стоп должен срабатывать ВСЕГДА"): the real
        # OS pid of the currently-live CLI subprocess, captured directly at
        # spawn time via session_context's cli-pid-sink (set right before
        # each self.client.connect() below) -- see
        # session_context.get_cli_pid_sink's own docstring for why this
        # replaced introspecting client._transport._process.pid at
        # stop-time, which was confirmed live to sometimes come back None
        # exactly when it was needed most (force_kill_cli_process_no_pid,
        # 2026-09-15 incident). Reset to None whenever the client that owns
        # it is torn down so a stale pid from an already-dead process is
        # never targeted.
        self._cli_process_pid: int | None = None
        # Bug fix (2026-09-15), per explicit instruction (see _check_hang's
        # own comment for the full reasoning): the real hang-detection
        # liveness signal, built on the same pid capture above. Recreated
        # whenever _cli_process_pid changes (a fresh connection); None
        # until the first pid is captured.
        self._process_activity_monitor: ProcessActivityMonitor | None = None

        # silent user-wait nudge (see SILENT_USER_WAIT_NUDGE_MS) -- tracks
        # only REAL user-typed messages (submit()'s is_real_user=True),
        # not proactive/reminder/retry turns
        self.last_real_user_turn_at: float | None = None
        # Bug fix (2026-09-16): wall-clock (epoch-ms) twin of the monotonic
        # field above -- see its own comment at the real submit() call
        # site for why a separate one is needed (transcript timestamps on
        # disk are wall-clock, not process-uptime-relative).
        self.last_real_user_turn_started_at_ms: float | None = None
        self.real_user_turn_answered = False
        self.silence_nudge_sent_for_turn = False

        # Ground-truth tool-outcome tracking for the self-check/push-through
        # mechanism (2026-09-22, per explicit instruction: "Кэролайн должна
        # не просто формально 'завершать ход', она должна решать задачу до
        # конца"). Scoped to the whole real-user EPISODE (reset only by a
        # fresh real submit(), same lifetime as real_user_turn_answered right
        # above), not per-turn like turn_saw_any_visible_text -- an internal
        # follow-up check is itself a new "turn" from submit()'s point of
        # view, and the whole point is remembering a tool failure ACROSS
        # those follow-ups until it's genuinely resolved (a later call to the
        # SAME tool succeeding overwrites/clears the earlier failure -- see
        # turn_tool_outcomes' own key). _pending_tool_calls is the id->name
        # scratch space bridging a ToolUseBlock to its eventual
        # ToolResultBlock; population happens once, at the top of _run_loop's
        # per-message handling, off the raw SDK objects both engines produce
        # identically -- see ToolOutcome's own docstring.
        self._pending_tool_calls: dict[str, str] = {}
        self.turn_tool_outcomes: dict[str, ToolOutcome] = {}

        # periodic mid-turn progress narration (see PROGRESS_NARRATION_INTERVAL_MS)
        # -- when a real user's own question was last actually shown something
        # (a real reply OR a generated stand-in progress comment), and what
        # that question was, so a comment (if generated) can tie back to it.
        self.last_visible_output_at: float | None = None
        # Bug fix (2026-09-22): see NARRATION_FAILURE_RETRY_S's own comment
        # -- a fully-failed generation attempt (all retries exhausted)
        # gets this short cooldown instead of either hammering the SW API
        # on the very next 5s watchdog tick or silently eating a full
        # extra 60s on top of the one that already produced nothing.
        self.last_narration_failed_at: float | None = None
        self.last_real_user_question: str | None = None
        # Set by main.py when it resumes a turn a restart interrupted: the user's ORIGINAL words. While set,
        # a resumed turn that ends without any visible text (a silent [[NO_UPDATE]]) is re-asked --
        # see the result handler and RESUMED_ANSWER_MAX_NUDGES.
        self.resumed_unanswered_question: str | None = None
        self.resumed_answer_nudges: int = 0
        # Bug fix (2026-09-14), per explicit instruction, root-caused via a
        # real live incident ("Кэролайн ведет беседу сама с собой"):
        # last_visible_output_at gets bumped by narration's OWN firing too
        # (it has to, to throttle to one comment per PROGRESS_NARRATION_
        # INTERVAL_MS) -- so it can't tell "a real reply just happened" from
        # "narration itself just fired" apart. Confirmed live: when a real
        # turn stays stuck for many minutes with no genuine progress,
        # _check_progress_narration kept firing every minute regardless,
        # each call re-narrating the same stale, unchanging dialogue window
        # -- individually novel enough to dodge the echo/garbage filters,
        # but collectively a nonsense stream of paraphrases that read as
        # Caroline talking to herself. This counter is separate and STRICT:
        # incremented only by narration actually firing, reset to 0 only by
        # genuine progress (a real user submit() or the real model's own
        # visible output, see MAX_CONSECUTIVE_NARRATION_COMMENTS's call
        # sites) -- never by narration's own output, which is the one
        # thing it exists to cap.
        self.consecutive_narration_count = 0

        # restart budget
        self.restart_timestamps: list[float] = []

        # session-id tracking. Context ageing is Claude's own native
        # auto-compaction now (see _run_loop's options: autoCompactEnabled)
        # -- no per-turn transcript rewrite, no per-turn CLI restart.
        self.last_saved_session_id: str | None = None

        self.current_chat_source: str | None = None
        # Per explicit instruction (2026-09-14): path to this tab's rolling
        # 24h-dialogue file -- see recent_dialogue_history_instruction's
        # own docstring (policies.py) and _write_recent_24h_dialogue_file
        # (above) for the full feature. The PATH is stable for this tab's
        # whole lifetime (unlike _system_prompt_language below, its own
        # system-prompt mention never goes stale/needs a restart to pick
        # up a change) -- only the file's CONTENT changes, rewritten fresh
        # before every real user submit(), so the model sees current data
        # any time it actually reads the file, regardless of how long this
        # particular query() client has been alive. None until the first
        # real user turn ever runs (see recent_dialogue_history_
        # instruction's own None-safe handling).
        self._recent_24h_dialogue_file_path: str | None = None
        # Single-flight state for _schedule_recent_24h_dialogue_refresh (see
        # its own docstring) -- at most one worker-thread refresh in flight
        # per tab; a request that arrives meanwhile just sets _dirty so one
        # more pass runs afterward, instead of piling up N parallel readers.
        self._recent_24h_refresh_task: "asyncio.Task[None] | None" = None
        self._recent_24h_refresh_dirty = False
        # The language hint baked into the CURRENT query()'s system prompt.
        # A long-lived client doesn't re-read it every turn anymore, so a
        # real user turn that finds the persisted language has changed
        # sets restart_pending to pick the new one up (see submit()).
        self._system_prompt_language: str | None = None

        # Consecutive "authentication_failed" api_retry messages on the
        # CURRENT connection -- see AUTH_RETRY_ESCALATION_THRESHOLD below.
        self.consecutive_auth_retry_failures = 0

        # deliberate-restart flags. restart_pending is the generic "tear the
        # query down and rebuild it cleanly next _run_loop iteration" signal
        # (auth-failure escalation, a settings/language change).
        self.restart_pending = False
        # Bug fix (2026-09-18), confirmed live: clear_tab()'s own kill
        # sometimes produces a normal, non-exceptional trailing
        # ResultMessage (the CLI winds down gracefully instead of the
        # connection just dying) -- without this, that ResultMessage was
        # treated as a genuine turn completion, which fired _fire_post_
        # turn_completion_check()'s "did you actually finish?" nudge
        # against the now-EMPTY freshly-cleared session. The model had
        # nothing to reference, produced SOME visible reply to it anyway,
        # and that reply became the first thing shown in the "cleared"
        # tab -- looking exactly like the clear had silently failed. Same
        # result_is_fake shape as hang_interrupt_result_pending/
        # forced_compaction_result_pending, one more reason a trailing
        # ResultMessage must never be treated as real.
        self.clear_tab_result_pending = False
        self.restart_for_unrecoverable_session = False
        self.unrecoverable_session_replay_text: str | None = None
        self.unrecoverable_session_replay_attachments: list[Any] = []
        self.skip_migration_fallback_once = False

        # Cross-engine handoff framing (2026-09-22), per explicit instruction
        # after a real incident: switch_engine_if_needed()'s force_restart()
        # ends the stream exactly like any other unexpected disconnect, so it
        # landed in _handle_failure's generic "internal failure, infrastructure
        # self-healing" framing -- which is simply WRONG for a deliberate,
        # user-requested engine switch, and (confirmed live) gives the newly
        # active engine no hint that it just inherited a conversation from a
        # DIFFERENT engine with none of its own memory of it. Set right before
        # force_restart() in switch_engine_if_needed(), consumed once (and
        # reset to None) at the top of _handle_failure, which swaps in a
        # dedicated note instead of the generic one when this is populated --
        # see _handle_failure's own comment for the incident this fixes.
        self.restart_engine_switch: tuple[str, str] | None = None

        # conn state / api retry / rate limit memory
        self.conn_state: dict[str, Any] = {"kind": "connected"}
        self.ignore_next_result_recovery = False
        # Bug fix (2026-09-22), confirmed live: the cc_cli_limit_message branch
        # below deliberately does NOT set ignore_next_result_recovery (per the
        # 2026-09-11 fix's own reasoning -- a real AssistantMessage came
        # through, so turn_pending must clear normally, not pretend the turn
        # never happened). But that flag ALSO gates the generic "reset
        # conn_state to connected" a little further down -- so conn_state,
        # set to "limited" moments earlier for a real, still-active usage
        # limit, was being silently overwritten back to "connected" by that
        # SAME turn's own trailing ResultMessage well under a second later.
        # Confirmed live: the status lamp/text never had a real chance to
        # show it. A narrower, single-purpose flag: protects ONLY the
        # conn_state reset for the one ResultMessage immediately following a
        # real limit hit, leaving turn_pending/the retry timer completely
        # unaffected (a genuine fix, not a revert of the 2026-09-11 one).
        self.suppress_next_conn_state_reset = False
        self.api_retry_timer: asyncio.TimerHandle | None = None
        # See _schedule_one_shot_followup_check's own doc comment -- a
        # separate timer from api_retry_timer above, deliberately never
        # re-armed by its own firing (unlike api_retry_timer, which keeps
        # rescheduling itself).
        self.one_shot_followup_timer: asyncio.TimerHandle | None = None
        self.last_rate_limit_info: dict[str, Any] | None = None
        self.last_api_retry_error: str | None = None
        self.mcp_reconnect_timers: dict[str, asyncio.TimerHandle] = {}

        self._run_loop_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------- lifecycle --

    async def start(self) -> None:
        # Bug fix (2026-09-10): confirmed live -- _watchdog_loop's task
        # died silently (no exception logged anywhere) and was never
        # restarted, permanently disabling hang-detection/progress-
        # narration/the silent-user-wait nudge/the idle-task-drift check
        # for that tab for the rest of the process's lifetime, unnoticed
        # for 20+ minutes. supervise() (task_supervisor.py) is the
        # standing fix: log any crash in full and restart the loop, for
        # every long-running background loop in this backend, not just
        # this one -- see its own module docstring.
        self._run_loop_task = supervise("run_loop", self._run_loop, self.tab_id)
        self._watchdog_task = supervise("watchdog", self._watchdog_loop, self.tab_id)

    def dispose(self) -> None:
        log_event("engine", "dispose", tab_id=self.tab_id)
        self.ended = True
        if self._watchdog_task:
            self._watchdog_task.cancel()
        self._clear_api_retry_timer()
        self._clear_one_shot_followup_timer()
        self._clear_mcp_reconnect_timers()
        if self.client:
            asyncio.create_task(self._safe_interrupt())
        REGISTRY.cancel_for_tab(self.tab_id)
        self._queue_event.set()

    def force_restart(self) -> None:
        """Forces this session down the same way hang-escalation's own hard-
        close does (disconnect() -> _run_loop's own exception handling ->
        a fresh session on the SAME session id) -- for a human or a script
        to trigger directly over /api/control instead of having to kill OS
        processes by hand. See main.py's "force_restart" control op."""
        log_event("engine", "force_restart_requested", tab_id=self.tab_id)
        if self.client:
            asyncio.create_task(self._safe_disconnect(self.client))

    def _check_deferred_engine_switch(self) -> None:
        """Applies a switch that had to wait for a running turn, as soon as the
        session is idle (watchdog tick) -- nothing is ever interrupted for it."""
        if self.engine_switch_pending and not self.turn_pending and not self.ended:
            self.engine_switch_pending = False
            self.switch_engine_if_needed()

    def switch_engine_if_needed(self) -> str:
        """Called after the tab's answer source changed. Only Claude <-> OpenAI
        needs a fresh session (they are different agent processes); SW routing
        is decided per message. Returns "unchanged", "switching" (the running
        session was torn down and the run loop rebuilds it on the new engine)
        or "busy" (a turn is in flight -- the switch is saved and applied by the
        watchdog as soon as the turn ends; nothing is interrupted)."""
        wanted = "openai" if load_chat_mode(self.workspace_dir, self.tab_id) == "openai" and openai_available(self.workspace_dir) else "claude"
        if wanted == self.engine_kind or self.client is None:
            return "unchanged"
        # The mode changed: a retry armed against the OLD engine's failure (bad key, outage)
        # must not fire into the new one -- cancelled here, before the busy check, so it
        # can't slip in while the switch waits for the running turn.
        self._clear_api_retry_timer()
        self._clear_one_shot_followup_timer()
        if self.turn_pending:
            log_event("engine", "engine_switch_deferred_turn_pending", tab_id=self.tab_id, wanted=wanted)
            self.engine_switch_pending = True
            return "busy"
        log_event("engine", "engine_switch", tab_id=self.tab_id, from_engine=self.engine_kind, to_engine=wanted)
        # All of this is Claude-CLI-specific compaction bookkeeping (dehydration/
        # forced-compaction machinery, see transcript_rotate.py's module docstring
        # and _check_forced_compaction) -- it must not survive onto the other
        # engine, which owns its own compaction and has no equivalent state.
        # Confirmed live: leaving forced_compaction_result_pending=True stuck
        # after a switch silently hid every subsequent reply from the client
        # forever (see the wire-send gate a few hundred lines down) and pinned
        # the status lamp on "Compacting conversation..." -- a real incident,
        # not a theoretical one. Any real user message queued behind that
        # compaction is replayed via the normal drain path, not dropped.
        if self.forced_compaction_result_pending:
            self.forced_compaction_result_pending = False
            self._drain_compaction_queue()
        self.needs_post_limit_compaction_check = False
        self.forced_compaction_blocked_until = 0.0
        # See restart_engine_switch's own __init__ comment -- consumed once
        # by _handle_failure, which is where force_restart()'s stream-end
        # actually lands.
        self.restart_engine_switch = (self.engine_kind, wanted)
        self.force_restart()
        return "switching"

    async def _safe_interrupt(self) -> None:
        try:
            if self.client:
                await self.client.interrupt()
        except Exception as exc:
            log_event("engine", "dispose_interrupt_failed", tab_id=self.tab_id, error=str(exc))

    async def _safe_disconnect(self, client: AgentEngine) -> None:
        """Fire-and-forget disconnect wrapper -- claude_agent_sdk's own
        subprocess_cli.py's close() has a confirmed live bug (2026-09-09):
        it doesn't guard against self._process already being None (a
        concurrent close from elsewhere, or the process having already
        exited), so it can throw a bare AttributeError
        ('NoneType' object has no attribute 'terminate'/'returncode') from
        deep inside the SDK's own internals. A disconnect() awaited
        directly is already covered by whatever try/except wraps its own
        call site (or _run_loop's own outer exception handler); this
        wrapper exists specifically for callers that fire disconnect() via
        asyncio.create_task() without awaiting it -- without a wrapper
        like this, that exception becomes an unretrieved Task exception
        (confirmed live: 'Task finished ... exception=AttributeError' with
        no owner to catch it), which is silent noise at best and, if the
        SDK's own internal state ends up inconsistent as a result, a
        plausible contributor to the process-level hangs seen the same
        night (see the migration plan's own incident writeup)."""
        try:
            await client.disconnect()
        except Exception as exc:
            log_event("engine", "safe_disconnect_failed", tab_id=self.tab_id, error=str(exc))

    # ------------------------------------------------------------- status --
    # Per explicit instruction (2026-09-11): "четыре режима: готов, работаю,
    # не готов но сам восстановлюсь, ошибка которую сам восстановить не
    # смогу" -- replaces the previous tangle of caroline_status/
    # system_notice/turnQueue-length-driven client-side busy inference that
    # kept producing new corner cases all night (a lamp that wouldn't
    # blink, one that wouldn't turn yellow, a busy state that never got
    # re-armed after a retry...). ONE authoritative value, computed here,
    # published to the client in ONE message type -- chat.js no longer
    # infers anything from turnQueue length or message-type bookkeeping,
    # it just renders whatever this says.

    @property
    def turn_pending(self) -> bool:
        return self._turn_pending

    @turn_pending.setter
    def turn_pending(self, value: bool) -> None:
        if value == self._turn_pending:
            return
        log_event("engine", "turn_pending_changed", tab_id=self.tab_id, prev=self._turn_pending, new=value)
        self._turn_pending = value
        # Bug fix (2026-09-16), per explicit instruction: _check_hang's
        # process-activity signal (2026-09-15) has no absolute ceiling of
        # its own -- a process that keeps showing SOME CPU/RSS/IO movement
        # at least once every <90s (its own periodic retries, background
        # chatter, anything) is "alive" by that definition forever, no
        # matter how long the actual turn has gone unanswered. Confirmed
        # live: a real turn sat pending for 15.5 minutes with hang_count
        # staying at 0 the entire time. This timestamp -- when the CURRENT
        # turn actually started, independent of the activity signal -- is
        # what lets _check_hang enforce a hard ceiling on top of it.
        self.turn_pending_since = time.monotonic() if value else None
        # Every one of the ~9 places in this file that flips turn_pending
        # now republishes status automatically -- no call site has to
        # remember to do it itself (that "remember to do it everywhere"
        # pattern is exactly what produced tonight's whole run of bugs).
        asyncio.create_task(self._publish_status())

    def _schedule_recent_24h_dialogue_refresh(self) -> None:
        """Rebuilds this tab's 24h-dialogue file in a WORKER THREAD, never on
        the event loop (see _write_recent_24h_dialogue_file's docstring for
        the outage that made that necessary). Single-flight: at most one
        refresh runs per tab at a time -- a request that arrives while one
        is in flight just marks it dirty so exactly one more pass runs after
        it, instead of N submits queuing N parallel transcript readers.
        Fire-and-forget by design: the file's PATH is stable and already in
        the system prompt, so nothing waits on this; a failure is logged and
        leaves the previous (stale but valid) file in place. With no running
        event loop (a plain script/test calling into ChatSession
        synchronously) it just runs inline."""
        if self._recent_24h_refresh_task is not None and not self._recent_24h_refresh_task.done():
            self._recent_24h_refresh_dirty = True
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                _write_recent_24h_dialogue_file(self.last_saved_session_id, self.tab_id, self.workspace_dir)
            except Exception as exc:
                log_event("engine", "recent_24h_dialogue_file_refresh_failed", tab_id=self.tab_id, error=str(exc))
            return
        self._recent_24h_refresh_task = loop.create_task(self._refresh_recent_24h_dialogue_async())

    async def _refresh_recent_24h_dialogue_async(self) -> None:
        """Runs the rebuild in a SEPARATE PROCESS (see dialogue_refresh_
        worker.py for the outage that made a thread not enough), at most one
        such process at a time across ALL tabs, at below-normal priority, and
        skipped outright when the file was rebuilt within the last
        RECENT_24H_REFRESH_MIN_AGE_S -- a restart storm otherwise re-parses
        the same hundreds of MB once per restart per tab. The file's PATH is
        stable and already in the system prompt, so a skipped/failed refresh
        just leaves the previous valid file in place."""
        while True:
            self._recent_24h_refresh_dirty = False
            started = time.monotonic()
            out_path = _recent_24h_dialogue_path(self.workspace_dir, self.tab_id)
            try:
                age_s = time.time() - out_path.stat().st_mtime
            except OSError:
                age_s = None
            if age_s is not None and age_s < RECENT_24H_REFRESH_MIN_AGE_S:
                log_event("engine", "recent_24h_dialogue_file_fresh_skip", tab_id=self.tab_id, age_s=round(age_s))
                return
            try:
                async with _RECENT_24H_REFRESH_SLOT:
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable, str(Path(__file__).resolve().parent / "dialogue_refresh_worker.py"),
                        self.last_saved_session_id or "-", self.tab_id, self.workspace_dir,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0),
                    )
                    try:
                        code = await asyncio.wait_for(proc.wait(), timeout=RECENT_24H_REFRESH_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                        raise RuntimeError(f"dialogue refresh worker exceeded {RECENT_24H_REFRESH_TIMEOUT_S}s and was killed")
                if code != 0:
                    raise RuntimeError(f"dialogue refresh worker exited with code {code}")
                log_event("engine", "recent_24h_dialogue_file_refreshed", tab_id=self.tab_id, ms=round((time.monotonic() - started) * 1000))
            except Exception as exc:
                log_event("engine", "recent_24h_dialogue_file_refresh_failed", tab_id=self.tab_id, error=str(exc))
            if not self._recent_24h_refresh_dirty:
                return

    def _check_funds_exhaustion_status(self) -> None:
        """Per explicit instruction (2026-09-16): sw_api.py's exhaustion
        flag is set/cleared as a side effect of whatever REAL call
        happened to notice it (a narration tick, a translation, ...) --
        nothing proactively tells THIS tab's own status bar when it
        changes on its own. Polled once per watchdog tick (cheap, no
        network call -- just reads a module-level string) so a change
        reaches the status bar within one tick either direction, not only
        the next time something else happens to trigger a republish."""
        current = get_funds_exhausted_reason()
        if current != self._last_known_funds_exhausted_reason:
            self._last_known_funds_exhausted_reason = current
            asyncio.create_task(self._publish_status())

    def _compute_public_status(self) -> tuple[str, str]:
        kind = self.conn_state.get("kind")
        reason = self.conn_state.get("reason") or ""
        if kind in ("billing_blocked", "not_logged_in", "auth_failed"):
            return "error", reason
        if kind in ("restarting", "restart_backoff", "limited", "engine_error"):
            return "recovering", reason
        # Bug fix (2026-09-16), per explicit instruction: "при исчерпании
        # баланса на клоде или опенроутере эта информация явно
        # прокидывалась в кэролайн и высвечивалась на статус-баре" -- see
        # sw_api.get_funds_exhausted_reason's own comment for how this is
        # detected. Deliberately NOT surfaced as "error" here (unlike
        # Claude's own billing_blocked above) -- an exhausted SquirrelWisdom/
        # OpenRouter balance doesn't block a tab using its own Claude
        # subscription at all, only narration/translation/consult/SW-mode
        # chat, which is why this rides along as extra reason text on
        # ready/working instead of overriding the actual state.
        # Per explicit instruction (2026-09-18): compaction must be visible
        # on the status bar (the yellow "recovering" lamp, same as
        # restarting/restart_backoff/limited above) -- previously silent,
        # since forced compaction deliberately bypasses turn_pending
        # entirely (see _check_forced_compaction's own docstring) and
        # nothing else was publishing a status change for it. Checked
        # before turn_pending: a real message submitted during compaction
        # is now queued (see compaction_queued_turns), not pushed, so
        # turn_pending itself stays False throughout -- this is the ONLY
        # signal that would otherwise tell the user anything is happening.
        if self.forced_compaction_result_pending:
            queued_note = f" ({len(self.compaction_queued_turns)} message(s) queued)" if self.compaction_queued_turns else ""
            return "recovering", f"Compacting conversation, one moment…{queued_note}"
        funds_note = ""
        funds_reason = get_funds_exhausted_reason()
        if funds_reason:
            funds_note = f"SquirrelWisdom/OpenRouter balance exhausted -- narration/translation/SW features are down until it's topped up ({funds_reason})"
        if self.turn_pending:
            return "working", funds_note
        return "ready", funds_note

    async def _publish_status(self) -> None:
        state, reason = self._compute_public_status()
        # Per standing instruction ("логи повсеместно"): every status the
        # client is told about is logged here, in ONE place, regardless of
        # which of the several call sites (turn_pending's own setter,
        # _apply_conn_state, the initial connect in main.py) triggered it --
        # matches the same "log at the one choke point, not at every
        # caller" shape as task_supervisor.py.
        log_event("engine", "status_published", tab_id=self.tab_id, state=state, reason=reason)
        # "kind" is additive (chat.js's lamp logic only ever reads "state"/
        # "reason", unaffected) -- lets chat.js tell "not_logged_in" apart
        # from "billing_blocked" even though both publish as state="error",
        # so it can raise a one-time native dialog only for the former (see
        # chat.js's applyCarolineStatus).
        await self.send({"type": "status", "state": state, "reason": reason, "kind": self.conn_state.get("kind")})

    # --------------------------------------------------------------- submit --

    def submit(self, text: str, attachments: list[Any] | None = None, is_real_user: bool = True, is_voice: bool = False, pending_text: str | None = None) -> None:
        # Per standing instruction ("ВЕЗДЕ логируем и ВСЁ"): this is the
        # single choke point EVERY turn goes through -- real user messages,
        # every proactive/internal nudge (inject_proactive already logs
        # its own text_len separately, but not is_real_user/is_voice), and
        # every replay -- confirmed live tonight this had NO logging of
        # its own at all, unlike inject_proactive.
        attachments = attachments or []
        # Bug fix (2026-09-18), per explicit instruction after a real
        # incident: never push straight into the live client while a
        # forced compaction is still in flight for this tab -- confirmed
        # live that a real user message submitted mid-compaction raced
        # the CLI's own compact boundary and came back as a reply claiming
        # the user's own phrase "got cut off" (it genuinely had). Queue
        # here instead; _drain_compaction_queue() replays every queued
        # item, in order, through this exact same submit() once
        # forced_compaction_result_pending goes back to False -- nothing
        # about a queued item's own eventual handling is special-cased,
        # it's a completely normal submit() just delayed.
        if self.forced_compaction_result_pending:
            log_event(
                "engine", "submit_queued_during_compaction", tab_id=self.tab_id, is_real_user=is_real_user,
                is_voice=is_voice, text_len=len(text), attachment_count=len(attachments),
            )
            self.compaction_queued_turns.append({"text": text, "attachments": attachments, "is_real_user": is_real_user, "is_voice": is_voice, "pending_text": pending_text})
            asyncio.create_task(self._publish_status())
            return
        log_event(
            "engine", "submit", tab_id=self.tab_id, is_real_user=is_real_user, is_voice=is_voice,
            text_len=len(text), attachment_count=len(attachments),
        )
        self.classifier_refusal_retry_count = 0
        self.pending_user_text = text
        # Reset for THIS turn -- see this flag's own __init__ comment for
        # why it exists (post-turn-completion sanity check for proactive
        # turns that silently produce no content at all).
        self.turn_saw_any_visible_text = False
        # Bug fix (2026-09-11), per explicit instruction: _gather_recent_
        # dialogue_for_narration's own pending_user_text fallback used to
        # label THIS unconditionally as if the user had just said it --
        # confirmed live: an internal retry nudge ("[Internal: automatic
        # recheck... Reply in English.") got fed to the narrator as literal
        # user speech, producing both nonsense commentary and an English
        # reply for an otherwise-Russian conversation. Tracked alongside
        # pending_user_text so that fallback can tell the difference.
        #
        # Bug fix (2026-09-15), confirmed live -- tab 4, right after this
        # same session's app restart: a real user message set this True,
        # then (mid-turn, while turn_pending was ALREADY True from that
        # same real question) a background-operation-completion nudge
        # fired via inject_proactive -> submit(is_real_user=False), which
        # unconditionally overwrote this back to False -- silencing the
        # narrator for the rest of that turn even though the model was
        # still working on nothing but the user's own real request the
        # whole time. A proactive/internal nudge injected INTO an
        # already-pending turn is additive content for that same turn,
        # not the start of a new one -- it must never downgrade an
        # already-True flag. Only actually reset the flag (to whatever
        # is_real_user says) when this submit() is starting a genuinely
        # NEW turn (self.turn_pending wasn't already True) -- a real
        # user message still always wins immediately either way.
        if is_real_user or not self.turn_pending:
            self.pending_is_real_user = is_real_user
        self.pending_attachments = attachments
        self.turn_pending = True
        self.last_activity = time.monotonic()
        if is_real_user:
            self.last_user_activity = time.monotonic()
            self.last_real_user_turn_at = time.monotonic()
            # Bug fix (2026-09-16), per explicit instruction: "нарратор
            # должен комментировать только текущую задачу" -- last_real_
            # user_turn_at above is monotonic (process-uptime-relative),
            # not comparable to the transcript's own wall-clock
            # timestamps. This is the SAME moment, in wall-clock epoch-ms,
            # so _gather_recent_dialogue_for_narration can use it as a
            # floor -- excluding anything left over from a previous,
            # already-finished task instead of a blind last-N-lines window
            # that doesn't know where the current task actually started.
            self.last_real_user_turn_started_at_ms = time.time() * 1000
            self.real_user_turn_answered = False
            self.silence_nudge_sent_for_turn = False
            # A genuine new real-user episode starts with a clean ground-truth
            # slate -- see the field's own __init__ comment for why this is
            # episode-scoped rather than per-turn. A leftover in-flight tool
            # call from whatever the PREVIOUS episode was doing can never be
            # resolved now (that episode is over), so it's discarded here
            # too rather than left to dangle in _pending_tool_calls forever.
            self._pending_tool_calls = {}
            self.turn_tool_outcomes = {}
            self.last_visible_output_at = time.monotonic()
            # Bug fix (2026-09-14): a genuine new real message is the one
            # thing that should give this a clean slate -- see its own
            # __init__ comment for the incident this fixes.
            self.consecutive_narration_count = 0
            self.last_real_user_question = text
            # Bug fix (2026-09-11), per explicit instruction: language must
            # be tracked CONTINUOUSLY, not resolved once and left alone --
            # confirmed live a tab can legitimately switch languages
            # mid-conversation (e.g. the user asking for a contract drafted
            # in three languages), and a one-shot resolution (at startup,
            # or only as a side effect of an unrecoverable-session-reset)
            # can never catch that. Every real user turn re-triggers it --
            # fire-and-forget, same mechanism as before, just called far
            # more often (as often as the user actually talks) instead of
            # a handful of one-off lifecycle events. Reads whatever's on
            # disk as of THIS call (not including the text being submitted
            # right now, which hasn't been flushed to the transcript yet)
            # -- a real language switch shows up starting from the
            # FOLLOWING turn, not instantly; an acceptable lag, not a
            # correctness gap (current_language_name() is only ever read
            # at the next query()/narration tick anyway).
            refresh_language_in_background(self.last_saved_session_id, self.tab_id)
            # Per explicit instruction (2026-09-14): this tab's 24h-dialogue
            # file is refreshed on every real user turn -- see
            # _write_recent_24h_dialogue_file's own docstring. Bug fix
            # (2026-09-20): no longer synchronous on the event loop (see
            # _schedule_recent_24h_dialogue_refresh) -- the path is stable,
            # so setting it now is instant; the content catches up in a
            # worker thread within a moment, long before the model reads it.
            self._recent_24h_dialogue_file_path = str(_recent_24h_dialogue_path(self.workspace_dir, self.tab_id))
            self._schedule_recent_24h_dialogue_refresh()
        # pending_text: what a restart should resume FROM -- the user's own words, not this
        # call's (possibly wrapped) wire text. See durability.unwrap_resume_note for the
        # nested-wrapper incident this prevents.
        save_pending_turn(self.workspace_dir, self.tab_id, pending_text if pending_text is not None else text, attachments)
        # Bug fix (2026-09-10): tag the WIRE copy (never pending_user_text/
        # last_real_user_question/the saved pending-turn file above -- those
        # all need to stay the real, clean text for their own consumers,
        # e.g. main.py's crash-resume path or a restart replay) so a reader
        # of the saved transcript can tell a proactive/synthetic turn from
        # a real one structurally, without guessing from its wording -- see
        # _SYNTHETIC_TURN_MARKER's own docstring.
        wire_text = text if is_real_user else f"{_SYNTHETIC_TURN_MARKER}{text}"
        self._push_message(wire_text, attachments, is_voice)

    def submit_or_try_small_model(self, text: str, attachments: list[Any] | None = None, is_voice: bool = False) -> None:
        """The real entry point for a REAL user_message (main.py's WS
        "user_message" handler and POST /api/message both call this now,
        never submit() directly) -- per explicit design (2026-09-12): try
        answering through the small/cheap Camerlengo model FIRST when
        eligible, falling through to the existing, completely untouched
        Claude Agent SDK path (self.submit()) otherwise, including on the
        small model's own escalation (self-reported sentinel or the
        mechanical repeated-tool-call guard, see small_model_engine.py).
        Gated per-tab now (2026-09-14) by this tab's own persisted chat
        mode (durability.py's load_chat_mode, Settings-controlled -- see
        chat_mode_eligible's own docstring for why "sw" can only be set
        when both subscriptions are active) -- every real turn falls
        through to self.submit() below unless this tab is specifically set
        to "sw".

        Eligible means: this tab's chat mode is "sw", no attachments (the
        small model never sees vision/document content here -- not a hard
        technical limit, just out of scope for this first cut), no turn
        already in flight, and SquirrelWisdom access is available --
        login_api.is_logged_in(), the SAME check sw_gate.py's own
        require_sw_or_prompt already uses at every other SW-gated call
        site, reused rather than re-derived.

        If a small-model turn is ALREADY running, a new message here is
        live context for THAT turn, not a fresh submit -- see the
        small_model_pending_comments docstring above."""
        attachments = attachments or []
        if self.small_model_active:
            log_event("engine", "small_model_live_comment_queued", tab_id=self.tab_id, text_len=len(text))
            self.small_model_pending_comments.append(text)
            return

        if load_chat_mode(self.workspace_dir, self.tab_id) != "sw" or attachments or self.turn_pending or not is_logged_in():
            self.submit(text, attachments, True, is_voice)
            return

        log_event("engine", "small_model_turn_attempting", tab_id=self.tab_id, text_len=len(text))
        # Mirrors submit()'s own is_real_user=True bookkeeping exactly (see
        # its comments for why each field exists) -- this turn is just as
        # real a user turn as one that goes through the SDK, so every
        # consumer of this state (narration, the silent-wait nudge, crash
        # recovery) must see it the same way.
        self.classifier_refusal_retry_count = 0
        self.pending_user_text = text
        self.pending_is_real_user = True
        self.pending_attachments = []
        self.turn_pending = True
        self.last_activity = time.monotonic()
        self.last_user_activity = time.monotonic()
        self.last_real_user_turn_at = time.monotonic()
        self.last_real_user_turn_started_at_ms = time.time() * 1000
        self.real_user_turn_answered = False
        self.silence_nudge_sent_for_turn = False
        # See the matching reset in submit() for why this is cleared here too.
        self._pending_tool_calls = {}
        self.turn_tool_outcomes = {}
        self.last_visible_output_at = time.monotonic()
        self.consecutive_narration_count = 0
        self.last_real_user_question = text
        refresh_language_in_background(self.last_saved_session_id, self.tab_id)
        save_pending_turn(self.workspace_dir, self.tab_id, text, [])

        self.small_model_active = True
        self.small_model_live_dialogue = None
        self._small_model_task = asyncio.create_task(self._run_small_model_turn(text, is_voice))

    def _end_small_model_turn(self) -> None:
        self.small_model_active = False
        self.small_model_live_dialogue = None
        self.small_model_pending_comments = []
        self._small_model_task = None

    async def _run_small_model_turn(self, text: str, is_voice: bool) -> None:
        persona = get_persona(self.workspace_dir)
        recent_dialogue_lines = _read_recent_dialogue_lines(
            self.last_saved_session_id, self.tab_id, self.workspace_dir, RECENT_DIALOGUE_WINDOW,
        )
        language = current_language_name(self.tab_id)

        def on_live_dialogue_update(lines: list[str]) -> None:
            self.small_model_live_dialogue = lines

        def get_new_user_comments() -> list[str] | None:
            if not self.small_model_pending_comments:
                return None
            comments = self.small_model_pending_comments
            self.small_model_pending_comments = []
            return comments

        try:
            result = await run_small_model_turn(
                tab_id=self.tab_id, workspace_dir=self.workspace_dir, persona=persona, user_text=text,
                recent_dialogue_lines=recent_dialogue_lines, language=language,
                send=self.send, on_live_dialogue_update=on_live_dialogue_update,
                get_new_user_comments=get_new_user_comments,
            )
        except asyncio.CancelledError:
            # Stop button -- see stop()'s own small_model_active branch.
            log_event("engine", "small_model_turn_cancelled", tab_id=self.tab_id)
            clear_pending_turn(self.workspace_dir, self.tab_id)
            self.turn_pending = False
            self._end_small_model_turn()
            raise
        except Exception as exc:  # noqa: BLE001 -- this path must never wedge the session
            log_event("engine", "small_model_turn_unexpected_error", tab_id=self.tab_id, error=str(exc), error_type=type(exc).__name__)
            result = {"status": "escalate", "reason": f"unexpected error: {exc}"}

        if result["status"] == "answered":
            await self._finish_small_model_turn_answered(text, result["text"], is_voice)
        else:
            log_event("engine", "small_model_escalating_to_sdk", tab_id=self.tab_id, reason=result.get("reason"))
            self._end_small_model_turn()
            # Falls through to the real, untouched SDK path with the
            # original text -- nothing was ever shown to the user yet, so
            # this is indistinguishable to them from a normal first submit.
            self.submit(text, [], True, is_voice)

    async def _finish_small_model_turn_answered(self, question_text: str, text: str, is_voice: bool) -> None:
        """Synthesizes the same {assistant} + {result} wire pair a normal
        SDK turn's completion sends (see wire.py's message_to_wire), so
        chat.js needs no changes at all to render a small-model answer --
        it can't tell the difference from a real SDK turn's own reply.

        Also persists this exchange onto the tab's own SDK session .jsonl
        (see _append_small_model_turn_to_session's own docstring) -- both
        engines answer the same conversation and get used interchangeably
        turn by turn, so both must leave the SAME durable history behind,
        not two that silently diverge.

        Per explicit instruction (2026-09-15): forced-translates `text`
        here too, same as the full SDK path's own _translate_wire_visible_
        text -- this path is just as much a "real visible reply" as that
        one, and the small model's own language compliance is no more
        reliable than the full model's. Persists the TRANSLATED text (not
        the original) to the session .jsonl -- that's genuinely what
        Caroline said to the user, and every later reader of that history
        (recent-dialogue narration input, the 24h-dialogue file, a resumed
        SDK session) should see the same thing the user actually saw."""
        from app.plugins.voice_api import translate_text

        try:
            translated = await translate_text(text, current_language_name(self.tab_id), gender=get_persona_gender(self.workspace_dir))
        except Exception as exc:
            log_event("engine", "small_model_translate_failed", tab_id=self.tab_id, error=str(exc))
            translated = None
        if translated and translated != text:
            log_event("engine", "small_model_translate_applied", tab_id=self.tab_id, original_len=len(text), translated_len=len(translated))
            text = translated
        log_event("engine", "small_model_turn_answered_finishing", tab_id=self.tab_id, text_len=len(text))
        _append_small_model_turn_to_session(self.workspace_dir, self.tab_id, self.last_saved_session_id, question_text, text)
        assistant_wire = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "model": "small-model",
                "stop_reason": "end_turn",
            },
            "session_id": self.last_saved_session_id,
            "parent_tool_use_id": None,
        }
        await self.send({"type": "sdk_message", "message": assistant_wire})
        result_wire = {
            "type": "result",
            "subtype": "success",
            "duration_ms": 0,
            "is_error": False,
            "num_turns": 1,
            "session_id": self.last_saved_session_id,
            "total_cost_usd": 0.0,
            "result": text,
        }
        await self.send({"type": "sdk_message", "message": result_wire, "isVoice": is_voice})
        self.real_user_turn_answered = True
        self.last_visible_output_at = time.monotonic()
        self.consecutive_narration_count = 0
        clear_pending_turn(self.workspace_dir, self.tab_id)
        self.turn_pending = False
        self._end_small_model_turn()
        # Per explicit instruction (2026-09-13): "Работа малой модели в
        # отсутствие эскалации не должна требовать Claude SDK вообще" --
        # this used to call self._fire_post_turn_completion_check() here,
        # which unconditionally goes through self.submit() to the full SDK.
        # That's wrong for a small-model-answered turn specifically: the
        # ONLY things allowed to reach the SDK are the two existing
        # escalation paths (the model's own sentinel, the mechanical
        # repeated-call guard) -- never a routine completion sanity check.
        # That check now lives entirely inside run_small_model_turn() itself
        # (small_model_engine.py), using Camerlengo/OpenRouter models only.

    def inject_proactive(self, text: str, attachments: list[Any] | None = None, is_voice: bool = False, pending_text: str | None = None) -> bool:
        """Bug fix (2026-09-11), per explicit instruction: no more
        silent/silent_turn parameter -- whether a proactive reply is worth
        showing is now decided ENTIRELY by the model's own reply content
        (the existing [[NO_UPDATE]] sentinel, see no_update_sentinel_instruction),
        never by a caller-side flag here. silent_turn used to be a
        whole-session AND-latch that (a) couldn't be un-set once a real
        conversation had made it False (confirmed live: an hourly
        "silent" reminder could still leak into the chat this way) and
        (b) got its own state corrupted by an unrelated bug (a rejected
        turn's own trailing ResultMessage wrongly flipping it back to
        True mid-conversation, silently swallowing a whole later real
        reply). Removed entirely rather than patched again."""
        if self.ended:
            return False
        attachments = attachments or []
        log_event("engine", "proactive_inject", tab_id=self.tab_id, text_len=len(text), attachment_count=len(attachments))
        asyncio.create_task(self.send({"type": "proactive_turn_queued"}))
        self.submit(text, attachments, False, is_voice, pending_text=pending_text)
        return True

    def stop(self) -> None:
        if not self.turn_pending:
            return
        log_event("engine", "user_stop", tab_id=self.tab_id)
        self.user_stop_requested = True
        # An explicit Stop must also cancel any pending "resumed question needs a visible answer" re-ask,
        # or the abandoned turn would start again on its own.
        self.resumed_unanswered_question = None
        if self.small_model_active:
            # No SDK client/query() is involved in this path at all -- the
            # only thing to stop is the background task running
            # resolve_agentic() (in a worker thread) and, via REGISTRY
            # below, any tool call it already dispatched.
            if self._small_model_task is not None:
                self._small_model_task.cancel()
        elif self.client:
            # Bug fix (2026-09-15), per explicit instruction: "Кнопка стоп
            # это абсолютный рубильник... Сразу по нажатии" -- Stop must
            # stop EVERYTHING in this tab (dialogue, tasks, agents)
            # immediately on click, not best-effort or after a grace
            # period. The previous design (2026-09-14) tried a soft
            # interrupt() first and only escalated to a hard kill after
            # STOP_ESCALATION_GRACE_S (5s) -- confirmed live, twice now,
            # that soft interrupt() alone routinely doesn't work, so that
            # 5s was just a guaranteed delay before the user got real
            # control back, not a real chance for the polite path to
            # succeed. Goes straight for the hard kill now, with no grace
            # period: _force_stop_client (fires interrupt() AND
            # disconnect() concurrently, immediately, and falls back to a
            # direct OS-process kill if disconnect() itself throws -- see
            # its own docstring for the SDK bug that makes that fallback
            # necessary).
            asyncio.create_task(self._force_stop_client())
        # Bug fix (2026-09-10): confirmed live -- client.interrupt() alone
        # only stops the model's own generation stream. A tool call that
        # already crossed dispatch()'s fast-path window (app/operations.py)
        # became a detached background asyncio.Task in the process-wide
        # REGISTRY, entirely decoupled from this turn/session -- Stop never
        # reached it, so it kept running to completion regardless. This
        # cancels exactly THIS tab's own still-running background
        # operations alongside the interrupt.
        cancelled = REGISTRY.cancel_for_tab(self.tab_id)
        if cancelled:
            log_event("engine", "user_stop_cancelled_operations", tab_id=self.tab_id, count=cancelled)
        # Bug fix (2026-09-14), per explicit instruction ("не понимает, что
        # её прерывали и ход тем самым завершён"): confirmed live -- Stop
        # never touched api_retry_timer/one_shot_followup_timer, so a
        # pending auto-recovery nudge (scheduled while THIS turn was still
        # struggling -- a rate-limit retry, a usage-cap follow-up) fired
        # anyway, later, as if the user had never stepped in at all. From
        # the user's side that reads as "I stopped her and she just kept
        # going" -- an explicit Stop should silence every background
        # recovery attempt tied to this now-abandoned turn, not just the
        # turn itself.
        self._clear_api_retry_timer()
        self._clear_one_shot_followup_timer()

    async def _force_stop_client(self) -> None:
        """Per explicit instruction (2026-09-15): "Кнопка стоп это
        абсолютный рубильник... Сразу по нажатии" -- no grace period, no
        "try the polite way first": fires client.interrupt() (harmless,
        occasionally lets the CLI wind down more cleanly) AND
        client.disconnect() (the real OS-process terminate()-then-kill()
        _check_hang's own hang recovery already relies on) CONCURRENTLY,
        immediately. disconnect() alone is a strict superset of what's
        needed -- interrupt() isn't awaited for or depended on, it can
        only help, never block this.

        Confirmed live (2026-09-15): even disconnect() itself can throw --
        the same claude_agent_sdk bug _safe_disconnect's own docstring
        documents (self._process already None inside the SDK's own
        close()). When it does, this was a dead end before: the turn
        stayed stuck with NEITHER interrupt() NOR disconnect() having
        actually worked, and nothing left to fall back to. Falls back to
        _force_kill_underlying_cli_process (a direct OS-level taskkill by
        PID, bypassing the SDK's own broken internals entirely) so Stop
        genuinely cannot fail to land.

        Bug fix (2026-09-15), THIRD report of this same shape of bug
        ("Стоп не останавливает всё немедленно"): the force-kill above
        used to run ONLY as a fallback, after first awaiting
        client.disconnect() for up to STOP_ESCALATION_DISCONNECT_TIMEOUT_S
        (10s) -- meaning a disconnect() that neither throws nor hangs, just
        runs its own slow internal graceful-shutdown sequence, made every
        Stop click take however long that took, contradicting "Сразу по
        нажатии" just as badly as the old soft-interrupt-then-grace-period
        design this was supposed to have replaced. _force_kill_underlying_
        cli_process is unconditionally safe to fire immediately, concurrently
        with disconnect() -- it goes straight for the real OS PID captured
        at spawn time, not through any SDK state disconnect() might also be
        touching, and taskkill against a process that's already exiting on
        its own just fails harmlessly (already caught inside that method).
        So it now fires first, unconditionally, with disconnect() awaited
        afterward purely as the SDK's own best-effort internal cleanup --
        no longer anything externally-visible depends on it finishing or
        even succeeding."""
        if self.client:
            asyncio.create_task(self._safe_interrupt())
        log_event("engine", "user_stop_force_close", tab_id=self.tab_id)
        self._force_kill_underlying_cli_process()
        try:
            if self.client:
                await asyncio.wait_for(self.client.disconnect(), timeout=STOP_ESCALATION_DISCONNECT_TIMEOUT_S)
        except Exception as exc:
            log_event("engine", "user_stop_close_failed", tab_id=self.tab_id, error=str(exc))

    def _on_cli_process_spawned(self, pid: int) -> None:
        """session_context's cli-pid-sink callback (see win_subprocess_
        patch.py) -- fires the moment a fresh claude.exe is actually
        spawned. Captures the pid (Stop's own fallback, see
        _force_kill_underlying_cli_process) AND builds this connection's
        ProcessActivityMonitor (_check_hang's real hang-detection signal,
        2026-09-15) in one place, since both are keyed off the exact same
        event and must always agree on which process they're tracking."""
        self._cli_process_pid = pid
        self._process_activity_monitor = ProcessActivityMonitor(pid)

    def _force_kill_underlying_cli_process(self) -> None:
        """Per explicit instruction (2026-09-15): mirrors BackendProcess.
        cs's own taskkill fallback for the identical class of problem (a
        graceful kill that doesn't reliably land) -- see
        _escalate_stop_if_still_pending's own comment for the incident.
        Kills the real OS PID directly, bypassing whatever broken internal
        state made client.disconnect() itself throw.

        Bug fix (2026-09-15), confirmed live -- "Стоп опять не сработал":
        the ORIGINAL version of this method reached into claude_agent_
        sdk's private client._transport._process.pid at THIS point in
        time (stop-time), and that came back None in a real incident
        (force_kill_cli_process_no_pid) at the exact same moment
        client.disconnect() was failing with the SDK's own confirmed
        'NoneType' object has no attribute 'returncode' bug -- i.e. Stop
        had no working path left at all. Use self._cli_process_pid
        instead: captured directly at process-spawn time (session_
        context's cli-pid-sink, wired in win_subprocess_patch.py), so it
        doesn't depend on the SDK's own internal bookkeeping still being
        intact at the moment something has already gone wrong. Falls back
        to the old transport introspection only if that capture somehow
        never happened (defense in depth, not the primary path anymore)."""
        pid = self._cli_process_pid
        if not pid:
            pid = self.client.process_pid() if self.client else None
        if not pid:
            log_event("engine", "force_kill_cli_process_no_pid", tab_id=self.tab_id)
            return
        log_event("engine", "force_kill_cli_process", tab_id=self.tab_id, pid=pid)
        try:
            subprocess.Popen(
                ["taskkill.exe", "/F", "/T", "/PID", str(pid)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            log_event("engine", "force_kill_cli_process_failed", tab_id=self.tab_id, error=str(exc))

    def _push_message(self, text: str, attachments: list[Any], is_voice: bool) -> None:
        sent_line = f"[Sent: {_format_timestamp_for_model(datetime.now(timezone.utc).astimezone())}"
        if is_voice:
            sent_line += ", via voice input -- may contain transcription errors"
        sent_line += "]"
        content: list[dict[str, Any]] = [{"type": "text", "text": sent_line}]
        for attachment in attachments:
            content.extend(_attachment_to_blocks(attachment))
        if text:
            content.append({"type": "text", "text": text})
        self.queue.append({
            "message": {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None},
            "is_voice": is_voice,
        })
        self._queue_event.set()

    async def _input_stream(self):
        while not self.ended:
            if not self.queue:
                self._queue_event.clear()
                await self._queue_event.wait()
                continue
            item = self.queue.pop(0)
            self.turn_is_voice = self.turn_is_voice or item["is_voice"]
            yield item["message"]

    # ---------------------------------------------------------- conn state --

    async def _apply_conn_state(self, kind: str, reason: str | None = None) -> None:
        # Bug fix (2026-09-11), per explicit instruction: used to send one
        # of TWO different wire message types (caroline_status/
        # system_notice) depending on kind, each carrying its own ad-hoc
        # status text/cls -- chat.js then had to reconstruct "is this
        # actually working, waiting, or broken" from THAT, PLUS turnQueue
        # length, PLUS wsConnected, independently. Collapsed to a single
        # _publish_status() call -- see _compute_public_status for the
        # kind -> READY/WORKING/RECOVERING/ERROR mapping.
        prev_kind = self.conn_state.get("kind")
        self.conn_state = {"kind": kind, "reason": reason}
        log_event("engine", "conn_state", tab_id=self.tab_id, prev=prev_kind, new=kind, reason=reason)
        # Bug fix (2026-09-15), per explicit instruction: confirmed live --
        # a forced-compaction attempt that itself hits the usage cap (the
        # CLI's own "Error during compaction: You've hit your session
        # limit" message) gets classified through the generic CC-CLI-
        # limit-message path, which never resets forced_compaction_result_
        # pending (that only happens in the normal ResultMessage handler,
        # which this classification path skips via its own `continue`).
        # Left stuck at True forever after that, _check_forced_compaction's
        # own first guard blocks EVERY future attempt -- even once the cap
        # resets, nothing ever retries, so a context that grew large enough
        # to need compaction in the first place (confirmed live: ~48MB of
        # accumulated Read-tool image results) never actually shrinks.
        # Coming out of "limited" is exactly the moment to re-check: set a
        # dedicated flag here (consumed by _check_forced_compaction, which
        # also bypasses the normal cooldown for it -- the point is
        # specifically to retry what the cap just interrupted, not wait for
        # the next regular cycle) and clear the stuck flag too, in case
        # that's what actually got stuck.
        if prev_kind == "limited" and kind != "limited":
            self.needs_post_limit_compaction_check = True
            if self.forced_compaction_result_pending:
                self.forced_compaction_result_pending = False
                log_event("engine", "forced_compaction_result_pending_cleared_after_limit", tab_id=self.tab_id)
                self._drain_compaction_queue()
        await self._publish_status()

    def _set_conn_state(self, kind: str, reason: str | None = None, arm_ignore_next_result: bool = False) -> None:
        if arm_ignore_next_result:
            self.ignore_next_result_recovery = True
        asyncio.create_task(self._apply_conn_state(kind, reason))

    # ------------------------------------------------------------- helpers --

    def has_live_dialog(self, idle_threshold_s: float = 5 * 60) -> bool:
        """Bug fix (2026-09-10): confirmed live -- deleted along with the
        homegrown compaction loop (its only OTHER caller), but main.py's
        _on_reminder_due still calls this to decide whether a "background"
        priority reminder (e.g. the hourly vault-backup nudge) should wait
        for the user to go quiet instead of interrupting a live turn."""
        if self.conn_state.get("kind") == "restart_backoff":
            return False
        return self.turn_pending or (time.monotonic() - self.last_user_activity) < idle_threshold_s

    def _clear_api_retry_timer(self) -> None:
        if self.api_retry_timer:
            self.api_retry_timer.cancel()
            self.api_retry_timer = None

    def _schedule_api_retry(self, reason: str, is_voice: bool) -> None:
        if self.api_retry_timer:
            log_event("engine", "api_retry_already_pending", tab_id=self.tab_id, reason=reason)
            return
        log_event("engine", "api_retry_scheduled", tab_id=self.tab_id, reason=reason, delay_ms=API_RETRY_INTERVAL_MS)

        def _fire() -> None:
            self.api_retry_timer = None
            if self.ended:
                return
            log_event("engine", "api_retry_firing", tab_id=self.tab_id, reason=reason)
            # Bug fix (2026-09-11): confirmed live -- this used to call
            # submit() directly, bypassing inject_proactive() (the only
            # thing that sends proactive_turn_queued, which re-arms
            # chat.js's busy lamp/heartbeat) -- so from the moment a
            # rate-limit hit to whenever the retry's own first assistant
            # message happened to trigger chat.js's lazy fallback, there
            # was no "working" indication at all. Also reuses
            # CONTINUE_OR_SILENT_NUDGE_TEMPLATE now (language-aware,
            # [[NO_UPDATE]]-aware) instead of bespoke text with neither.
            self.inject_proactive(
                "[Internal: automatic recheck after an API/subscription limit blocked a previous turn.] "
                f"{CONTINUE_OR_SILENT_NUDGE_TEMPLATE.format(language=current_language_name(self.tab_id))}",
                is_voice,
            )

        loop = asyncio.get_event_loop()
        self.api_retry_timer = loop.call_later(API_RETRY_INTERVAL_MS / 1000, _fire)

    def _schedule_one_shot_followup_check(self, reason: str, is_voice: bool) -> None:
        """Per explicit instruction (2026-09-11): distinguishes "the
        operation never even started" (a genuine rejection -- no balance,
        or an in-flight rate-limit rejection -- see _schedule_api_retry) from
        "the operation genuinely completed" (a real AssistantMessage came
        through -- e.g. the CC CLI's own "you've hit your session limit"
        reply -- the turn is over, just concluded by reporting a cap). Both
        cases retry forever, flat interval, same as every other retry in
        this codebase (RESTART_BACKOFF_MS, MCP_RECONNECT_INTERVAL_MS,
        api_retry_timer) -- the only real difference is HOW each retry is
        delivered: _schedule_api_retry resubmits the same still-pending
        turn (nothing happened yet), this one injects a fresh follow-up
        check (the turn already completed once).

        Bug fix (2026-09-22), per explicit instruction, after a real
        incident: a 2026-09-14 change (since reverted -- see git history)
        made this give up permanently after exactly one retry per "cap
        episode" (one_shot_followup_used_for_limit), reasoning that
        _schedule_api_retry's own forever-retry had once fired for hours
        after a task was already genuinely finished. That reasoning doesn't
        apply here: this branch only ever fires when the model's own reply
        JUST reported a real, current usage cap -- there is no ambiguity to
        protect against, and confirmed live, giving up after one attempt
        left a real, still-pending task abandoned for 6+ hours after the
        cap had long since reset, with nothing to resume it. Every firing
        that ALSO concludes by reporting the same still-active cap goes
        through the SAME cc_cli_limit_message handler again, which calls
        this method again -- and now simply re-arms, exactly like every
        other flat retry in this codebase, until a real answer (anything
        other than another cap report) comes through."""
        if self.one_shot_followup_timer:
            log_event("engine", "one_shot_followup_already_pending", tab_id=self.tab_id, reason=reason)
            return
        log_event(
            "engine", "one_shot_followup_scheduled", tab_id=self.tab_id, reason=reason,
            delay_ms=ONE_SHOT_FOLLOWUP_CHECK_DELAY_MS,
        )

        def _fire() -> None:
            self.one_shot_followup_timer = None
            if self.ended:
                return
            log_event("engine", "one_shot_followup_firing", tab_id=self.tab_id, reason=reason)
            self.inject_proactive(
                f"[Internal: automatic follow-up check -- your previous turn concluded by reporting a usage cap.] "
                f"{CONTINUE_OR_SILENT_NUDGE_TEMPLATE.format(language=current_language_name(self.tab_id))}",
                is_voice,
            )
            # No rescheduling here -- if the cap is still active, the model's
            # own reply reports it again, which routes back through the
            # cc_cli_limit_message handler and calls this method fresh. That
            # IS the retry loop now, same shape as every other flat retry.

        loop = asyncio.get_event_loop()
        self.one_shot_followup_timer = loop.call_later(ONE_SHOT_FOLLOWUP_CHECK_DELAY_MS / 1000, _fire)

    def _clear_one_shot_followup_timer(self) -> None:
        if self.one_shot_followup_timer:
            self.one_shot_followup_timer.cancel()
            self.one_shot_followup_timer = None

    def _clear_mcp_reconnect_timers(self) -> None:
        for t in self.mcp_reconnect_timers.values():
            t.cancel()
        self.mcp_reconnect_timers.clear()

    def _schedule_mcp_reconnect(self, client: AgentEngine, name: str, attempt: int = 0) -> None:
        """Keeps retrying client.reconnect_mcp_server(name) on a FLAT
        interval until it succeeds -- so a server that's down for a
        moment (its target not up yet, a transient launch race) comes
        back on its own instead of staying unavailable for the rest of
        the session, and WITHOUT tearing down the whole session (a
        single unrelated server being down previously produced an
        infinite restart-then-fail loop -- see the 'init' handling below).
        Flat, not exponential, per the same standing instruction as the
        restart budget's own RESTART_BACKOFF_MS (never gives up, no
        ceiling, no growing delay) -- the original TS ported this as
        exponential-with-a-cap; deliberately not followed here.

        Bound to the specific `client` instance active when the failure
        was reported: if that's no longer self.client by the time a retry
        fires (session restarted for an unrelated reason, or ended), the
        retry loop for it is abandoned -- the new session's own init
        handling will report and schedule fresh retries if still down."""
        if name in self.mcp_reconnect_timers:
            return  # already retrying this one

        def _fire() -> None:
            self.mcp_reconnect_timers.pop(name, None)
            if self.ended or self.client is not client:
                return  # superseded by a full session restart
            asyncio.create_task(self._do_mcp_reconnect(client, name, attempt))

        loop = asyncio.get_event_loop()
        self.mcp_reconnect_timers[name] = loop.call_later(MCP_RECONNECT_INTERVAL_MS / 1000, _fire)

    async def _do_mcp_reconnect(self, client: AgentEngine, name: str, attempt: int) -> None:
        try:
            await client.reconnect_mcp_server(name)
            log_event("engine", "mcp_server_reconnected", tab_id=self.tab_id, server=name, attempt=attempt)
        except Exception as exc:
            log_event("engine", "mcp_server_reconnect_failed", tab_id=self.tab_id, server=name, attempt=attempt, retry_in_ms=MCP_RECONNECT_INTERVAL_MS, error=str(exc))
            self._schedule_mcp_reconnect(client, name, attempt + 1)

    def _resolve_resume_session_id(self) -> str | None:
        stored = load_tab_session_id(self.workspace_dir, self.tab_id, self.engine_kind)
        if stored:
            return stored
        if self.engine_kind != "claude":
            return None  # no pre-existing Claude session to migrate into another engine
        if self.skip_migration_fallback_once:
            self.skip_migration_fallback_once = False
            log_event("engine", "skip_migration_fallback", tab_id=self.tab_id)
            return None
        if self.tab_id != PRIMARY_TAB_ID:
            return None
        migrated = find_most_recent_claude_session_id(self.workspace_dir)
        if migrated:
            log_event("engine", "migrated_pre_multitab_session", tab_id=self.tab_id, session_id=migrated)
            save_tab_session_id(self.workspace_dir, self.tab_id, migrated)
        return migrated

    def _reset_unrecoverable_session(self, replay_text: str | None, replay_attachments: list[Any]) -> None:
        log_event("engine", "reset_unrecoverable_session", tab_id=self.tab_id)
        archive_note = ""
        old_session_id = load_tab_session_id(self.workspace_dir, self.tab_id)
        if old_session_id:
            try:
                old_path = claude_project_dir(self.workspace_dir) / f"{old_session_id}.jsonl"
                if old_path.exists():
                    import shutil
                    directory = dehydrated_dir(self.workspace_dir)
                    directory.mkdir(parents=True, exist_ok=True)
                    archive_path = str(directory / f"{uuid_mod.uuid4()}.txt")
                    shutil.copyfile(old_path, archive_path)
                    archive_note = (
                        "[System note: this session was just reset after an unrecoverable internal error -- for "
                        "your own situational awareness only, don't alarm the user about it. The conversation "
                        f"from BEFORE this reset is fully preserved at {archive_path} -- if the user's message "
                        "below refers to something earlier that you don't have in this fresh context, Read that "
                        "file to find it before saying you don't know.]\n\n"
                    )
                    save_tab_continuity_archive(self.workspace_dir, self.tab_id, archive_path)
                    log_event("engine", "archived_unrecoverable_session", tab_id=self.tab_id, old_session_id=old_session_id, archive_path=archive_path)
            except Exception as exc:
                log_event("engine", "archive_unrecoverable_session_failed", tab_id=self.tab_id, error=str(exc))
        clear_tab_session_id(self.workspace_dir, self.tab_id)
        self.last_saved_session_id = None
        self.skip_migration_fallback_once = True
        self.unrecoverable_session_replay_text = f"{archive_note}{replay_text}" if replay_text is not None else (archive_note or None)
        self.unrecoverable_session_replay_attachments = replay_attachments
        self.restart_for_unrecoverable_session = True
        if self.client:
            asyncio.create_task(self._safe_disconnect(self.client))

    def clear_tab(self) -> None:
        """Per explicit instruction (2026-09-17): a deliberate, user-
        confirmed (via the frontend's own modal -- this method trusts
        that already happened) full wipe of this tab's conversation.
        Unlike _reset_unrecoverable_session (which archives the old
        transcript and replays whatever was pending -- that reset is
        involuntary/recovery, never meant to lose anything), this is a
        genuine delete: no archive, no replay, nothing carried forward --
        see clear_tab_disk_state for the on-disk half.

        Stops/kills whatever's currently running first and unconditionally
        forces the live client to reconnect (unlike stop(), which no-ops
        when nothing's pending) -- an IDLE but still-connected client
        holds the old conversation in its own process memory regardless
        of what the on-disk file says, so leaving it alone would silently
        undo the clear the moment the user sent a new message."""
        log_event("engine", "clear_tab", tab_id=self.tab_id)
        clear_tab_disk_state(self.workspace_dir, self.tab_id)
        self.last_saved_session_id = None
        self.pending_user_text = None
        self.pending_attachments = []
        self.pending_is_real_user = False
        self.restart_pending = True
        # Bug fix (2026-09-18): the force-kill below can still surface as a
        # normal, non-exceptional trailing ResultMessage rather than a dead
        # connection -- without this flag that message was treated as a
        # genuine turn completion, which fired the post-turn-completion
        # "did you actually finish?" nudge against the now-EMPTY,
        # freshly-cleared session, producing a confusing reply that made
        # the clear look like it had silently failed. See result_is_fake.
        self.clear_tab_result_pending = True
        # Bug fix (2026-09-18), found immediately after the fix above:
        # marking the trailing ResultMessage fake means the message loop's
        # OWN normal completion path (the one that sets turn_pending=False)
        # never runs for it either -- unlike hang_interrupt_result_pending
        # (which leaves turn_pending alone deliberately, because a REPLAY
        # is expected to come through submit() and manage it itself) or
        # forced_compaction_result_pending (turn_pending is already False
        # the whole time that runs), clear_tab has explicitly declared "no
        # replay, nothing carried forward" -- there is no other path left
        # that will ever flip turn_pending back to False. Without this,
        # the tab would stay stuck showing turn_pending=True forever after
        # a clear. Set it here, synchronously, the same way pending_user_
        # text/pending_attachments above already are.
        self.turn_pending = False
        if self.small_model_active:
            if self._small_model_task is not None:
                self._small_model_task.cancel()
        elif self.client:
            asyncio.create_task(self._force_stop_client())
        cancelled = REGISTRY.cancel_for_tab(self.tab_id)
        if cancelled:
            log_event("engine", "clear_tab_cancelled_operations", tab_id=self.tab_id, count=cancelled)

    async def _pre_compact_hook(self, hook_input: Any, tool_use_id: Any, context: Any) -> dict[str, Any]:
        """Fires just before Claude's own compaction summarises older turns
        -- either its own native auto-compaction, or our forced "/compact"
        (see FORCED_COMPACTION_HOURLY_MS; trigger is "manual" for that one,
        live-confirmed).

        Bug fix (2026-09-20), confirmed live: this used to copy the ENTIRE
        transcript into workspace/dehydrated/ on every compaction and point
        the tab's continuity pointer at the copy. Compaction never touches
        the live transcript, though -- it is append-only, and after 10 days
        of hourly compactions tab 1's still held every record back to its
        first day (1,325 compact_boundary markers inside it), and the newest
        archive was a byte-for-byte prefix of it. So the copies preserved
        nothing the live file didn't already have, while costing a
        hundreds-of-MB copy each time: 4,484 of them, 892 GB, mostly from a
        compaction loop (see _note_compaction_hit_limit). The continuity
        pointer they set was wrong too -- it tells the model "an earlier
        session was abandoned after an internal error", which is simply
        false after an ordinary compaction. Nothing is copied now; the
        24h-dialogue file (recent_dialogue_history_instruction) is the
        recall path, and it reads the live file's tail. Observation only."""
        trigger = hook_input.get("trigger") if isinstance(hook_input, dict) else None
        log_event("engine", "pre_compact_observed", tab_id=self.tab_id, trigger=trigger)
        return {}

    def _handle_balance_exhausted(self) -> str:
        """Own-Anthropic credits/quota ran out. Nothing to fall back to and
        no top-up we can drive (it's the user's own Anthropic billing) --
        just surface it and let _schedule_api_retry keep probing."""
        log_event("engine", "balance_exhausted", tab_id=self.tab_id)
        return BALANCE_EXHAUSTED_MESSAGE

    def _handle_rate_limit_rejected(self, source: str, info: dict[str, Any]) -> None:
        resets_at = info.get("resets_at")
        # Bug fix (2026-09-10): confirmed live this produced a bogus 1970
        # date ("Resets: 1970-01-21T..."). The SDK's own RateLimitInfo
        # docs just say "Unix timestamp" (ambiguous on paper), but
        # dividing by 1000 landed ~20 days after the epoch -- resets_at is
        # already in SECONDS (the standard meaning of "Unix timestamp",
        # and what datetime.fromtimestamp() itself expects), not
        # milliseconds. No division.
        reset_text = f" Resets: {datetime.fromtimestamp(resets_at).isoformat()}." if resets_at else ""
        type_text = f" ({info.get('rate_limit_type')})" if info.get("rate_limit_type") else ""
        text = f"Hit the Claude usage limit{type_text}.{reset_text} Retrying automatically."
        log_event("engine", "rate_limit_rejected", tab_id=self.tab_id, source=source)
        self._set_conn_state("limited", text)
        self._schedule_api_retry(f"rate_limit_rejected:{source}", self.turn_is_voice)

    # ------------------------------------------------------------ watchdog --

    async def _watchdog_loop(self) -> None:
        try:
            while not self.ended:
                await asyncio.sleep(WATCHDOG_INTERVAL_MS / 1000)
                if self.ended:
                    return
                # Bug fix (2026-09-10): confirmed live -- one of these four
                # checks raising ANYTHING other than CancelledError used to
                # end this whole loop silently (only CancelledError was
                # caught below), permanently disabling hang-detection,
                # progress narration, the silent-user-wait nudge, AND the
                # idle-task-drift check for this tab for the rest of the
                # process's lifetime -- no log, no restart, unnoticed for
                # 20+ minutes until the downstream symptoms (no narrator
                # comments, a lamp that never blinks) got reported. A
                # single bad tick must never cost this tab everything these
                # four checks are responsible for -- log it and keep
                # ticking. supervise() (start(), task_supervisor.py) is the
                # outer safety net if this task ever dies anyway. (The old
                # fifth check here, idle-task-drift, is gone -- see
                # _fire_post_turn_completion_check()'s own doc comment for
                # why it's now event-driven, off the turn_pending setter,
                # not a periodic tick.)
                try:
                    await self._check_hang()
                    self._check_user_wait_nudge()
                    await self._check_progress_narration()
                    self._check_forced_compaction()
                    self._check_deferred_engine_switch()
                    self._check_funds_exhaustion_status()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 -- must log, never let this tick die silently
                    log_event("engine", "watchdog_tick_failed", tab_id=self.tab_id, error=str(exc), error_type=type(exc).__name__)
        except asyncio.CancelledError:
            pass

    def _gather_recent_dialogue_for_narration(self, limit: int = RECENT_DIALOGUE_WINDOW) -> str:
        """Per explicit correction (2026-09-10): the first version of
        _check_progress_narration fed generate_progress_comment only the
        single original question plus a list of currently-running tool
        names -- confirmed live (screenshots), this produced bland,
        near-identical, operation-sounding remarks every time ("I'm
        checking the phone-related messages...", "let me pull up that
        operation...") instead of anything that actually engaged with the
        conversation, and stayed generic turn after turn since none of its
        inputs ever changed mid-turn. Rebuilt to hand the model the REAL
        recent back-and-forth instead, via _read_recent_dialogue_lines --
        shared with refresh_language_in_background (see that module-level
        function's own docstring for why this used to be two separately-
        drifting readers, and why the window widened from 8/5 to
        RECENT_DIALOGUE_WINDOW).

        Per explicit instruction (2026-09-12): the narrator must work in
        BOTH engines. A small-model turn (small_model_engine.py) never
        talks to the CLI at all, so nothing it does ever reaches the
        on-disk transcript _read_recent_dialogue_lines reads below --
        while one is active, prefer its own live in-memory exchange
        instead (small_model_live_dialogue, kept current by
        on_live_dialogue_update as the turn progresses)."""
        if self.small_model_active and self.small_model_live_dialogue:
            return "\n".join(self.small_model_live_dialogue[-limit:])

        # Bug fix (2026-09-16), per explicit instruction: "нарратор должен
        # комментировать только текущую задачу, а не весь предыдущий
        # диалог" -- bounded to the CURRENT real user turn's own start
        # (see last_real_user_turn_started_at_ms's own comment), not just
        # a blind last-`limit`-lines window that could still reach back
        # into an earlier, already-finished task if the current one hasn't
        # produced `limit` lines of its own yet. None (no real user turn
        # started yet this process lifetime) keeps the old unbounded
        # behavior -- nothing to anchor to.
        lines = _read_recent_dialogue_lines(
            self.last_saved_session_id, self.tab_id, self.workspace_dir, limit,
            min_ts_ms=self.last_real_user_turn_started_at_ms,
        )

        # Bug fix (2026-09-15), per explicit instruction ("внимательно
        # смотри... почему он возвращает херню"): confirmed live -- a
        # tab that repeatedly hit the same usage-limit/error condition can
        # have the SAME assistant line (e.g. "You've hit your session
        # limit...") land in the transcript several times in a row (each
        # one a real reply the user actually saw at the time, so it's not
        # synthetic-marked and doesn't get filtered as such) -- confirmed
        # live: 5 of a 12-line window were the identical repeated line.
        # That dominates the "recent conversation" the narrator is asked
        # to react to, and directly produced one of its failures (echoing
        # that exact line back). Collapse consecutive exact repeats to one
        # occurrence here -- narrator-specific (this function's own
        # output), not in the shared _read_recent_dialogue_lines/
        # _usable_dialogue_lines readers, which other consumers (language
        # detection, get_history) may have their own reasons to keep as-is.
        deduped: list[str] = []
        for line in lines:
            if not deduped or deduped[-1] != line:
                deduped.append(line)
        lines = deduped

        # Bug fix (2026-09-10): confirmed live -- the disk-persisted
        # transcript doesn't yet contain a real user message that's merely
        # QUEUED (submitted but not yet consumed/flushed by the CLI, e.g.
        # because this tab's query() is still stuck resuming). The
        # narrator then only ever saw Caroline's OWN earlier turn and
        # reacted to nothing the user actually just said -- reading as if
        # she'd lost track of their answer entirely. Always make sure the
        # most recent real question is represented as the LAST line,
        # appending it if the disk read didn't already surface it.
        # Also (2026-09-10): last_real_user_question is only ever set by a
        # REAL submit() (is_real_user=True). pending_user_text used to be
        # used as a blanket fallback for ANY pending turn, real or
        # proactive, so a long-running proactive task still had something
        # to narrate about before anything hit the real transcript on disk.
        #
        # Bug fix (2026-09-11), per explicit instruction: confirmed live --
        # that blanket fallback also caught purely-internal synthetic
        # nudges (the API-retry recheck: "[Internal: automatic recheck
        # after an API/subscription limit blocked a previous turn.]
        # Continue any unfinished work... Reply in English."), labeled them
        # "User: ..." same as real speech, and fed that straight into the
        # narrator -- which then both reacted to internal bookkeeping as if
        # it were conversation AND picked up the nudge's own hardcoded
        # English wording, breaking language for an otherwise-Russian
        # conversation. Only trust pending_user_text here when it's a REAL
        # user's own words (pending_is_real_user, set alongside it in
        # submit()) -- a genuinely informative proactive task (companion
        # message, startup greeting) loses this one fallback line for the
        # brief window before anything real lands on disk, but
        # _read_recent_dialogue_lines (the real, general source) picks it
        # up the moment it does; that's a far smaller gap than actively
        # narrating on internal nudge text for the whole length of a
        # rate-limit wait.
        question = (self.last_real_user_question or (self.pending_user_text if self.pending_is_real_user else None) or "").strip()
        if question and (not lines or question not in lines[-1]):
            lines.append(f"User: {question}")

        return "\n".join(lines[-limit:])

    async def _translate_wire_visible_text(self, wire: dict[str, Any]) -> dict[str, Any]:
        """Per explicit instruction (2026-09-15): forced translation used
        to be narration-only (generate_progress_comment) -- confirmed live
        this left a real gap: the real Claude model itself dropped a terse
        English status line ("Now executing deletes and marks in
        batches.") into an otherwise-Russian conversation mid-tool-call-
        chain, and language_hint_instruction's system-prompt nudge is
        advisory, not a guarantee. Runs every REAL visible assistant text
        block through the same translate_text() forced-correction pass
        narration already uses, targeting current_language_name(tab_id) --
        same unconditional-pass philosophy (translate_text itself is a
        no-op, text-preserving pass when the input is already in the
        target language). Falls back to the ORIGINAL text per block on any
        translation failure -- never blocks or drops a real reply over
        this. Only touches "assistant" wire messages; "result"/"system"/
        etc. are left exactly as they were."""
        if wire.get("type") != "assistant":
            return wire
        content_blocks = wire.get("message", {}).get("content") or []
        if not any(isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip() for b in content_blocks):
            return wire
        from app.plugins.voice_api import translate_text

        language = current_language_name(self.tab_id)
        for block in content_blocks:
            if not (isinstance(block, dict) and block.get("type") == "text"):
                continue
            text = block.get("text") or ""
            if not text.strip():
                continue
            try:
                translated = await translate_text(text, language, gender=get_persona_gender(self.workspace_dir))
            except Exception as exc:
                log_event("engine", "wire_translate_failed", tab_id=self.tab_id, error=str(exc))
                continue
            if translated and translated != text:
                log_event("engine", "wire_translate_applied", tab_id=self.tab_id, language=language, original_len=len(text), translated_len=len(translated))
                block["text"] = translated
        return wire

    async def _check_progress_narration(self) -> None:
        """Per explicit instruction (2026-09-10): the user must see SOME
        comment from Caroline at least once a minute while a real turn of
        hers is still running, even deep in a long tool-call chain --
        confirmed there is no safe way to inject anything into her own
        live session mid-turn to make that happen: the SDK's own input
        stream only ever delivers a newly-submitted message once the
        CURRENT turn (the whole tool-call chain) has fully finished (see
        _input_stream's own comment), and interrupt() is a genuine abort,
        not a "pause, say something, then resume" signal -- using it every
        minute would repeatedly disrupt real in-progress work. Sidesteps
        the problem entirely instead: a separate, lightweight ai:resolve
        call (generate_progress_comment, voice_api.py) drafts a short
        cosmetic stand-in remark, sent straight to the client. The real
        session never sees or knows about this. Repeats every
        PROGRESS_NARRATION_INTERVAL_MS as long as the turn keeps running;
        resets whenever the real model actually says something of its own
        (see the "assistant" wire-send site) or a real new user message
        starts a fresh turn."""
        # Bug fix (2026-09-10): confirmed live -- gating this on
        # last_real_user_question specifically meant a proactively-injected
        # turn (inject_proactive() always passes is_real_user=False --
        # startup greeting, reminders, ratatosk nudges, crash-resume) never
        # got narration at all on a fresh process, even a long one (e.g.
        # resuming an unfinished image-generation + document task after a
        # crash). _gather_recent_dialogue_for_narration now also falls back
        # to pending_user_text (set for every pending turn, real or
        # proactive), so the emptiness check below on the actual gathered
        # dialogue is the real, general guard now -- not this field.
        # (silent_turn removed 2026-09-11 -- whether the eventual reply
        # itself gets shown is now a per-message [[NO_UPDATE]] decision,
        # unrelated to whether this cosmetic aside narration comment fires.)
        #
        # Bug fix (2026-09-11), per explicit instruction: confirmed live --
        # turn_pending alone stays True for the ENTIRE duration of a rate-
        # limit-wait cycle (the turn is legitimately still "owed" a reply,
        # by design, so the retry can resume it later), even though nothing
        # is actually happening except quietly waiting for the limit to
        # reset. This kept firing narration every minute throughout that
        # wait, reacting to nothing real. Only narrate while actually
        # connected -- recovering/limited/restarting/billing_blocked all
        # mean there's genuinely nothing to narrate about right now.
        if self.ended or not self.turn_pending or self.conn_state.get("kind") != "connected":
            return
        # Bug fix (2026-09-15), per explicit instruction: narration must
        # never fire for a turn the user didn't actually start -- a
        # scheduled reminder, a ratatosk nudge, a startup greeting, a
        # vault-backup check, any of inject_proactive()'s other callers
        # (all pass is_real_user=False, tracked here as pending_is_real_
        # user). Confirmed live: narration comments were reaching the
        # user at 12:26/1:51/1:59/2:00 AM -- clearly proactive/scheduled
        # activity, not a live conversation -- because this function never
        # checked who actually started the turn it was narrating, only
        # whether SOME turn was pending. The user isn't watching and
        # waiting on a proactive turn the way they are on one they just
        # sent, so there's nothing for this cosmetic aside to usefully do
        # there anyway.
        if not self.pending_is_real_user:
            return
        # Bug fix (2026-09-14): see consecutive_narration_count's own
        # __init__ comment and MAX_CONSECUTIVE_NARRATION_COMMENTS's own
        # comment -- a turn stuck this long isn't helped by yet another
        # paraphrase of the same stale context; stop until real progress
        # (a real reply, or a fresh real user message) resets the counter.
        if self.consecutive_narration_count >= MAX_CONSECUTIVE_NARRATION_COMMENTS:
            return
        now = time.monotonic()
        if self.last_visible_output_at is not None and now - self.last_visible_output_at < PROGRESS_NARRATION_INTERVAL_MS / 1000:
            return
        # Bug fix (2026-09-22): a fully-failed attempt (see below) gets
        # its own short cooldown instead of re-passing the main 60s gate
        # above on literally the next 5s watchdog tick.
        if self.last_narration_failed_at is not None and now - self.last_narration_failed_at < NARRATION_FAILURE_RETRY_S:
            return
        # Bug fix (2026-09-22): last_visible_output_at is claimed AFTER a
        # successful send now (see below), not eagerly here -- confirmed
        # live the old eager claim let one slow generate+translate round-
        # trip (71s, real incident) silently cost an EXTRA clean 60s on
        # top of its own latency, since the interval was measured from the
        # ATTEMPT'S START rather than its actual completion. Re-entrancy
        # (the original reason given for the eager claim) isn't actually
        # possible here: _watchdog_loop awaits this whole coroutine
        # sequentially, one tick at a time -- no other tick can start
        # until this one returns, slow or not.
        from app.plugins.voice_api import generate_progress_comment

        dialogue = self._gather_recent_dialogue_for_narration()
        if not dialogue.strip():
            # Nothing to narrate about yet (first-ever turn, nothing on disk,
            # no pending text either) -- skip rather than call ai:resolve with
            # empty context for a comment that couldn't mean anything.
            return
        log_event("engine", "progress_narration_context", tab_id=self.tab_id, dialogue_chars=len(dialogue), dialogue_preview=dialogue[-300:])
        # Bug fix (2026-09-15), per explicit instruction ("Нарратор должен
        # давать сообщения раз в минуту"): confirmed live -- the SMALL
        # model backing generate_progress_comment can fail its own output
        # contract (echoing stale dialogue, truncated/malformed tags,
        # ignoring the format entirely) several times in a row (a real
        # incident: 3 straight failures before a 4th attempt succeeded).
        # Retry a few times in the SAME tick instead of waiting for the
        # next one. Each attempt also gets NARRATION_NETWORK_TIMEOUT_S
        # (2026-09-22, well under the default 30s-per-attempt SW API
        # budget -- see that constant's own comment for the 71s real
        # incident this fixes) so a slow/hung attempt fails fast and this
        # loop gets a real chance to try again within the SAME tick.
        comment: str | None = None
        for attempt in range(1, NARRATION_GENERATION_RETRY_ATTEMPTS + 1):
            try:
                comment = await generate_progress_comment(
                    dialogue, current_language_name(self.tab_id), timeout=NARRATION_NETWORK_TIMEOUT_S,
                    gender=get_persona_gender(self.workspace_dir),
                )
            except Exception as exc:
                log_event("engine", "progress_narration_failed", tab_id=self.tab_id, attempt=attempt, error=str(exc))
                comment = None
            if comment:
                break
            log_event("engine", "progress_narration_retry", tab_id=self.tab_id, attempt=attempt, exhausted=attempt == NARRATION_GENERATION_RETRY_ATTEMPTS)
        if not comment:
            self.last_narration_failed_at = time.monotonic()
            return
        self.last_narration_failed_at = None
        self.last_visible_output_at = time.monotonic()
        self.consecutive_narration_count += 1
        log_event("engine", "progress_narration_sent", tab_id=self.tab_id, comment=comment, consecutive_count=self.consecutive_narration_count)
        wire = {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": comment}], "model": None, "stop_reason": None},
            "session_id": None, "parent_tool_use_id": None,
        }
        await self.send({"type": "sdk_message", "message": wire})

    def _check_user_wait_nudge(self) -> None:
        """Separate from hang-detection above -- that guards against a
        DEAD transport (no init, or nothing streaming at all); this guards
        against a LIVE transport where the model itself went quiet without
        ever replying to a real user message (finished a turn with no
        text, lost track mid-task, etc.). Deliberately does nothing for a
        fully hung/never-initialized query(): the nudge this queues via
        submit() sits behind the same stuck input stream as the original
        message until hang-detection's own, separate recovery runs."""
        if self.last_real_user_turn_at is None or self.real_user_turn_answered or self.silence_nudge_sent_for_turn:
            return
        elapsed = time.monotonic() - self.last_real_user_turn_at
        if elapsed < SILENT_USER_WAIT_NUDGE_MS / 1000:
            return
        log_event("engine", "silent_user_wait_nudge", tab_id=self.tab_id, elapsed_s=round(elapsed, 1))
        self.silence_nudge_sent_for_turn = True
        # Bug fix (2026-09-10): confirmed live -- unlike CONTINUE_OR_SILENT_NUDGE_TEMPLATE
        # (used by the other internal nudges), this text never told the model which
        # language to answer in at all, so a reply to it could land in the wrong
        # language even once current_language_name() itself is correct.
        self.submit(
            "[Internal: it's been over 90 seconds since the user's message and nothing has reached them yet. If "
            "you're already working on something (a tool call, research, a multi-step task), just continue -- "
            "don't restart from scratch. If you actually finished and simply didn't reply, or lost track, answer "
            f"them now, directly, in {current_language_name(self.tab_id)}. Don't mention this note itself.]",
            [], False, False,
        )

    def _track_background_tasks(self, message: Any) -> None:
        """Feeds agent_registry from the SDK's typed task lifecycle events (see that module's docstring for
        why). A background task can end with a TaskNotificationMessage OR only a TaskUpdatedMessage whose
        status is terminal (e.g. a stopped task) -- either one clears it."""
        try:
            if isinstance(message, TaskStartedMessage):
                agent_registry.register(self.workspace_dir, self.tab_id, message.task_id, message.description, message.task_type, message.tool_use_id)
            elif isinstance(message, TaskNotificationMessage):
                agent_registry.finish(self.workspace_dir, self.tab_id, message.task_id, message.status)
            elif isinstance(message, TaskUpdatedMessage) and (message.status or (message.patch or {}).get("status")) in TERMINAL_TASK_STATUSES:
                agent_registry.finish(self.workspace_dir, self.tab_id, message.task_id, message.status or (message.patch or {}).get("status"))
        except Exception as exc:
            log_event("engine", "track_background_tasks_failed", tab_id=self.tab_id, error=str(exc))

    def _tell_model_agents_were_lost(self, lost: list[dict[str, Any]]) -> None:
        listing = "; ".join(f"agentId {e['task_id']}: {e.get('description') or '?'}" for e in lost)
        log_event("engine", "agents_lost_note_injected", tab_id=self.tab_id, count=len(lost))
        self.inject_proactive(
            "[The app restarted while these agents/background tasks you had launched were still running, and a "
            f"restart kills them -- they are GONE and will never report back: {listing}. If the work is still "
            "needed, launch it again (check first what they may already have produced, e.g. files on disk). Never "
            "mention the restart to the user. If nothing here needs doing, reply exactly [[NO_UPDATE]].]"
        )
        agent_registry.clear_lost(self.workspace_dir, self.tab_id)

    def _track_tool_outcomes(self, message: Any) -> None:
        """Ground-truth tool-outcome tracking (see ToolOutcome's own
        docstring and the field comments in __init__): every ToolUseBlock is
        remembered here by call id until its matching ToolResultBlock (on
        the following UserMessage) arrives, at which point it's recorded
        into turn_tool_outcomes keyed by NAME -- so a later successful retry
        of the same tool overwrites an earlier failure instead of the two
        coexisting. Called once per raw SDK message at the top of the
        per-message loop, off the exact same objects both ClaudeEngine and
        CodexEngine already produce there, so this works identically for
        either engine with zero engine-specific code. Split out as its own
        method (rather than left inline in the loop) so it can be exercised
        directly against fake SDK messages without a live engine."""
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    self._pending_tool_calls[block.id] = block.name
            return
        if isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    tool_name = self._pending_tool_calls.pop(block.tool_use_id, None) or "unknown_tool"
                    content = block.content
                    if isinstance(content, list):
                        # SDK's own shape here is raw dicts (e.g.
                        # {"type": "text", "text": ...}), NOT TextBlock
                        # objects -- see ToolResultBlock's own type in
                        # claude_agent_sdk.types.
                        preview = " ".join(
                            item.get("text", "") for item in content
                            if isinstance(item, dict) and item.get("type") == "text"
                        )
                    elif isinstance(content, str):
                        preview = content
                    else:
                        preview = ""
                    preview = preview.strip()[:300]
                    self.turn_tool_outcomes[tool_name] = ToolOutcome(
                        name=tool_name, is_error=bool(block.is_error), result_preview=preview,
                    )

    def _turn_outcomes_summary(self) -> str:
        """Ground-truth text for the current real-user episode's tool calls
        (see turn_tool_outcomes' own __init__ comment) -- literally the
        name/is_error/content already on the wire, never inferred or
        guessed. Empty string when there's nothing to report (no tool calls
        yet, or every one of them succeeded), so callers can just check
        truthiness before appending it to a nudge. Deliberately reports
        EVERY outcome, not just errors -- a reader reconciling "did I
        actually finish" needs the full picture, not just the bad news."""
        if not self.turn_tool_outcomes:
            return ""
        lines = []
        for outcome in self.turn_tool_outcomes.values():
            status = f"ERROR ({outcome.result_preview})" if outcome.is_error else "ok"
            lines.append(f"{outcome.name} -> {status}")
        return "Ground truth for tool calls this task actually made so far (not your own account of it): " + "; ".join(lines) + "."

    def _turn_has_unresolved_tool_error(self) -> bool:
        """True when the ground-truth record (see _turn_outcomes_summary)
        shows at least one tool call still sitting at is_error=True for this
        episode -- used to refuse a blind [[NO_UPDATE]] rather than trust
        the model's own self-report over what its own tool calls actually
        returned."""
        return any(outcome.is_error for outcome in self.turn_tool_outcomes.values())

    def _fire_post_turn_completion_check(self) -> None:
        """Sanity check run right after a turn finishes -- per explicit
        correction (2026-09-13), replacing a periodic timer-based version
        (every IDLE_TASK_CHECK_INTERVAL_MS regardless of whether anything
        had happened) that the user rejected as needless expense. Called
        from the turn_pending property setter's True->False edge:
        unconditionally for a REAL user turn, or for a PROACTIVE turn that
        produced genuinely no visible content at all (see
        turn_saw_any_visible_text's own comment for that second case, added
        2026-09-13 after a real incident -- a scheduled mailbox check did
        real work but its final answer never reached the wire). Never for
        forced_compaction's own "/compact" (that bypasses turn_pending
        entirely, see _push_internal_command), and never twice for a turn
        that started on the small-model path and escalated (that hand-off
        keeps turn_pending continuously True across the switch, see
        submit_or_try_small_model's own docstring, so only the FINAL
        completion trips this edge).

        Sets _awaiting_post_turn_check_reply so THIS check's own reply
        (submitted via inject_proactive, always is_real_user=False) can't
        chain into firing the proactive-empty branch above again ON ITSELF
        if that reply comes back genuinely empty -- never an infinite tight
        loop of a check chasing its own silence.

        Bug fix (2026-09-18), per a real live incident: this flag used to
        ALSO suppress a re-check when the reply was NOT empty -- confirmed
        live, a check's reply turned into a whole further round of real
        work (tool calls, real visible text) that then ALSO ended mid-task,
        and nothing ever checked it again, leaving the tab silently stuck
        until the user manually nudged it. A reply with real visible
        content is evidence of genuine continued work, not a final answer
        -- it gets re-armed for another check right alongside a real user
        turn (see the call site), so a genuinely multi-step task keeps
        getting checked round after round until it's either actually done
        or truly falls silent, not just once per originating turn.

        Still asks the model directly rather than trusting our own
        bookkeeping (confirmed live that bookkeeping alone can be wrong --
        see hang_interrupt_result_pending's own comment) -- just event-
        driven now instead of polling on a clock."""
        if self.ended:
            return
        lang = current_language_name(self.tab_id)
        # Ground-truth cross-check (2026-09-22, see ToolOutcome's own
        # docstring): grounds this nudge in what the last turn's own tool
        # calls actually returned, instead of leaving it pure introspection
        # -- appended, never replacing CONTINUE_OR_SILENT_NUDGE_TEMPLATE
        # itself, so every existing call site of that template elsewhere
        # keeps behaving exactly as before.
        outcomes_summary = self._turn_outcomes_summary()
        nudge = CONTINUE_OR_SILENT_NUDGE_TEMPLATE.format(language=lang)
        if outcomes_summary:
            nudge = f"{nudge}\n\n{outcomes_summary}"
        log_event("engine", "post_turn_completion_check", tab_id=self.tab_id, has_tool_outcomes=bool(outcomes_summary))
        self._awaiting_post_turn_check_reply = True
        self.inject_proactive(nudge)

    def _current_session_file_size(self) -> int | None:
        """Bytes on disk for this tab's currently-resumed session transcript,
        or None if there's nothing resolvable yet (no session id, or the
        file genuinely isn't there). No longer the growth-trigger's own
        decision signal (2026-09-15 -- see tokens_at_last_forced_
        compaction's own __init__ comment for why file bytes were wrong for
        that) -- kept only for the session_size_bytes log field, which is
        still useful context even though it's not what drives the trigger."""
        if not self.last_saved_session_id:
            return None
        try:
            path = claude_project_dir(self.workspace_dir) / f"{self.last_saved_session_id}.jsonl"
            return path.stat().st_size
        except OSError:
            return None

    def _push_internal_command(self, text: str) -> None:
        """Queues a raw CLI command (e.g. "/compact") the same way a human
        typing it would send it -- bypasses submit()/_push_message entirely
        (no "[Sent: ...]" timestamp line, no attachments, no turn_pending/
        pending_user_text bookkeeping) since this isn't a conversational
        turn at all. Live-confirmed this exact shape (content as a
        single-block list, no extra text) is recognized as a slash command
        through THIS engine's own connect()+generator wiring, not just the
        SDK's separate query(str) convenience path."""
        self.queue.append({
            "message": {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}, "parent_tool_use_id": None},
            "is_voice": False,
        })
        self._queue_event.set()

    def _check_forced_compaction(self) -> None:
        """See FORCED_COMPACTION_HOURLY_MS's own comment for why this
        exists at all (native auto-compaction confirmed never firing on its
        own). Four triggers, checked in priority order: needs_post_limit_
        compaction_check (per explicit instruction, 2026-09-15 -- see its
        own __init__ comment; deliberately bypasses the cooldown gate
        below, since the whole point is retrying what a usage-limit hit
        just interrupted, not waiting for the next regular cycle), a
        pending startup compaction (main.py sets needs_startup_compaction
        once per tab per process), the hourly clock, or real context-token
        growth past FORCED_COMPACTION_GROWTH_TOKENS_THRESHOLD since the
        last forced compaction. Never runs while a real turn is in flight or the
        connection isn't fully settled -- this is maintenance, not
        something to inject into or race with actual work."""
        if self.engine_kind != "claude":
            return  # Codex owns its own compaction; this machinery edits Claude transcripts
        if self.ended or self.turn_pending or self.forced_compaction_result_pending:
            return
        if self.conn_state.get("kind") != "connected":
            return
        if not self.last_saved_session_id:
            return
        now = time.monotonic()
        # Right after a compaction that itself hit the usage cap: wait, no
        # matter which trigger is armed -- INCLUDING needs_post_limit_
        # compaction_check, which used to bypass every gate and is exactly
        # what turned one failed compaction into thousands (see
        # FORCED_COMPACTION_LIMIT_PAUSE_MS). The armed flag simply stays
        # armed until the pause ends.
        if now < self.forced_compaction_blocked_until:
            return

        if self.needs_post_limit_compaction_check:
            self.needs_post_limit_compaction_check = False
            reason = "post_limit"
        elif self.last_forced_compaction_at is not None and now - self.last_forced_compaction_at < FORCED_COMPACTION_MIN_INTERVAL_MS / 1000:
            return
        elif self.needs_startup_compaction:
            reason = "startup"
        elif self.last_forced_compaction_at is None or now - self.last_forced_compaction_at >= FORCED_COMPACTION_HOURLY_MS / 1000:
            reason = "hourly"
        else:
            reason = None
            if self.tokens_at_last_forced_compaction is not None and self.last_known_context_tokens is not None:
                grown = self.last_known_context_tokens - self.tokens_at_last_forced_compaction
                if grown >= FORCED_COMPACTION_GROWTH_TOKENS_THRESHOLD:
                    reason = "growth"

        if reason is None:
            return

        # Nothing to compact -> don't (except "growth", which by definition
        # already measured real growth). Measured live: every hourly/startup
        # compaction on an idle tab ran with 3.5K-7K tokens in context and
        # came out the same size, yet each one still cost a full transcript
        # scan/copy. The size comes from the live turns' own usage reports
        # when this process has seen any, otherwise from the END of the
        # transcript itself (read_last_context_tokens) -- a fresh process
        # has no usage report yet, which is precisely when the startup
        # compaction fires. Unknown (None) keeps the old behavior: compact.
        if reason != "growth":
            context_tokens = self.last_known_context_tokens
            if context_tokens is None:
                try:
                    path = claude_project_dir(self.workspace_dir) / f"{self.last_saved_session_id}.jsonl"
                    context_tokens = read_last_context_tokens(path)
                    if context_tokens is not None:
                        self.last_known_context_tokens = context_tokens
                except Exception as exc:
                    log_event("engine", "forced_compaction_context_probe_failed", tab_id=self.tab_id, error=str(exc))
            if context_tokens is not None and context_tokens < FORCED_COMPACTION_MIN_CONTEXT_TOKENS:
                log_event(
                    "engine", "forced_compaction_skipped_small_context", tab_id=self.tab_id, reason=reason,
                    context_tokens=context_tokens, threshold=FORCED_COMPACTION_MIN_CONTEXT_TOKENS,
                )
                # Treated as "checked": no re-evaluation until the next
                # hourly slot (or real growth), not on every 5 s tick.
                self.needs_startup_compaction = False
                self.last_forced_compaction_at = now
                return

        self.needs_startup_compaction = False
        self.last_forced_compaction_at = now
        self.size_at_last_forced_compaction = self._current_session_file_size() or 0
        self.forced_compaction_result_pending = True
        log_event(
            "engine", "forced_compaction_triggered", tab_id=self.tab_id, reason=reason,
            session_size_bytes=self.size_at_last_forced_compaction,
            context_tokens_at_trigger=self.last_known_context_tokens,
            context_tokens_baseline=self.tokens_at_last_forced_compaction,
        )
        # Per explicit instruction (2026-09-18): this flag flipping is the
        # ONLY thing that makes compaction visible on the status bar (see
        # _compute_public_status) -- nothing else republishes status for
        # it, since compaction deliberately never touches turn_pending
        # (whose own setter is the usual auto-publish trigger).
        asyncio.create_task(self._publish_status())
        self._push_internal_command("/compact")

    def _note_compaction_hit_limit(self, limit_text: str) -> None:
        """A forced compaction just failed because the usage cap is in force.
        Per explicit instruction (2026-09-20), after a real outage: this used
        to be handled like a real turn hitting the cap -- the tab went
        "limited", and the error's own trailing ResultMessage flipped it back
        to "connected" within a millisecond, which is the exact edge that
        arms needs_post_limit_compaction_check (retry what the cap
        interrupted, no cooldown) -- so the next 5 s watchdog tick compacted
        again, failed again, and so on for as long as the cap lasted (1,464
        times in one hour), each attempt copying the whole transcript.
        Maintenance failing against a cap isn't a tab-level event at all: no
        state change, just a flat pause before the next attempt (see
        FORCED_COMPACTION_LIMIT_PAUSE_MS). The trailing ResultMessage is
        consumed by the normal forced-compaction path (forced_compaction_
        result_pending is still True) exactly as any other compaction
        result."""
        self.forced_compaction_blocked_until = time.monotonic() + FORCED_COMPACTION_LIMIT_PAUSE_MS / 1000
        log_event(
            "engine", "forced_compaction_hit_limit", tab_id=self.tab_id,
            pause_s=FORCED_COMPACTION_LIMIT_PAUSE_MS // 1000, message=limit_text[:200],
        )

    def _drain_compaction_queue(self) -> None:
        """Called right after forced_compaction_result_pending flips back
        to False (both places: the normal ResultMessage consumption path,
        and _apply_conn_state's own post-usage-cap-limit recovery edge
        case) -- replays every submit() call that arrived while compaction
        was still in flight, in the order they originally arrived, through
        the exact same submit() they'd have gone through immediately if
        compaction hadn't been running at all. See submit()'s own guard
        and compaction_queued_turns' __init__ comment for the incident
        this fixes."""
        if not self.compaction_queued_turns:
            return
        queued = self.compaction_queued_turns
        self.compaction_queued_turns = []
        log_event("engine", "compaction_queue_drained", tab_id=self.tab_id, count=len(queued))
        had_real_user_turn = any(item["is_real_user"] for item in queued)
        for item in queued:
            self.submit(item["text"], item["attachments"], item["is_real_user"], item["is_voice"], pending_text=item.get("pending_text"))
        if had_real_user_turn:
            # Per explicit instruction (2026-09-18), after a real incident:
            # a real user message got queued behind compaction and the
            # reply that followed falsely claimed the message itself "got
            # cut off" -- context had just been summarized right as it
            # arrived. recent_dialogue_history_instruction (policies.py,
            # always in the system prompt, unaffected by compaction -- it
            # summarizes conversation turns, not the system prompt) already
            # covers this in general, but an explicit, in-the-moment
            # reminder right here is more reliable than trusting the model
            # to recall a general standing rule at exactly the one moment
            # it matters most. Queued AFTER the real message(s) above, not
            # merged into their own text (last_real_user_question/
            # pending_user_text must stay the user's actual clean words).
            lang = current_language_name(self.tab_id)
            self.submit(
                "[Internal: a forced compaction just finished right as the user's message above arrived -- your "
                "context just got summarized, so some detail may not obviously still be there. Before asking the "
                "user to re-explain anything or claiming their message was incomplete/cut off, check your recent "
                "dialogue history file (the path is in your system prompt, refreshed independently of compaction) "
                f"first -- it still has the real recent back-and-forth. Then answer their actual message normally. "
                f"Reply in {lang}. Don't mention this note itself.]",
                [], False, False,
            )

    async def _check_hang(self) -> None:
        # Bug fix (2026-09-15), per explicit instruction: "Это должно быть
        # 90-секунд отсчитываемых, когда ничего не происходит: процессы не
        # потребляют процессор и не меняется загрузка памяти. Это не
        # таймаут выполнения, а таймаут от зависания." -- the real
        # liveness signal is whether the underlying claude.exe OS process
        # is doing anything (CPU/memory), not "did an SDK message arrive"
        # -- those aren't the same thing. Confirmed live via a direct
        # isolated test: a real /compact against a realistically-sized
        # session took 109.5s end to end with ZERO intermediate SDK
        # messages, while the process was genuinely working the whole
        # time -- the old signal could not tell that apart from an
        # actually frozen process. See process_activity.py's own module
        # docstring. Falls back to the old last-SDK-message clock only if
        # the monitor itself isn't usable (no pid captured yet, or psutil
        # couldn't read the process for some reason) -- "can't tell" must
        # never silently mean "assume hung", so the fallback also restores
        # the tool_in_flight extended leash from the previous version of
        # this fix, as a second safety net for exactly that degraded case.
        monitor = self._process_activity_monitor
        if monitor is not None and monitor.available:
            monitor.sample()
            elapsed = monitor.seconds_since_last_activity()
            signal_source = "process_activity"
            effective_timeout_s = (HANG_TIMEOUT_MS if self.has_seen_init else STARTUP_TIMEOUT_MS) / 1000
            tool_in_flight = False
        else:
            elapsed = time.monotonic() - self.last_activity
            signal_source = "last_sdk_message_fallback"
            tool_in_flight = (
                self.last_tool_use_started_at is not None
                and time.monotonic() - self.last_tool_use_started_at <= elapsed + 1
            )
            effective_timeout_s = (
                HANG_TIMEOUT_WITH_TOOL_IN_FLIGHT_MS if tool_in_flight
                else (HANG_TIMEOUT_MS if self.has_seen_init else STARTUP_TIMEOUT_MS)
            ) / 1000
        # Bug fix (2026-09-16), per explicit instruction: hard backstop on
        # top of the activity signal -- see HANG_ABSOLUTE_CEILING_MS's own
        # comment for the incident (a real turn stuck 15.5 minutes,
        # hang_count never left 0 because the process kept showing just
        # enough activity to look "alive" every tick). Checked against
        # turn_pending_since (when THIS turn actually started), not the
        # activity clock -- entirely independent axis.
        turn_elapsed_s = (time.monotonic() - self.turn_pending_since) if self.turn_pending_since is not None else 0.0
        past_absolute_ceiling = turn_elapsed_s >= HANG_ABSOLUTE_CEILING_MS / 1000
        log_event(
            "engine", "check_hang_tick", tab_id=self.tab_id, turn_pending=self.turn_pending,
            last_activity_s=round(elapsed, 1), hang_count=self.hang_count,
            hang_interrupted_at=self.hang_interrupted_at, has_seen_init=self.has_seen_init,
            effective_timeout_s=effective_timeout_s, signal_source=signal_source,
            tool_in_flight=tool_in_flight, tool_in_flight_name=self.last_tool_use_name if tool_in_flight else None,
            turn_elapsed_s=round(turn_elapsed_s, 1), past_absolute_ceiling=past_absolute_ceiling,
            # Bug fix (2026-09-16): raw numbers behind the activity
            # boolean, not just the final verdict -- see ProcessActivity
            # Monitor's own comment for the incident this closes (no way
            # to tell from the log alone WHY a process kept looking
            # "active").
            monitor_cpu_percent=monitor.last_cpu_percent if monitor is not None else None,
            monitor_rss_delta=monitor.last_rss_delta if monitor is not None else None,
            monitor_io_delta=monitor.last_io_delta if monitor is not None else None,
        )
        if not self.turn_pending and self.has_seen_init:
            self.hang_interrupted_at = None
            return

        # --- Phase 2: already soft-interrupted, waiting on escalation ------
        # Bug fix (2026-09-15), confirmed live (tab 4, 2026-09-15: real
        # stream_ended_unexpectedly elapsed_ms up to 351453 -- 3-4x the
        # intended ~110s = 90s detect + 20s grace): this branch used to be
        # reachable ONLY when `elapsed` (the SAME clock phase 1 below
        # uses) was STILL past effective_timeout_s -- so the CLI's own
        # courtesy "aborted-turn" ResultMessage (which always arrives
        # moments after a soft interrupt, and always used to reset
        # last_activity) silently swallowed the whole escalation: elapsed
        # dropped back near zero, this function returned early every tick
        # from the check below, and the REAL 20s grace period never
        # actually got examined until ANOTHER full 90s of silence had
        # passed on top. Once a hang is already being escalated, only ITS
        # OWN clock (hang_interrupted_at) should gate it -- checked
        # unconditionally here, completely independent of whatever the
        # liveness signal above says in the meantime.
        if self.hang_interrupted_at is not None:
            if time.monotonic() - self.hang_interrupted_at < HANG_ESCALATION_GRACE_MS / 1000:
                return
            log_event("engine", "hang_escalation_force_close", tab_id=self.tab_id)
            self.hang_interrupted_at = None
            try:
                if self.client:
                    await self.client.disconnect()
            except Exception as exc:
                log_event("engine", "hang_escalation_close_failed", tab_id=self.tab_id, error=str(exc))
            return

        # --- Phase 1: not yet interrupted -- is THIS tick a fresh hang? ----
        # past_absolute_ceiling (computed above) can force this even while
        # the activity signal alone says "still alive" -- see
        # HANG_ABSOLUTE_CEILING_MS's own comment.
        if elapsed < effective_timeout_s and not past_absolute_ceiling:
            return
        if past_absolute_ceiling and elapsed < effective_timeout_s:
            log_event(
                "engine", "hang_absolute_ceiling_override", tab_id=self.tab_id,
                turn_elapsed_s=round(turn_elapsed_s, 1), signal_source=signal_source,
            )

        self.hang_count += 1
        # Bug fix (2026-09-10): capture what was actually running, before
        # interrupting it, so _handle_failure's replay nudge can tell the
        # model specifically what got force-terminated (per explicit
        # instruction) rather than a generic note -- lets it try something
        # else instead of blindly repeating the same slow/stuck call. Only
        # meaningful in the last_sdk_message_fallback path (tool_in_flight
        # is always False when the process-activity monitor is the one
        # driving this, since real work reaching the full 90s of genuine
        # process inactivity would itself now be a real hang either way).
        if tool_in_flight:
            self.hang_interrupted_tool_name = self.last_tool_use_name
            self.hang_interrupted_tool_elapsed_s = time.monotonic() - self.last_tool_use_started_at
        else:
            self.hang_interrupted_tool_name = None
            self.hang_interrupted_tool_elapsed_s = None
        if self.hang_count >= 2:
            log_event("engine", "hang_repeat_force_close", tab_id=self.tab_id, hang_count=self.hang_count)
            self.hang_interrupt_result_pending = True
            try:
                if self.client:
                    await self.client.disconnect()
            except Exception as exc:
                log_event("engine", "hang_force_close_failed", tab_id=self.tab_id, error=str(exc))
            return
        log_event(
            "engine", "hang_detected_soft_interrupt", tab_id=self.tab_id,
            tool_name=self.hang_interrupted_tool_name, tool_elapsed_s=self.hang_interrupted_tool_elapsed_s,
        )
        self.hang_interrupted_at = time.monotonic()
        # Bug fix (2026-09-10): confirmed live -- interrupt() is a soft
        # ask, not a hard kill; the CLI still sends a final ResultMessage
        # for the turn it just aborted. Without this flag, that
        # ResultMessage was treated exactly like a real completion --
        # wiping turn_pending/pending_user_text AND resetting silent_turn=
        # True -- so even when _handle_failure's replay genuinely
        # succeeded a few seconds later, its real answer was silently
        # swallowed (silent_turn never got reset back to False, since the
        # replay goes through _push_message directly, not submit()). See
        # the ResultMessage handler below for the other half of this fix.
        self.hang_interrupt_result_pending = True
        try:
            if self.client:
                await self.client.interrupt()
        except Exception as exc:
            log_event("engine", "hang_soft_interrupt_failed", tab_id=self.tab_id, error=str(exc))
        # Same reasoning as stop()'s own fix -- a hang is plausibly caused
        # by exactly a detached background operation that never completes/
        # never gets polled again, so cancel this tab's in-flight
        # operations here too, not just on an explicit user Stop.
        cancelled = REGISTRY.cancel_for_tab(self.tab_id)
        if cancelled:
            log_event("engine", "hang_soft_interrupt_cancelled_operations", tab_id=self.tab_id, count=cancelled)

    # ------------------------------------------------------------- failure --

    async def _handle_failure(self, exc: BaseException, extra_note: str | None = None) -> None:
        # See _format_exception_chain's own docstring for the incident this
        # fixes -- error_chain carries whatever REAL underlying cause a
        # wrapped SDK exception (CLINotFoundError et al.) would otherwise
        # hide; error is kept too, unchanged, for anything already
        # filtering/searching logs on that exact field.
        log_event(
            "engine", "handle_failure_entered", tab_id=self.tab_id, hang_count=self.hang_count,
            turn_pending=self.turn_pending, error=str(exc), error_chain=_format_exception_chain(exc),
        )
        # See restart_engine_switch's own __init__ comment. Consumed once,
        # right here, since switch_engine_if_needed()'s force_restart() ends
        # the stream the exact same way any other disconnect does -- this is
        # the one place that stream-end always lands, regardless of cause.
        engine_switch = self.restart_engine_switch
        self.restart_engine_switch = None
        # Any pending per-server reconnect retries belong to the client
        # instance that's being torn down -- _schedule_mcp_reconnect's own
        # self.client-identity check would catch this anyway, but clearing
        # here avoids leaking timers.
        self._clear_mcp_reconnect_timers()
        now = time.monotonic()
        self.restart_timestamps = [t for t in self.restart_timestamps if now - t < RESTART_WINDOW_MS / 1000]
        self.restart_timestamps.append(now)
        log_event("engine", "restart_budget", tab_id=self.tab_id, count=len(self.restart_timestamps), max=MAX_RESTARTS_PER_WINDOW)
        if len(self.restart_timestamps) > MAX_RESTARTS_PER_WINDOW:
            log_event("engine", "restart_budget_exceeded", tab_id=self.tab_id, count=len(self.restart_timestamps))
            self._set_conn_state(
                "restart_backoff",
                f"Trouble reconnecting (failed {len(self.restart_timestamps)} times in {RESTART_WINDOW_MS // 60_000}min) "
                f"-- retrying in {RESTART_BACKOFF_MS // 1000}s",
            )
            await asyncio.sleep(RESTART_BACKOFF_MS / 1000)
        self._set_conn_state("restarting", str(exc))
        # Per explicit instruction (2026-09-10, revised 2026-09-15): a
        # genuinely slow-but-alive tool call (a recursive grep/filesystem
        # scan, a slow browser evaluate, a large fetch/decode) gets a much
        # longer leash now (see _check_hang's own tool_in_flight/
        # HANG_TIMEOUT_WITH_TOOL_IN_FLIGHT_MS) rather than none at all --
        # this note only fires once that longer timeout is ALSO exceeded
        # (real work truly stuck), or when there was no tool in flight to
        # begin with (genuine dead-air silence past the plain 90s/300s).
        # Either way, tell the model exactly what got force-terminated so
        # it can try a different approach on retry instead of blindly
        # repeating the same slow/stuck call.
        if engine_switch:
            # Confirmed live (2026-09-22): the generic "internal failure,
            # infrastructure self-healing" framing below is actively
            # misleading here, and gives the newly active engine no hint
            # that it just inherited a conversation from a DIFFERENT engine
            # with none of its own memory of it -- it resumes its OWN prior
            # session (if any) via native --resume, which has a hole for
            # exactly however long the other engine was active; the only
            # bridge across that hole is the cross-engine dialogue-history
            # file (recent_dialogue_history_instruction, already in this
            # tab's system prompt). Confirmed live the model, told only
            # "you recovered from a failure, carry on", read that file but
            # then treated its narrated summary as settled history rather
            # than something to actively verify/continue -- replying
            # [[NO_UPDATE]] to a real, unresolved "continue" from the user.
            from_engine, to_engine = engine_switch
            watchdog_note = (
                f"[System note: this tab just switched engines, from {from_engine} to {to_engine}, per the "
                "user's own explicit request -- a deliberate handoff, not a failure; don't mention any 'error' "
                "or 'recovery' about it unless the user specifically asks what happened. You do NOT have this "
                f"tab's own memory of whatever happened while {from_engine} was active -- that work only exists "
                "in the cross-engine dialogue-history file this system prompt already points you at (see the "
                "instruction about it earlier in this prompt). If you haven't already, read that file FIRST, "
                "before answering anything below it -- and don't just treat what it describes as settled "
                "history: if it shows a task that isn't actually finished, or the user's own message below is "
                "asking you to continue something, actually continue it for real, the same as if you'd been "
                "working on it yourself the whole time.]"
            )
        else:
            # Per explicit instruction (2026-09-10, revised 2026-09-15): a
            # genuinely slow-but-alive tool call (a recursive grep/filesystem
            # scan, a slow browser evaluate, a large fetch/decode) gets a much
            # longer leash now (see _check_hang's own tool_in_flight/
            # HANG_TIMEOUT_WITH_TOOL_IN_FLIGHT_MS) rather than none at all --
            # this note only fires once that longer timeout is ALSO exceeded
            # (real work truly stuck), or when there was no tool in flight to
            # begin with (genuine dead-air silence past the plain 90s/300s).
            # Either way, tell the model exactly what got force-terminated so
            # it can try a different approach on retry instead of blindly
            # repeating the same slow/stuck call.
            tool_note = ""
            if self.hang_interrupted_tool_name:
                tool_note = (
                    f" The tool call in progress ('{self.hang_interrupted_tool_name}') had not finished after "
                    f"{round(self.hang_interrupted_tool_elapsed_s or 0)}s and was force-terminated -- if this is still "
                    "relevant, try a different approach instead of just repeating that same call, since whatever made "
                    "it slow or stuck likely hasn't changed."
                )
            self.hang_interrupted_tool_name = None
            self.hang_interrupted_tool_elapsed_s = None
            watchdog_note = (
                f"[System note: this session just recovered from an internal failure (hangCount={self.hang_count}):"
                f"{tool_note} {exc}.{(' ' + extra_note) if extra_note else ''} This is Caroline's own infrastructure "
                "self-healing, already handled -- for your own situational awareness only. Do not mention this or "
                "sound any alarm about it to the user unless they specifically ask what happened just now.]"
            )
        if self.pending_user_text is not None:
            log_event("engine", "handle_failure_replay_pending", tab_id=self.tab_id, text_len=len(self.pending_user_text))
            self._push_message(f"{watchdog_note}\n\n{self.pending_user_text}", self.pending_attachments, False)
        else:
            # current_language_name() is synchronous/instant (2026-09-09
            # redesign) -- this pending_user_text re-check is kept regardless
            # as cheap insurance against a real message queued by some other
            # earlier await in this same handler.
            lang = current_language_name(self.tab_id)
            if self.pending_user_text is not None:
                log_event("engine", "handle_failure_real_message_arrived_before_nudge", tab_id=self.tab_id)
                self.inject_proactive(watchdog_note)
                # Bug fix (2026-09-15): same root cause and same fix as
                # main.py's resuming_unfinished_turn -- inject_proactive()
                # just above always sets pending_is_real_user=False, but
                # pending_user_text is_not_none right here means this IS a
                # real, still-unanswered user question, just recovering
                # via internal machinery rather than a live submit(). See
                # main.py's own call site for the full incident writeup.
                self.pending_is_real_user = True
            else:
                log_event("engine", "handle_failure_continue_or_silent_nudge", tab_id=self.tab_id, lang=lang)
                refresh_language_in_background(self.last_saved_session_id, self.tab_id)
                self.inject_proactive(f"{watchdog_note}\n\n{CONTINUE_OR_SILENT_NUDGE_TEMPLATE.format(language=lang)}")
        log_event("engine", "handle_failure_done", tab_id=self.tab_id)

    # -------------------------------------------------------------- status --

    def status(self) -> dict[str, Any]:
        return {
            "tabId": self.tab_id,
            "ended": self.ended,
            "turnPending": self.turn_pending,
            "hasSeenInit": self.has_seen_init,
            "lastActivityMs": round((time.monotonic() - self.last_activity) * 1000),
            "lastUserActivityMs": round((time.monotonic() - self.last_user_activity) * 1000),
            "hangCount": self.hang_count,
            "connState": self.conn_state,
            # Per explicit instruction (2026-09-13): App.xaml.cs's startup
            # splash must not dismiss (and the main window must not even be
            # shown) while any tab's forced startup compaction is still
            # running -- see WaitForSplashDismissAsync's own doc comment for
            # why _check_forced_compaction's own "startup" trigger otherwise
            # raced the splash-dismiss/SyncTabListToBackend timing and
            # produced a window with a missing tab list.
            "forcedCompactionPending": self.forced_compaction_result_pending,
        }

    # ------------------------------------------------------------- run loop --

    async def _run_loop(self) -> None:
        set_send(lambda message: self.send(message))
        set_tab_id(self.tab_id)
        set_inject_proactive(lambda text: self.inject_proactive(text))
        set_cli_pid_sink(self._on_cli_process_spawned)
        while not self.ended:
            try:
                self.hang_count = 0
                self.has_seen_init = False
                # Bug fix (2026-09-10): confirmed live -- last_activity is only
                # ever refreshed by submit() (a real user message) or a message
                # actually arriving on the wire. Once a session starts failing
                # and replaying its pending turn via _push_message (NOT
                # submit(), see handle_failure), last_activity goes stale and
                # NEVER updates again while nothing streams in. Every fresh
                # query() this loop creates then inherits that ancient
                # timestamp -- _check_hang sees an "elapsed" of many minutes
                # (confirmed live: 3246s and climbing) against the 300s
                # startup timeout, so it judges the brand-new query "hung" on
                # its very first watchdog tick and force-closes it around 22s
                # in, long before even a legitimately slow resume (tab 1 has
                # needed 128-280s for a real one) can ever finish -- a
                # self-perpetuating trap this tab could never escape. A fresh
                # attempt deserves its own fresh clock.
                self.last_activity = time.monotonic()
                # Bug fix (2026-09-10): confirmed live -- progress narration
                # (_check_progress_narration) fired 5s after a fresh restart
                # (mid tool-use-concurrency recovery), because
                # last_visible_output_at was still whatever stale value it
                # had from BEFORE the whole restart cascade -- the 60s clock
                # needs to restart from a genuine restart too, not just a
                # real user submit() or an actual assistant reply, or it can
                # fire almost immediately with stale/pre-restart context.
                self.last_visible_output_at = time.monotonic()
                log_event("engine", "run_loop_fresh_session", tab_id=self.tab_id)

                self.engine_kind = (
                    "openai" if load_chat_mode(self.workspace_dir, self.tab_id) == "openai" and openai_available(self.workspace_dir)
                    else "claude"
                )
                mode = await resolve_mode(self.workspace_dir, self.tab_id)
                self.current_chat_source = "openai" if self.engine_kind == "openai" else mode.chat_source
                if mode.chat_source == "none" and self.engine_kind == "claude":
                    # query() is about to fail on its very first real request no
                    # matter what -- there's no chat source to even try. Checked
                    # here, before query() creation, so opening the login window
                    # is native/deterministic instead of depending on the model
                    # being able to run a tool call it has no chat source to run
                    # WITH (see sw_gate.py's own docstring).
                    gate = await require_sw_or_prompt(self.send, True)
                    log_event("engine", "no_chat_source", tab_id=self.tab_id, gate_ok=gate.ok, gate_message=gate.message)

                anthropic_env: dict[str, str] | None = None
                try:
                    anthropic_env = await build_options_env(self.workspace_dir, mode)
                except Exception as exc:
                    log_event("engine", "build_options_env_failed", tab_id=self.tab_id, error=str(exc))

                resume_session_id = self._resolve_resume_session_id()
                if resume_session_id:
                    self.last_saved_session_id = resume_session_id
                if resume_session_id and self.engine_kind == "claude":
                    # No CLI process has this transcript open right now (a
                    # fresh query() is about to be built), so it is safe to
                    # move a huge already-compacted prefix out of the file
                    # the CLI has to read on every resume.
                    await asyncio.to_thread(
                        rotate_transcript,
                        claude_project_dir(self.workspace_dir) / f"{resume_session_id}.jsonl",
                        self.workspace_dir,
                    )
                    # Bug fix (2026-09-18), confirmed live: _recent_24h_
                    # dialogue_file_path used to only ever get (re)written
                    # inside submit()'s real-user branch -- meaning the
                    # VERY FIRST turn of a freshly (re)built session (the
                    # resumed-unfinished-turn injection included, which is
                    # is_real_user=False and so never reaches that branch
                    # at all) had recent_dialogue_history_instruction
                    # return "" -- no safety net whatsoever right when a
                    # restart makes Caroline most likely to have lost track
                    # of what she was doing. Confirmed live: exactly this
                    # produced a real incident where a resumed task's own
                    # details had to be re-explained by the user because
                    # nothing pointed her at where to look. Populate it
                    # here too, proactively, on every fresh session build --
                    # not conditional on a real submit() having happened
                    # yet this process lifetime.
                    #
                    # Bug fix (2026-09-20), confirmed live: that eager
                    # write used to be SYNCHRONOUS here, on the event loop,
                    # reading each tab's WHOLE transcript -- with four tabs
                    # starting at once (two of them 147/368 MB) the backend
                    # stopped answering /api/status for minutes and its
                    # watchdog killed it, repeatedly. Now only the (stable)
                    # PATH is set below, instantly, and the content is
                    # built by a single-flight worker-thread refresh
                    # reading just the last 24 hours (see
                    # _schedule_recent_24h_dialogue_refresh) -- moved OUT
                    # of this `if` too: right after an unrecoverable-
                    # session reset there is no resume id but there IS a
                    # continuity archive, exactly when this file matters.
                self._recent_24h_dialogue_file_path = str(_recent_24h_dialogue_path(self.workspace_dir, self.tab_id))
                self._schedule_recent_24h_dialogue_refresh()

                mcp_servers = build_mcp_servers()
                # Dynamic backstop (2026-09-22), zero hardcoded names or
                # keywords -- see compute_foreign_tool_overlap's own
                # docstring. What THIS turn disallows is exactly what the
                # PREVIOUS init message actually observed on the wire
                # (refreshed below once this connection's own init
                # arrives); empty on this process's very first-ever query,
                # self-healing one query later and from every query after
                # that, including across a full restart (cached to disk).
                dynamic_disallowed_tools = load_discovered_foreign_tool_overlap(self.workspace_dir)
                self._system_prompt_language = current_language_name(self.tab_id)
                system_prompt_parts = [
                    persona_system_prompt_append(get_persona(self.workspace_dir)),
                    *[fn() for fn in ALWAYS_ON_INSTRUCTIONS],
                    continuity_pointer_instruction(load_tab_continuity_archive(self.workspace_dir, self.tab_id)),
                    recent_dialogue_history_instruction(self._recent_24h_dialogue_file_path),
                    running_agents_pointer_instruction(agent_registry.ensure_status_file(self.workspace_dir, self.tab_id)),
                    language_hint_instruction(self._system_prompt_language),
                    OPENAI_TOOL_HONESTY_INSTRUCTION if self.engine_kind == "openai" else None,
                ]
                system_prompt_append = "\n\n".join(p for p in system_prompt_parts if p)

                def _stderr_handler(data: str, _resume_session_id: str | None = resume_session_id) -> None:
                    log_event("engine", "claude_stderr", tab_id=self.tab_id, resume=_resume_session_id, data=data.strip()[:500])
                    if SESSION_NOT_FOUND_PATTERN.search(data) and not self.restart_for_unrecoverable_session:
                        log_event("engine", "session_not_found_on_disk", tab_id=self.tab_id, resume=_resume_session_id)
                        self._reset_unrecoverable_session(self.pending_user_text, self.pending_attachments)

                options_kwargs: dict[str, Any] = {
                    "cwd": self.workspace_dir,
                    "permission_mode": "bypassPermissions",
                    "mcp_servers": mcp_servers,
                    "disallowed_tools": ["mcp__caroline-notes__notes_login", *dynamic_disallowed_tools],
                    # Only Caroline's OWN in-process MCP servers -- ignore every other MCP configuration (user/
                    # project scope in ~/.claude.json, claude.ai connectors). Confirmed live (2026-09-24): those
                    # belong to whoever's dev setup shares that file, come and go with their connection state,
                    # and agents reached for them (an external email MCP) instead of Caroline's own tools. A
                    # structural fix with no names or lists in it; applies to subagents too. Verified with a real
                    # probe that the Claude login is unaffected.
                    "extra_args": {"strict-mcp-config": None},
                    # Caroline's own definition of the default agent -- carries her rules, which a subagent does
                    # NOT inherit from --append-system-prompt. See agent_definitions.py.
                    "agents": caroline_agents(),
                    "stderr": _stderr_handler,
                    # Claude's own native auto-compaction handles context
                    # ageing now -- explicitly on, and one long-lived client
                    # across turns (no per-turn transcript rewrite/restart).
                    # settings wants a FILE PATH, not raw JSON -- see
                    # _ensure_settings_file's own docstring for the bug this
                    # fixes.
                    "settings": _ensure_settings_file(self.workspace_dir),
                    "hooks": {"PreCompact": [HookMatcher(hooks=[self._pre_compact_hook])]},
                    # Bug fix (2026-09-13), confirmed live (tab 2, "Flying
                    # Squirrel", twice -- 2026-09-11 and again 2026-09-13):
                    # the SDK's own subprocess transport frames the CLI's
                    # NDJSON stdout one line at a time and hard-fails
                    # (SDKJSONDecodeError, "JSON message exceeded maximum
                    # buffer size") past its default 1MB-per-line limit -- a
                    # single large tool result (base64 image content, here)
                    # is enough to cross it. That failure is NOT recoverable
                    # by this class's own restart/replay machinery once it
                    # happens: the oversized line is already persisted in
                    # the resumed session's own transcript, so the very next
                    # `resume` reads it right back off disk and dies again
                    # immediately -- confirmed live as a real, indefinite
                    # "query() stream ended unexpectedly" retry loop (over
                    # an hour, every ~5min, restart budget never even
                    # tripped because each cycle "succeeded" at restarting
                    # only to fail the same way seconds later). Raised well
                    # past any plausible single-message size this app
                    # produces (a handful of embedded images, at most) --
                    # None keeps inheriting the SDK's own default forever if
                    # this line is ever removed, so pin an explicit value.
                    "max_buffer_size": 50 * 1024 * 1024,
                }
                if anthropic_env:
                    options_kwargs["env"] = anthropic_env
                if resume_session_id:
                    options_kwargs["resume"] = resume_session_id
                claude_model = get_model_override(self.workspace_dir, "claude")
                if claude_model:
                    options_kwargs["model"] = claude_model
                if system_prompt_append:
                    options_kwargs["system_prompt"] = {"type": "preset", "preset": "claude_code", "append": system_prompt_append}
                query_started_at = time.monotonic()
                log_event("engine", "query_creating", tab_id=self.tab_id, resume=resume_session_id, chat_source=self.current_chat_source, engine=self.engine_kind)
                if self.engine_kind == "openai":
                    self.client = CodexEngine(build_codex_options(self.workspace_dir, system_prompt_append, mcp_servers, resume_session_id))
                else:
                    options = ClaudeAgentOptions(**options_kwargs)
                    self.client = ClaudeEngine(options)
                self._cli_process_pid = None
                self._process_activity_monitor = None
                # A fresh CLI process: every agent the previous one had running died with it, silently.
                lost_agents = agent_registry.mark_running_as_lost(self.workspace_dir, self.tab_id)
                await self.client.connect(self._input_stream())
                if lost_agents:
                    self._tell_model_agents_were_lost(lost_agents)

                async for raw_message in self.client.receive_messages():
                    self.last_activity = time.monotonic()
                    message: Any = raw_message

                    # Bug fix (2026-09-10): tracks whatever tool call is
                    # currently in flight so, if a hang fires while one is
                    # running, _check_hang/_handle_failure can tell the
                    # model specifically what got force-interrupted (per
                    # explicit instruction) instead of a generic "something
                    # failed" note. Never explicitly cleared on completion --
                    # if the tool finished, a NEW message would have arrived
                    # and reset last_activity anyway, so a hang could only
                    # ever fire while this really is the one still running.
                    if isinstance(message, AssistantMessage):
                        tool_use_block = next((b for b in message.content if isinstance(b, ToolUseBlock)), None)
                        if tool_use_block:
                            self.last_tool_use_name = tool_use_block.name
                            self.last_tool_use_started_at = time.monotonic()

                    # Ground-truth tool-outcome tracking -- see
                    # _track_tool_outcomes' own docstring.
                    self._track_tool_outcomes(message)
                    self._track_background_tasks(message)

                    # --- classifier refusal ---
                    if isinstance(message, AssistantMessage):
                        refusal_text = next(
                            (b.text for b in message.content if isinstance(b, TextBlock) and CLASSIFIER_REFUSAL_PATTERN.search(b.text)),
                            None,
                        )
                        if refusal_text:
                            category = extract_classifier_refusal_category(refusal_text)
                            if self.classifier_refusal_retry_count == 0:
                                self.classifier_refusal_retry_count += 1
                                log_event("engine", "classifier_refusal_retry", tab_id=self.tab_id, category=category)
                                self._push_message(
                                    "[System note: your previous reply was blocked by an Anthropic API "
                                    "content-classifier false positive (unrelated to the actual conversation) and "
                                    "never reached the user. Please just try answering their last message again.]",
                                    [], False,
                                )
                                continue
                            log_event("engine", "classifier_refusal_recurred", tab_id=self.tab_id, category=category)
                            explanation = (
                                "Не смогла ответить на предыдущее сообщение: сработал внутренний фильтр безопасности "
                                "Anthropic" + (f" (категория «{category}»)" if category else "") +
                                ", похоже на ложное срабатывание — с содержанием разговора это не связано. Повторная "
                                "попытка тоже не прошла. Попробуйте переформулировать сообщение или повторить чуть "
                                "позже."
                            )
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    block.text = explanation
                            self.classifier_refusal_retry_count = 0

                    # --- billing_error (structured field) ---
                    if isinstance(message, AssistantMessage) and message.error == "billing_error":
                        log_event("engine", "billing_error", tab_id=self.tab_id, chat_source=mode.chat_source)
                        explanation = self._handle_balance_exhausted()
                        self._set_conn_state("billing_blocked", explanation, arm_ignore_next_result=True)
                        self._schedule_api_retry("billing_error", self.turn_is_voice)
                        continue

                    # --- PROMPT_TOO_LONG / TOOL_CONCURRENCY / NOT_LOGGED_IN ---
                    if isinstance(message, AssistantMessage):
                        text_blocks = [b.text for b in message.content if isinstance(b, TextBlock)]
                        prompt_too_long = next((t for t in text_blocks if PROMPT_TOO_LONG_PATTERN.search(t)), None)
                        if prompt_too_long:
                            # Native auto-compaction should keep the context
                            # under the limit; if this still fires, the
                            # session is somehow past saving -- abandon it and
                            # start fresh (the pre-reset transcript is archived
                            # for Read, same as any unrecoverable reset).
                            log_event("engine", "prompt_too_long", tab_id=self.tab_id, text=prompt_too_long[:200])
                            self._set_conn_state("restarting", "Recovering (context overflow)...")
                            self._reset_unrecoverable_session(self.pending_user_text, self.pending_attachments)
                            continue
                        tool_concurrency = next((t for t in text_blocks if TOOL_CONCURRENCY_ERROR_PATTERN.search(t)), None)
                        if tool_concurrency:
                            log_event("engine", "tool_concurrency_error", tab_id=self.tab_id, text=tool_concurrency[:200])
                            self._set_conn_state("restarting", "Recovering (unrecoverable session)...")
                            self._reset_unrecoverable_session(self.pending_user_text, self.pending_attachments)
                            continue
                        not_logged_in = next((t for t in text_blocks if NOT_LOGGED_IN_PATTERN.search(t)), None)
                        if not_logged_in:
                            log_event("engine", "not_logged_in_suppressed", tab_id=self.tab_id)
                            # Bug fix (2026-09-12): unlike its two siblings just above
                            # (prompt_too_long, tool_concurrency), this branch used to just
                            # log and swallow the turn -- carolineStatus was left at
                            # whatever it was before (usually "ready"), so the lamp stayed
                            # green and the user got zero feedback that nothing will ever
                            # come back until they sign in. Genuinely distinct from
                            # billing_blocked/limited (a logged-in user out of quota, which
                            # DOES self-heal via _schedule_api_retry) -- this is "no chat
                            # source configured at all" (see subscription_mode.resolve_mode),
                            # which no amount of retrying fixes, so it maps to the same
                            # "error" public state as billing_blocked, not "recovering".
                            self._set_conn_state(
                                "not_logged_in",
                                "You don't have an active Claude Code login or a SquirrelWisdom account "
                                "signed in -- please sign in to one of them to continue.",
                            )
                            continue

                    # --- typed engine error: never a chat bubble ---
                    if isinstance(message, AssistantMessage) and message.error in ENGINE_ERROR_SUPPRESSED_KINDS:
                        raw_error_text = " ".join(b.text for b in message.content if isinstance(b, TextBlock))
                        log_event("engine", "engine_error_suppressed", tab_id=self.tab_id, error_kind=message.error, text=raw_error_text[:500])
                        if message.error == "authentication_failed":
                            self._set_conn_state("auth_failed", "The AI provider rejected the credentials -- check the login/API key in Settings.")
                        else:
                            self._set_conn_state("engine_error", "The AI provider returned an error. Try again in a moment.")
                        # The trailing ResultMessage is a REAL completion (clears turn_pending) but must
                        # not flip this status straight back to "connected" -- same flag the usage-cap
                        # branch below uses. The flat retry stays ON for every kind, auth included,
                        # per explicit instruction: credentials/tokens can be fixed at any moment (a
                        # refreshed login, a new key) and the session must pick that up by itself.
                        # What changed is only that the failure is silent in the chat, and that the
                        # retry is cancelled on an engine/mode switch (switch_engine_if_needed).
                        self.suppress_next_conn_state_reset = True
                        self._schedule_api_retry(f"engine_error:{message.error}", self.turn_is_voice)
                        continue

                    # --- CC CLI usage-cap message ---
                    if isinstance(message, AssistantMessage):
                        text_blocks = [b.text for b in message.content if isinstance(b, TextBlock)]
                        limit_text = next((t for t in text_blocks if CC_CLI_LIMIT_PATTERN.search(t)), None)
                        if limit_text:
                            log_event("engine", "cc_cli_limit_message", tab_id=self.tab_id)
                            # The limit message is OUR forced compaction's own
                            # failure ("Error during compaction: You've hit your
                            # session limit"), not a real turn hitting the cap --
                            # maintenance, so it must not flip the whole tab
                            # limited/connected. See _note_compaction_hit_limit.
                            if self.forced_compaction_result_pending:
                                self._note_compaction_hit_limit(limit_text)
                                continue
                            # Bug fix (2026-09-11), per explicit instruction: this is
                            # NOT the same shape as a rate-limit REJECTION (no
                            # arm_ignore_next_result here, deliberately) -- a real
                            # AssistantMessage came through, meaning the turn
                            # genuinely completed; the model just concluded it by
                            # reporting a usage cap. The trailing ResultMessage is
                            # therefore a REAL completion, not a fake one -- let it
                            # clear turn_pending normally instead of pretending the
                            # turn never happened. _schedule_api_retry (which retries
                            # every 90s FOREVER, correct only for "the operation never
                            # even started" -- a true rejection, no balance) does not
                            # apply here; see _schedule_one_shot_followup_check's own
                            # doc comment for the distinction.
                            self._set_conn_state("limited", limit_text)
                            self.suppress_next_conn_state_reset = True
                            self._schedule_one_shot_followup_check("cc_cli_limit_message", self.turn_is_voice)
                            continue

                    # --- structured rate-limit event ---
                    if isinstance(message, RateLimitEvent):
                        info = message.rate_limit_info
                        info_dict = {"status": info.status, "resets_at": info.resets_at, "rate_limit_type": info.rate_limit_type}
                        self.last_rate_limit_info = info_dict
                        if info.status == "rejected":
                            self.ignore_next_result_recovery = True
                            self._handle_rate_limit_rejected("in-stream", info_dict)
                        continue

                    # --- system/status (compacting progress), only logged while
                    # WE triggered it -- see FORCED_COMPACTION_HOURLY_MS. Not a
                    # `continue` -- the wire-send suppression a few lines down
                    # (result_is_fake/forced_compaction_result_pending) already
                    # covers not showing this to the client; this is purely a
                    # side-effect log line, everything else about this message
                    # still flows through the loop normally.
                    if isinstance(message, SystemMessage) and message.subtype == "status" and self.forced_compaction_result_pending:
                        log_event(
                            "engine", "forced_compaction_status", tab_id=self.tab_id,
                            status=message.data.get("status"), compact_result=message.data.get("compact_result"),
                            compact_error=message.data.get("compact_error"),
                        )

                    # --- system/compact_boundary (2026-09-15) -- fires for
                    # EVERY compaction, ours (forced) or the CLI's own
                    # native auto-compaction alike, carrying the authoritative
                    # post-compaction token count in compact_metadata.
                    # post_tokens -- confirmed live via a direct isolated
                    # test (a real /compact against a copy of a real bloated
                    # session: pre_tokens=69422, post_tokens=10158). This is
                    # the new growth-trigger baseline (see
                    # tokens_at_last_forced_compaction's own __init__
                    # comment for why file bytes were the wrong signal).
                    if isinstance(message, SystemMessage) and message.subtype == "compact_boundary":
                        post_tokens = (message.data.get("compact_metadata") or {}).get("post_tokens")
                        log_event(
                            "engine", "compact_boundary_observed", tab_id=self.tab_id,
                            pre_tokens=(message.data.get("compact_metadata") or {}).get("pre_tokens"),
                            post_tokens=post_tokens, trigger=(message.data.get("compact_metadata") or {}).get("trigger"),
                        )
                        if isinstance(post_tokens, int):
                            self.tokens_at_last_forced_compaction = post_tokens
                            # A boundary is itself the freshest possible
                            # reading of "what's in context right now" --
                            # keep the two baselines in sync so the growth
                            # branch doesn't compare a stale pre-compaction
                            # last_known_context_tokens against the brand
                            # new baseline on its very next check.
                            self.last_known_context_tokens = post_tokens
                        # Compaction can summarize away who was launched -- say so again, from ground truth.
                        still_running = agent_registry.running(self.workspace_dir, self.tab_id)
                        if still_running:
                            listing = "; ".join(f"agentId {i}: {e.get('description') or '?'}" for i, e in still_running.items())
                            self.inject_proactive(
                                "[Your context was just compacted. Agents/background tasks you launched that are STILL "
                                f"RUNNING and will report back on their own: {listing}. Do not re-launch them and do not "
                                "assume they are done; the full, current list is in the file named in your instructions. "
                                "If this changes nothing for the user, reply exactly [[NO_UPDATE]].]"
                            )

                    # --- system/api_retry ---
                    if isinstance(message, SystemMessage) and message.subtype == "api_retry":
                        self.last_api_retry_error = message.data.get("error")
                        log_event("engine", "api_retry_system_message", tab_id=self.tab_id, error=self.last_api_retry_error)
                        # Real incident (2026-09-09): an "authentication_failed" api_retry
                        # is just logged-and-waited here, with NOTHING that ever tears the
                        # connection down -- the CLI keeps retrying the exact same doomed
                        # request against the exact same (possibly stale) session/env
                        # forever. After a bounded number of consecutive auth failures on
                        # this connection, force a clean restart -- the next _run_loop
                        # iteration re-resolves the mode and rebuilds Options.env from
                        # scratch. Own choice of threshold (3), not a specified value.
                        if self.last_api_retry_error == "authentication_failed":
                            self.consecutive_auth_retry_failures += 1
                            log_event("engine", "auth_retry_failure_streak", tab_id=self.tab_id, count=self.consecutive_auth_retry_failures)
                            if self.consecutive_auth_retry_failures >= AUTH_RETRY_ESCALATION_THRESHOLD:
                                log_event("engine", "auth_retry_escalation_force_restart", tab_id=self.tab_id, chat_source=mode.chat_source)
                                self.consecutive_auth_retry_failures = 0
                                self.restart_pending = True
                                if self.client:
                                    await self._safe_disconnect(self.client)
                        continue

                    # --- session id capture ---
                    # Bug fix (2026-09-10): confirmed live -- tab 1 was stuck
                    # in a ~13s tool-use-concurrency restart loop, reusing the
                    # SAME condemned session id every time. Root cause: once
                    # _reset_unrecoverable_session() clears the tab's session
                    # id (self.last_saved_session_id = None) and schedules the
                    # client's disconnect in the background, this receive_messages()
                    # loop keeps running for a moment on the SAME still-alive
                    # client -- and its trailing messages still carry the OLD
                    # (now-condemned) session_id. Since last_saved_session_id
                    # had just become None, "sid != self.last_saved_session_id"
                    # was true again, so this block immediately re-saved the
                    # poisoned id, undoing the reset before the next restart
                    # even got a chance to start genuinely fresh. Guarded now:
                    # once a reset has been triggered this cycle, no further
                    # session_id from this doomed client is trusted.
                    sid = getattr(message, "session_id", None)
                    if sid and sid != self.last_saved_session_id and not self.restart_for_unrecoverable_session:
                        self.last_saved_session_id = sid
                        save_tab_session_id(self.workspace_dir, self.tab_id, sid, self.engine_kind)

                    if isinstance(message, ResultMessage):
                        # Bug fix (2026-09-15): the live "how much is
                        # actually in context right now" signal for the
                        # growth trigger (see tokens_at_last_forced_
                        # compaction's own __init__ comment) -- every real
                        # turn's own ResultMessage.usage already reports
                        # this, no separate query needed. Deliberately
                        # OUTSIDE the result_is_fake check below: even a
                        # hang-interrupted or otherwise "fake" turn's
                        # ResultMessage still reflects a real API call
                        # against the real current context, which is
                        # exactly the number this is meant to track --
                        # only the compaction command's OWN ResultMessage
                        # would be misleading here, and that's excluded
                        # explicitly (compaction's own token accounting
                        # comes from compact_boundary instead, see above).
                        usage = getattr(message, "usage", None)
                        if isinstance(usage, dict) and not self.forced_compaction_result_pending:
                            context_tokens = sum(
                                v for k, v in usage.items()
                                if k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                                and isinstance(v, int)
                            )
                            if context_tokens:
                                self.last_known_context_tokens = context_tokens
                        # Bug fix (2026-09-11): confirmed live -- a
                        # RateLimitEvent(rejected) sets
                        # ignore_next_result_recovery=True specifically so
                        # the trailing ResultMessage the SDK still sends
                        # for that same rejected turn doesn't get mistaken
                        # for a real completion -- but that flag only ever
                        # protected conn_state/the retry timer (below),
                        # never turn_pending/pending_user_text/silent_turn.
                        # Confirmed live: the rejected turn's own
                        # ResultMessage arrived ~2s after the rejection,
                        # cleared turn_pending/pending_user_text and set
                        # silent_turn=True -- so when the retry then fired
                        # 90s later and genuinely completed a real,
                        # substantive reply (present in the session
                        # transcript), that reply was silently swallowed
                        # for the reply's ENTIRE duration: the retry's own
                        # submit() call passes silent=True, and
                        # (True and True) stays True forever after, since
                        # nothing else was left to flip it back. Same bug
                        # class, same fix shape as hang_interrupt_result_pending
                        # (built for a hang-interrupt, a different
                        # trigger) -- captured together here, once, before
                        # ignore_next_result_recovery gets consumed a few
                        # lines down, so every place a ResultMessage
                        # touches turn state uses the SAME verdict.
                        #
                        # forced_compaction_result_pending (2026-09-11) joins
                        # the same verdict for the same reason: our own
                        # "/compact" produces its own ResultMessage that must
                        # not be shown, and must not clear turn_pending/
                        # pending_user_text -- especially since turn_pending
                        # was already False the whole time this ran (forced
                        # compaction only fires while idle), so touching it
                        # here would risk clobbering pending_user_text/
                        # pending_attachments for something ELSE that got
                        # queued in the meantime.
                        result_is_fake = self.hang_interrupt_result_pending or self.ignore_next_result_recovery or self.forced_compaction_result_pending or self.clear_tab_result_pending
                        if not result_is_fake:
                            was_real_user_turn = self.pending_is_real_user
                            # Captured (and consumed) BEFORE the checks below can
                            # re-arm it for the NEXT turn -- see this flag's own
                            # __init__ comment for why it must never let its own
                            # nudge's reply re-trigger itself.
                            was_awaiting_post_turn_check_reply = self._awaiting_post_turn_check_reply
                            self._awaiting_post_turn_check_reply = False
                            self.turn_pending = False
                            self.pending_user_text = None
                            self.pending_attachments = []
                            clear_pending_turn(self.workspace_dir, self.tab_id)
                            # Per explicit correction (2026-09-13): a real
                            # turn's genuine completion (not this fake/
                            # compaction/hang-recovery ResultMessage, and not
                            # an internal nudge answering itself) is exactly
                            # the moment to sanity-check whether the work is
                            # actually done -- see _fire_post_turn_completion_
                            # check's own doc comment for why this replaced a
                            # periodic timer instead.
                            #
                            # Bug fix (2026-09-18), per a real live incident:
                            # was_awaiting_post_turn_check_reply used to
                            # blanket-suppress EVERY completion that followed
                            # a check's own nudge, no matter what that
                            # completion actually contained -- confirmed live
                            # on a real multi-step task (checking a grant
                            # budget file for a typo): the check fired once,
                            # the nudge's reply turned into a whole further
                            # round of real work (multiple tool calls, real
                            # visible text: "Нашла реальную проблему...
                            # Ищу, где именно."), and THAT round then also
                            # ended mid-task with no further text -- but
                            # since it was "the check's reply", the guard
                            # blocked any further check forever, leaving the
                            # tab silently stuck until the user manually
                            # typed "и?" two minutes later to push it along.
                            # The guard's actual purpose (per its own
                            # original comment below) was only ever to stop
                            # an INFINITE TIGHT LOOP of a check chasing its
                            # own GENUINELY EMPTY reply -- a reply that did
                            # real, visible work is exactly the case that
                            # still needs checking again, same as a fresh
                            # real user turn. Re-arm on it, right alongside
                            # was_real_user_turn; still suppressed for a
                            # check-reply that came back with nothing at all
                            # (the elif below, unchanged for that case).
                            # A resumed real question must not end in silence (2026-09-23, live: a
                            # restart-interrupted question was "answered" with a bare [[NO_UPDATE]] the user
                            # never saw, so the tab looked idle until they typed "continue").
                            # A provider-rejected turn is not "finished but silent": re-asking "did you
                            # actually finish?" (or re-nudging a resumed question) against a request the
                            # provider keeps rejecting is what looped forever on a bad API key.
                            error_result = bool(getattr(message, "is_error", False))
                            resumed_nudged = False
                            if self.resumed_unanswered_question is not None and not error_result:
                                if self.turn_saw_any_visible_text:
                                    self.resumed_unanswered_question = None
                                    self.resumed_answer_nudges = 0
                                elif self.resumed_answer_nudges < RESUMED_ANSWER_MAX_NUDGES:
                                    self.resumed_answer_nudges += 1
                                    resumed_nudged = True
                                    log_event("engine", "resumed_question_silent_reply_renudged", tab_id=self.tab_id, attempt=self.resumed_answer_nudges)
                                    self.inject_proactive(
                                        "[The user's message below was interrupted by a restart and you have NOT yet shown them any "
                                        "visible answer to it -- they are waiting and currently see nothing. A reply of [[NO_UPDATE]] "
                                        "is not allowed here. Answer it now, in "
                                        f"{current_language_name(self.tab_id)}: if the work was already completed, say what was done and "
                                        "where the result is (check first if you're unsure); if it still needs doing, do it. Never "
                                        "mention the restart itself.\n\nThe user's original message:\n"
                                        f'"{self.resumed_unanswered_question}"]',
                                        pending_text=self.resumed_unanswered_question,
                                    )
                                else:
                                    log_event("engine", "resumed_question_renudge_exhausted", tab_id=self.tab_id)
                                    self.resumed_unanswered_question = None
                                    self.resumed_answer_nudges = 0
                            if resumed_nudged:
                                pass
                            elif error_result:
                                log_event("engine", "post_turn_completion_check_skipped_error_result", tab_id=self.tab_id)
                            elif was_real_user_turn or (was_awaiting_post_turn_check_reply and self.turn_saw_any_visible_text):
                                self._fire_post_turn_completion_check()
                            elif not was_awaiting_post_turn_check_reply and not self.turn_saw_any_visible_text:
                                # Per explicit instruction (2026-09-13), after a
                                # real incident: a PROACTIVE turn (a scheduled
                                # mailbox check) completed with genuinely EMPTY
                                # visible content -- real work done, real output
                                # tokens spent, but no text (not even
                                # [[NO_UPDATE]]) ever reached the wire. Most
                                # proactive completions are legitimately silent
                                # via a real NO_UPDATE text block (which DOES
                                # count as "visible" here, see turn_saw_any_
                                # visible_text's own comment) -- this only fires
                                # for the genuinely-empty case; a check-reply
                                # that's ALSO genuinely empty stops here (see
                                # the branch above for the non-empty case),
                                # never chaining into an infinite tight loop of
                                # a check chasing its own silence.
                                log_event("engine", "post_turn_completion_check_proactive_empty", tab_id=self.tab_id)
                                self._fire_post_turn_completion_check()
                            elif was_awaiting_post_turn_check_reply and not self.turn_saw_any_visible_text and self._turn_has_unresolved_tool_error():
                                # Ground-truth override (2026-09-22), per
                                # explicit instruction that Caroline must
                                # push through to a real answer rather than
                                # formally "end a turn": neither branch
                                # above caught this case -- a bare
                                # [[NO_UPDATE]] check-reply (stripped before
                                # turn_saw_any_visible_text is ever set, see
                                # _strip_no_update_from_wire's own call
                                # site) would otherwise be accepted at face
                                # value and silently drop the check here.
                                # But this episode's own ground truth (see
                                # ToolOutcome/turn_tool_outcomes) shows a
                                # tool call that's still sitting at
                                # is_error=True -- the model's silence is
                                # not evidence the work is actually done, so
                                # don't trust it; re-fire the same check
                                # again, now carrying that ground truth (see
                                # _fire_post_turn_completion_check's own
                                # _turn_outcomes_summary() call), same flat
                                # re-ask-forever shape every other internal
                                # nudge here already uses.
                                log_event("engine", "post_turn_completion_check_ground_truth_override", tab_id=self.tab_id)
                                self._fire_post_turn_completion_check()
                        self.classifier_refusal_retry_count = 0
                        self.last_api_retry_error = None
                        self.consecutive_auth_retry_failures = 0
                        # Bug fix (2026-09-10): confirmed live -- a
                        # RateLimitEvent(rejected) schedules a 90s retry
                        # timer and sets ignore_next_result_recovery=True
                        # specifically so the ResultMessage the SDK still
                        # sends for that same failed turn doesn't get
                        # mistaken for real recovery. That flag protected
                        # conn_state (stayed "limited", correctly) but NOT
                        # this retry timer -- _clear_api_retry_timer() ran
                        # unconditionally right here and silently cancelled
                        # the just-scheduled retry in the same breath it
                        # was created. Nothing ever rescheduled a fresh
                        # one, so the tab sat in "limited" forever with a
                        # dead retry mechanism (confirmed live: 3/3 real
                        # incidents show reset_at.reschedule.reset_at with
                        # no api_retry_firing ever following). Now the
                        # whole "this ResultMessage isn't real recovery"
                        # branch, including the retry timer, is gated on
                        # the SAME flag.
                        if self.ignore_next_result_recovery:
                            self.ignore_next_result_recovery = False
                        else:
                            self._clear_api_retry_timer()
                            if self.suppress_next_conn_state_reset:
                                self.suppress_next_conn_state_reset = False
                            elif self.conn_state.get("kind") != "connected":
                                self._set_conn_state("connected")

                    wire = message_to_wire(message)
                    # Bug fix (2026-09-10/11): an interrupted or rejected
                    # turn's own ResultMessage carries whatever partial/
                    # empty/error result the CLI had at that moment --
                    # never something to show as if it were Caroline's
                    # real answer. Suppressing the send (not just the
                    # turn_pending clear above) also keeps chat.js's
                    # turnQueue placeholder for the ORIGINAL real submit
                    # open until the REPLAY's own genuine ResultMessage
                    # resolves it for real, instead of being wrongly
                    # resolved early by this one. (silent_turn itself
                    # removed 2026-09-11, per explicit instruction -- it
                    # was a whole-session AND-latch that couldn't be
                    # un-set once a real conversation had made it False,
                    # and its own state got corrupted by exactly this kind
                    # of fake-ResultMessage bug. Whether a REAL message
                    # gets shown is now a per-message [[NO_UPDATE]]
                    # decision the model itself makes -- this check here
                    # is now purely about not sending a KNOWN-meaningless
                    # fake result.
                    #
                    # forced_compaction_result_pending (2026-09-11) needs its
                    # OWN, broader term here rather than folding into
                    # result_is_fake's usual ResultMessage-only check: a
                    # real "/compact" round trip also produces real
                    # AssistantMessage/SystemMessage traffic BEFORE its
                    # ResultMessage (live-confirmed: an assistant reply like
                    # "Not enough messages to compact.", plus a "compacting"
                    # status message) that would otherwise show up as a
                    # bogus chat bubble -- suppress the whole stretch, not
                    # just the terminal ResultMessage.)
                    if wire is not None and not self.forced_compaction_result_pending and not (isinstance(message, ResultMessage) and result_is_fake):
                        wire = _strip_no_update_from_wire(wire)
                        if wire is not None:
                            wire = await self._translate_wire_visible_text(wire)
                            # Bug fix (2026-09-10): confirmed live -- voice-reply
                            # auto-play/animation (chat.js checks evt.isVoice on
                            # the "result" event) never fired, because this send
                            # site never attached isVoice at all. server.ts's
                            # original does: { type: "sdk_message", message,
                            # isVoice: this.turnIsVoice } specifically on the
                            # "result" message -- ported that shape here; every
                            # other message type is sent exactly as before.
                            #
                            # Bug fix (2026-09-13), confirmed live from a real
                            # incident: a voice-originated turn that ran long
                            # (several minutes of tool calls) DID produce a real,
                            # visible mid-turn text reply (shown as a chat bubble)
                            # -- but the SDK's own transport then hit an unrelated
                            # failure ("JSON message exceeded maximum buffer size")
                            # before ever reaching a clean ResultMessage, so
                            # isVoice (previously attached ONLY to "result")
                            # never reached the client at all -- nothing was ever
                            # spoken/animated, even though the user had already
                            # SEEN a real reply. Voice/Visual-Mode playback must
                            # not depend on the turn eventually reaching a clean
                            # result -- attach isVoice to EVERY sdk_message now
                            # (self.turn_is_voice is stable for the whole logical
                            # turn, including across a crash-triggered replay --
                            # see its own "leave untouched"/"preserved" comments
                            # below), so chat.js can speak/animate each visible
                            # reply as it actually arrives.
                            envelope: dict[str, Any] = {"type": "sdk_message", "message": wire, "isVoice": self.turn_is_voice}
                            if wire.get("type") == "result":
                                # Distinguishes a genuine turn's settlement from
                                # OUR OWN internal post-turn completion check
                                # re-asking itself (was_awaiting_post_turn_check_
                                # reply, captured above -- see
                                # _fire_post_turn_completion_check). A caller
                                # that cares whether a settlement represents a
                                # real externally-triggered turn (e.g. the
                                # Ratatosk owner-channel's "did this actually
                                # get replied to" fallback, app/main.py's
                                # _ratatosk_session_send) needs this signal --
                                # without it, a legitimately-silent internal
                                # check-reply (a bare [[NO_UPDATE]] answering
                                # our own nudge, not the owner) looks identical
                                # to a real turn that genuinely never got
                                # replied to. Nothing else currently reads it.
                                envelope["wasInternalCheckReply"] = was_awaiting_post_turn_check_reply
                            await self.send(envelope)
                            if wire.get("type") in ("assistant", "result"):
                                if not self.real_user_turn_answered:
                                    self.real_user_turn_answered = True
                                if wire.get("type") == "assistant":
                                    # Bug fix (2026-09-13), confirmed live from the
                                    # real session transcript (tab 1, "Основной
                                    # диалог", 2026-09-13 ~14:04-14:08 UTC): dozens
                                    # of consecutive assistant wire messages during
                                    # a long multi-mailbox tool-calling stretch were
                                    # tool_use-ONLY (no text block) -- chat.js's own
                                    # hasToolUse check (mirrored server-side in
                                    # history.py's _extract_entries_from_jsonl)
                                    # suppresses exactly these as pre-tool narration
                                    # the user never sees. This line used to bump
                                    # last_visible_output_at on EVERY one of them
                                    # regardless, repeatedly re-arming
                                    # _check_progress_narration()'s 60s cooldown
                                    # without the user ever actually seeing anything
                                    # new -- the confirmed cause of a 5+ minute
                                    # narrator silence with real work still running.
                                    # Only a message carrying an actual visible text
                                    # block should count as "the user just saw
                                    # something new".
                                    content_blocks = wire.get("message", {}).get("content") or []
                                    has_visible_text = any(
                                        isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
                                        for b in content_blocks
                                    )
                                    has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content_blocks)
                                    log_event(
                                        "engine", "assistant_wire_sent", tab_id=self.tab_id,
                                        has_visible_text=has_visible_text, has_tool_use=has_tool_use,
                                        last_visible_output_updated=has_visible_text,
                                    )
                                    if has_visible_text:
                                        self.last_visible_output_at = time.monotonic()
                                        self.turn_saw_any_visible_text = True
                                        self.consecutive_narration_count = 0

                    if isinstance(message, ResultMessage):
                        if result_is_fake:
                            # Consumed here (not earlier) -- this is the
                            # last of the three places this ResultMessage
                            # touches result_is_fake-gated state. Leave
                            # turn_is_voice untouched too (not reset to
                            # False here) -- preserved for whichever REAL
                            # ResultMessage eventually completes this same
                            # logical turn.
                            self.hang_interrupt_result_pending = False
                            if self.forced_compaction_result_pending:
                                self.forced_compaction_result_pending = False
                                log_event("engine", "forced_compaction_done", tab_id=self.tab_id, subtype=message.subtype)
                                # Per explicit instruction (2026-09-18): the
                                # status-bar yellow dot (_compute_public_
                                # status) and anything else that treats
                                # forced_compaction_result_pending as "still
                                # compacting" needs to hear about this edge
                                # THE MOMENT it happens, not whenever the
                                # next unrelated turn_pending flip republishes
                                # status -- compaction touches neither.
                                asyncio.create_task(self._publish_status())
                                self._drain_compaction_queue()
                            elif self.clear_tab_result_pending:
                                self.clear_tab_result_pending = False
                                log_event("engine", "clear_tab_result_discarded", tab_id=self.tab_id, subtype=message.subtype)
                            else:
                                log_event("engine", "fake_result_message_preserved", tab_id=self.tab_id)
                        else:
                            # One long-lived client now -- the turn is done,
                            # but the stream stays open for the next queued
                            # turn (_input_stream yields it). No per-turn
                            # restart.
                            self.turn_is_voice = False

                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        self.has_seen_init = True
                        log_event("engine", "init_received", tab_id=self.tab_id, elapsed_ms=round((time.monotonic() - query_started_at) * 1000), resume=resume_session_id)
                        if self.conn_state.get("kind") != "connected":
                            self._set_conn_state("connected")
                        # A failed server here behaves the same way it would in
                        # an interactive `claude` session: unavailable, but
                        # everything else still works -- previously this would
                        # have had to throw and tear down the WHOLE session to
                        # retry, which just re-fails the same way on every
                        # restart for an unrelated server (an infinite
                        # "recovering session..." loop). 'needs-auth'/'pending'/
                        # 'disabled' aren't retried here (an auth prompt, a
                        # race that may still resolve, or intentional), only
                        # 'failed' is.
                        failed_servers = [s.get("name") for s in (message.data.get("mcp_servers") or []) if s.get("status") == "failed"]
                        if failed_servers:
                            log_event("engine", "mcp_servers_failed", tab_id=self.tab_id, servers=failed_servers)
                            for server_name in failed_servers:
                                if self.client and server_name:
                                    self._schedule_mcp_reconnect(self.client, server_name)
                        # See dynamic_disallowed_tools' own comment above --
                        # this init message's own "tools" field is the real,
                        # live ground truth for what's actually available THIS
                        # connection (own and foreign alike); persisted so the
                        # NEXT query build (not this already-in-flight one --
                        # disallowed_tools is already locked in for it) closes
                        # the loop instead of staying wrong forever.
                        overlap = compute_foreign_tool_overlap(message.data.get("tools") or [], set(mcp_servers.keys()))
                        save_discovered_foreign_tool_overlap(self.workspace_dir, overlap)
                        if overlap:
                            log_event("engine", "foreign_tool_overlap_discovered", tab_id=self.tab_id, tools=overlap)

                if not self.ended:
                    log_event("engine", "stream_ended_unexpectedly", tab_id=self.tab_id, elapsed_ms=round((time.monotonic() - query_started_at) * 1000), has_seen_init=self.has_seen_init)
                    raise RuntimeError("query() stream ended unexpectedly")

            except Exception as exc:  # noqa: BLE001 -- must classify, not swallow
                if self.ended:
                    return

                if self.user_stop_requested:
                    self.user_stop_requested = False
                    # Bug fix (2026-09-11): no more explicit "stopped"
                    # wire message -- setting turn_pending below (via the
                    # property setter) already auto-publishes the correct
                    # status (READY, since nothing else is pending yet at
                    # this exact instant), and the imminent resubmit()
                    # right after auto-publishes WORKING again. Two states
                    # correctly represented, no special-cased message type.
                    self.turn_pending = False
                    self.pending_user_text = None
                    self.pending_attachments = []
                    self.submit(
                        "[The user just stopped what you were doing. Whatever action was in progress may be "
                        "incomplete or partially applied -- don't assume it finished. Wait for their next "
                        "instruction.]",
                        [], False,
                    )
                    continue

                if self.restart_pending:
                    self.restart_pending = False
                    replay_text = self.pending_user_text
                    replay_attachments = self.pending_attachments
                    log_event("engine", "restart_pending_handled", tab_id=self.tab_id, has_replay=replay_text is not None)
                    self.last_rate_limit_info = None
                    self.turn_pending = False
                    self.pending_user_text = None
                    self.pending_attachments = []
                    clear_pending_turn(self.workspace_dir, self.tab_id)
                    if replay_text is not None:
                        self.submit(replay_text, replay_attachments, True, self.turn_is_voice)
                    continue

                if self.restart_for_unrecoverable_session:
                    self.restart_for_unrecoverable_session = False
                    replay_text = self.unrecoverable_session_replay_text
                    replay_attachments = self.unrecoverable_session_replay_attachments
                    self.unrecoverable_session_replay_text = None
                    self.unrecoverable_session_replay_attachments = []
                    log_event("engine", "restart_unrecoverable_session", tab_id=self.tab_id, has_replay=replay_text is not None)
                    self.turn_pending = False
                    self.pending_user_text = None
                    self.pending_attachments = []
                    clear_pending_turn(self.workspace_dir, self.tab_id)
                    if replay_text is not None:
                        # No per-call directive text needed (2026-09-09
                        # redesign) -- the fresh query() this replay lands in
                        # already carries language_hint_instruction
                        # (current_language_name(), synchronous, instant) in
                        # its own system prompt. Just kick off a background
                        # refresh for the next reset/turn.
                        refresh_language_in_background(self.last_saved_session_id, self.tab_id)
                        self.submit(replay_text, replay_attachments, True, self.turn_is_voice)
                    continue

                balance_source = detect_balance_exhaustion(str(exc))
                if balance_source:
                    log_event("engine", "thrown_balance_exhaustion", tab_id=self.tab_id, source=balance_source)
                    explanation = self._handle_balance_exhausted()
                    self.turn_pending = False
                    self.pending_user_text = None
                    self.pending_attachments = []
                    clear_pending_turn(self.workspace_dir, self.tab_id)
                    self._set_conn_state("billing_blocked", explanation)
                    self._schedule_api_retry(f"billing_error(thrown):{balance_source}", self.turn_is_voice)
                    continue

                if self.last_api_retry_error == "rate_limit":
                    log_event("engine", "silent_death_rate_limit", tab_id=self.tab_id)
                    self.turn_pending = False
                    clear_pending_turn(self.workspace_dir, self.tab_id)
                    self._set_conn_state("limited", "Hit the Claude usage limit. Retrying automatically.")
                    self._schedule_api_retry("api_retry:rate_limit", self.turn_is_voice)
                    continue

                if self.last_rate_limit_info and self.last_rate_limit_info.get("status") == "rejected":
                    log_event("engine", "silent_death_rate_limit_rejected", tab_id=self.tab_id)
                    self.turn_pending = False
                    clear_pending_turn(self.workspace_dir, self.tab_id)
                    self._handle_rate_limit_rejected("silent-stream-death", self.last_rate_limit_info)
                    continue

                # Per explicit instruction (2026-09-13): raising
                # max_buffer_size (options_kwargs, above) is a ceiling, not
                # a fix -- confirmed live that blindly replaying the exact
                # same request after this failure just repeats the exact
                # same expensive approach (reading several full-resolution
                # images via the native Read tool back-to-back) and can
                # cross even a much higher ceiling. A dedicated note is
                # needed so the model actually changes approach, not just
                # retries -- generic _handle_failure's own {exc} text alone
                # gives it nothing to act on differently. No manual
                # transcript surgery to "delete" the oversized result (the
                # CLI already wrote it to the resumed session's own
                # transcript before this failure fired; editing that file
                # out from under a resuming CLI is not safe) -- the EXISTING
                # forced-compaction-on-growth check (_check_forced_
                # compaction, 100KB threshold) already dehydrates it out to
                # a file + pointer automatically within one watchdog tick
                # of the replayed turn actually completing, which is
                # exactly the "write to a file and point at it" shape this
                # was missing -- it just needs THIS retry to succeed rather
                # than loop forever repeating the same failure.
                if OVERSIZED_MESSAGE_PATTERN.search(str(exc)):
                    log_event("engine", "oversized_tool_result_failure", tab_id=self.tab_id, error=str(exc))
                    await self._handle_failure(
                        exc,
                        extra_note=(
                            "The failure was a single tool result too large to process (most likely reading "
                            "several full-resolution images in a row with the Read tool). Don't repeat that same "
                            "approach: process images one at a time rather than in a burst, and prefer whatever "
                            "cheaper/lower-resolution option is available (a thumbnail, a smaller crop, a text "
                            "description of the image) over reading multiple large full-resolution files back to "
                            "back."
                        ),
                    )
                    continue

                await self._handle_failure(exc)


def _format_timestamp_for_model(dt: datetime) -> str:
    weekday = dt.strftime("%a")
    month = dt.strftime("%b")
    hour12 = dt.strftime("%I").lstrip("0") or "0"
    minute = dt.strftime("%M")
    ampm = dt.strftime("%p")
    tz_name = dt.strftime("%Z")
    return f"{weekday}, {month} {dt.day}, {dt.year}, {hour12}:{minute} {ampm} {tz_name}".strip()


def _uuid() -> str:
    return uuid_mod.uuid4().hex
