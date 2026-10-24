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

from app.action_registry import Action, ActionRegistry
from app.archive_chunks import chunk_archive
from app.durability import (
    _sanitize_tab_id,
    claude_project_dir,
    clear_action_state,
    clear_pending_api_retry,
    clear_pending_one_shot_followup,
    clear_pending_turn,
    clear_tab_continuity_archive,
    clear_tab_session_id,
    compute_foreign_tool_overlap,
    dehydrated_dir,
    find_most_recent_claude_session_id,
    has_chat_mode,
    load_action_state,
    load_chat_mode,
    load_discovered_foreign_tool_overlap,
    load_tab_continuity_archive,
    load_tab_session_id,
    save_action_state,
    save_discovered_foreign_tool_overlap,
    save_pending_api_retry,
    save_pending_one_shot_followup,
    save_pending_turn,
    save_chat_mode,
    save_tab_continuity_archive,
    save_tab_session_id,
    session_transcript_path,
    openai_transcripts_dir,
)
from app.compaction import compact_session_if_due
from app.engines.base import AgentEngine
from app.engines.claude_engine import ClaudeEngine
from app.engines.codex_engine import CodexEngine
from app.openai_mode import build_codex_options, openai_available
from app.transcript_rotate import rotate_transcript
from app.history import ATTACHMENT_NOTE_MARKER, _ATTACHMENT_NOTE_PREFIXES, _HISTORY_STAMP_PATTERN, iter_entries_reversed, iter_lines_reversed, read_last_context_tokens
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
from app.small_model_engine import (
    CASCADE_SKIP_TRANSLATION_WHEN_IN_LANGUAGE, answer_is_in_language, draft_narration_remark, run_small_model_turn,
    try_fast_answer,
)
from app.task_supervisor import supervise
from app.persona import get_persona, get_persona_gender, persona_system_prompt_append
from app.owner_profile import get_owner_profile, owner_profile_system_prompt_clause
from app.working_memory import load_working_memory, working_memory_system_prompt_clause
from app.memory_topics import memory_topics_system_prompt_clause
from app import agent_registry
from app import pseudo_tab
from app.agent_definitions import caroline_agents
from app.policies import (
    ALWAYS_ON_INSTRUCTIONS, continuity_pointer_instruction, language_hint_instruction, recent_dialogue_history_instruction,
    running_agents_pointer_instruction, pseudo_tab_pointer_instruction,
)
from app.operations import REGISTRY
from app.pdf_pages import extract_pdf_page_texts
from app.process_activity import ProcessActivityMonitor
from app import memory_nudge
from app.session_context import get_action_closer, get_recent_real_user_directives, get_sessions_getter, register_action_closer, register_inject_proactive, register_permission_blocker, register_recent_real_user_directives, register_reply_watch, register_file_transfer_guard, register_send_action_guard, register_send_for_tab, register_sessions_getter, set_cli_pid_sink, set_inject_proactive, set_send, set_tab_id
from app.plugins.sw_api import get_funds_exhausted_reason, mark_funds_exhausted
from app import memory_turns
from app.process_kill import kill_process_tree
from app.sw_gate import require_sw_or_prompt
from app.subscription_mode import (
    build_options_env,
    chat_mode_eligible,
    get_model_override,
    resolve_mode,
)
from app.wire import message_to_wire
from app.workspace_dir import WORKSPACE_DIR
from app import sw_history

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
# Connection states that exist only because a retry loop is waiting on them;
# an explicit Stop cancels that loop, so it clears these states too.
STOP_RESETS_CONN_STATES = ("limited", "billing_blocked")
# Stop tears down the main model and everything it launched through tools --
# except a write into memory already under way, which is left to finish.
STOP_KEEPS_TOOLS = frozenset({"save_info", "save_document", "owner_profile_remember"})
# Shown to the model with the user's first message after a Stop, as its own block.
STOP_NOTE_TEXT = (
    "[Note: the user pressed Stop during your previous turn, and everything it was running was killed. "
    "Pressing Stop usually means the user did not like what you were doing or how you were doing it: they "
    "may well expect you to change your behavior, not to carry on. Whatever action was in progress may be "
    "incomplete or partially applied -- do not assume it finished, and do not resume it or repeat it unless "
    "the user asks you to. Read their message below with that in mind, and if it is not clear what they want "
    "done differently, ask.]"
)
# Shown to the model with the user's first message after the permission guard stopped a
# turn (2026-10-11) -- same {reason}-interpolated-note shape as STOP_NOTE_TEXT above, but
# with its own, more specific explanation (a plain Stop note would not tell the model WHY).
PERMISSION_NOTE_TEXT_TEMPLATE = (
    "[Note: your previous turn was stopped because {reason} needs the user's explicit permission, which you "
    "did not have yet. Asking a yes/no question and getting no clear answer is NOT permission -- silence "
    "means no. Ask the user directly and plainly, and wait for their actual answer before attempting this or "
    "an equivalent action again.]"
)
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

# How submit() opens the interim-notes prefix -- _narration_task strips it back off.
_INTERIM_NOTES_HEADER = "[Interim messages you already showed the user "

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
# Raised 3 -> 4 (2026-10-15), per explicit instruction, same incident as
# NARRATION_NETWORK_TIMEOUT_S's own 2026-10-15 note just below: more
# attempts per burst gives a flaky SMALL-model endpoint more chances to
# land one success before a long-running turn's own watchdog tick moves on.
NARRATION_GENERATION_RETRY_ATTEMPTS = 4

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
# Raised 30.0 -> 45.0 (2026-10-15), per explicit instruction, after a real
# incident: the SMALL model's own ai:resolve endpoint degraded for a couple
# of hours (41 of 104 narration attempts that day failed with a flat 30.0s
# ReadTimeout, Camerlengo itself confirmed healthy -- the bottleneck is
# upstream of it), leaving long stretches of a still-running turn with no
# narration at all despite 3-4 retries, since every attempt hit the same
# wall before the model had a chance to actually answer. Deliberately
# accepts a looser "every minute" promise during a degraded patch like this
# one over guaranteed silence -- the explicit tradeoff chosen when this was
# raised.
NARRATION_NETWORK_TIMEOUT_S = 45.0

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
# Lowered 15.0 -> 8.0 (2026-10-15), same incident/instruction as the two
# constants above: a fully-exhausted burst (now up to 4 x 45s = 180s worst
# case) already spaces attempts out plenty on its own during a real outage;
# the extra 15s on top of that, repeated every burst for the lifetime of a
# long turn, was pure additional silence for no real benefit to backing off
# -- shortened so a still-running turn gets more total bursts, and each one
# more chances to land a comment.
NARRATION_FAILURE_RETRY_S = 8.0

# Voice narration (2026-10-09), per explicit instruction ("непрерывный диалог, чтобы
# реплики не накладывались одна на другую, но при этом между ними не было значимых
# пауз. И чтобы они были осмысленными"): for an sw-mode VOICE turn (TTS or Visual
# Mode) the once-a-minute cadence above is a minute of silence on a live call. Those
# turns get their own loop (_voice_narration_loop) instead: the next remark is drafted
# while the previous one is still being spoken, and shown once chat.js reports its
# speech queue quiet (speech_state, see on_client_speech_state). Text-only turns and
# SDK-path turns keep PROGRESS_NARRATION_INTERVAL_MS unchanged.
VOICE_NARRATION_TICK_S = 1.0
# When nothing new has happened and the user has heard nothing for VOICE_NARRATION_STALE_S,
# a remark only may not repeat the last this many remarks (instead of any said this turn).
# Live 2026-10-14: a 2-minute turn had 9 remarks behind it, every new draft of both models
# was rejected as a repeat of one of them, and the user heard nothing for 31.7s right
# before the answer. Saying again, in other words, that the work goes on -- after half a
# minute -- is what a person would do; silence on a call sounds like a hang.
VOICE_NARRATION_STALE_REPEAT_WINDOW = 2
# Per explicit instruction (2026-10-11, and again 2026-10-12 when its cost for the voice
# loop was put to him: "он необходим для предотвращения ошибок модели, оставляем"): every
# outbound text goes through the server's reply.review, a narration remark first of all.
# Measured 2026-10-12: 2.2-3.8s per remark, on top of ~0.6s of drafting. It runs before
# the remark's audio is made -- while the previous remark is still being spoken where
# there is one, and inside the merge window for the first remark of a turn. Fail-open as
# everywhere else, and bounded: a review that has not answered in this time lets the
# remark go as it was drafted.
VOICE_NARRATION_REVIEW = True
VOICE_NARRATION_REVIEW_TIMEOUT_S = 6.0
# No remark before this much of a turn has passed -- a quick turn answers on its own.
VOICE_NARRATION_FIRST_S = 5.0
# Drafting starts this early so the first remark is ready by VOICE_NARRATION_FIRST_S.
VOICE_NARRATION_PREPARE_FROM_S = 1.5
# Breath between the end of one spoken line and the start of the next.
VOICE_NARRATION_AFTER_SPEECH_GAP_S = 1.2
# With no new facts since the last remark, at most one remark per this long -- a new
# fact is what makes a remark worth saying; without one this is the longest silence
# allowed, not a cadence (see consecutive_narration_count's "talking to herself").
# 10s, not 15 (2026-10-10): live, 15s left a 20s silence while the model thought
# with no new step -- one more chance to say "this one is taking a while".
VOICE_NARRATION_STALE_S = 10.0
# Drafting for that case starts this much sooner (2026-10-11): a draft plus its audio
# takes 4-6s, and started only once the 10s were up it made the silence 14-15s.
VOICE_NARRATION_STALE_LEAD_S = 5.0
# What a "nothing new" draft is told once the user already knows the work goes on: after
# the first such remark in a row (_step_voice_narration), and right away when such a draft
# came back as a repeat (_draft_voice_narration_body).
VOICE_NARRATION_SAY_ELSE = (
    "the user already knows the work is still going on; say how long it has taken so far, or which part is being done now."
)
# A long turn with nothing new to tell (2026-10-15): live, 22 remarks in 4.5 minutes, most
# of them "Продолжаю ..." every ten seconds. Each further remark in a row with nothing new
# waits this much longer than the one before, up to VOICE_NARRATION_STALE_MAX_S; the
# first new fact puts it back to VOICE_NARRATION_STALE_S.
VOICE_NARRATION_STALE_STEP_S = 2.0


def _spoken_duration(seconds: float) -> str:
    """Elapsed time the way a person would say it (2026-10-15): given "104 s" the narrator
    said "Уже сто четыре секунды ..."; past a minute it is half-minutes, "about 1.5 minutes"."""
    if seconds < 60:
        return f"{int(seconds)} s"
    minutes = round(seconds / 30) / 2
    return f"about {minutes:g} minute{'s' if minutes != 1 else ''}"


# 14, not 18: with 18 the silences heard live reached 17-20s (the interval plus drafting
# and review), which already sounds like a pause.
VOICE_NARRATION_STALE_MAX_S = 14.0
# While a tier's answer is with the verifier, a drafted remark waits until the user has
# heard nothing for this long: the answer is most likely a few seconds away (the check
# takes 2.5-4.5s, the answer's audio is made meanwhile). Live 2026-10-17: a 13.6s remark
# went out 0.6s before the verified answer, which then waited 12s behind it and repeated
# its prices. A check that runs longer than this still gets a remark, not a silence.
VOICE_NARRATION_ANSWER_HOLD_S = 6.0
# A drafted remark not yet shown by then describes a state that has moved on.
VOICE_NARRATION_MAX_AGE_S = 20.0
# Shorter than NARRATION_FAILURE_RETRY_S: on a live call, 15s is itself the gap.
VOICE_NARRATION_FAILURE_RETRY_S = 5.0
# Only used when chat.js never reported speech_state (an older client): a rough
# speaking rate to guess when the last remark finished playing.
VOICE_NARRATION_EST_CHARS_PER_S = 14.0
# A "speaking" report older than this is not trusted: a dropped connection would
# otherwise leave the client looking busy forever and silence narration for good.
VOICE_NARRATION_CLIENT_SPEAKING_STALE_S = 120.0

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
# chat_mode_eligible() holds -- a PAID SquirrelWisdom balance (no Claude
# subscription needed since 2026-10-22). Nothing changes for anyone who never opens that
# toggle: every tab still starts on "claude", identical to this flag's old
# permanent False.

# How many recent user-visible dialogue lines (_read_recent_dialogue_lines)
# feed progress narration, which genuinely needs real back-and-forth
# (both speakers) to have something to react to.
RECENT_DIALOGUE_WINDOW = 12

# How long an sw turn waits for the previous turn's background history work
# (its tool-work summary, a compaction -- see app/sw_history.py's after_turn)
# before building its context anyway. Without a summary the context falls back
# to the raw call previews with their links, so nothing is lost by not waiting
# longer, only made less compact.
SW_HISTORY_WAIT_S = 60

# Bug fix (2026-10-12), confirmed live (a real false-positive incident: the
# user HAD explicitly approved a conference submission, in an earlier turn
# of a long multi-step task -- build an app, research venues, draft a paper
# -- but the permission guard still reported no approval present and the
# turn was wrongly stopped). _check_permission_guard used to reuse
# _gather_recent_dialogue_for_narration() wholesale, inheriting TWO limits
# that make sense for narration but not for this: a short RECENT_DIALOGUE_
# WINDOW=12 tuned for "does the narrator have enough to react to", and a
# hard floor at the CURRENT real user turn's own start time (see that
# function's own 2026-09-16 bug fix) that excludes every EARLIER turn
# entirely -- exactly where a multi-step task's actual approval usually
# lives, once several turns of real work have passed since it was given.
# The guard needs to see further back than the narrator ever does: approval
# given once for a multi-step task should stay visible for as long as that
# task continues, not just within the turn it happened to be given in.
PERMISSION_GUARD_DIALOGUE_WINDOW = 60

# Per explicit instruction (2026-10-12): the reply-expected auto-checker's
# own tapering schedule for watching a mailbox after a message is sent --
# "ближайший час каждые 5 минут, в ближайшие 6 часов каждые 15 минут"
# (next hour every 5 minutes, next 6 hours every 15 minutes). Each entry is
# (interval_s, tick_count) run in order; the window ends -- silently, no
# nudge -- once every phase's ticks are exhausted with no new mail. Email
# only: every messenger channel already notifies on arrival on its own
# (push connection, or Ratatosk's tight global poll) -- see
# _on_outbound_message_sent's own docstring for why there is nothing to
# schedule there.
REPLY_WATCH_SCHEDULE = ((300.0, 12), (900.0, 24))

# Hard break for the post-turn completion check (2026-10-03): it re-arms itself
# after any reply with visible text, so a check whose own replies keep producing
# text chained forever. At most this many checks in a row, counted from the last
# real user message; reset by a real user message, and the Stop button halts it
# outright until the next real user message.
POST_TURN_CHECK_MAX_ROUNDS = 5

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
# Per explicit instruction (2026-10-19), after a confirmed live incident:
# tab 1's own native "/compact" sat completely stuck for 14+ minutes, its
# live transcript never touched again after forced_compaction_triggered --
# with zero automatic recovery, since the regular hang watchdog bails out
# immediately whenever turn_pending is False, and compaction deliberately
# never sets turn_pending (see _push_internal_command's own docstring). See
# chat_session_mixins/compaction.py's own _check_compaction_stuck.
COMPACTION_STUCK_TIMEOUT_S = 120
# Per explicit instruction (2026-10-10): messages the user sends one after
# another are one message for the model. A user message waits this long;
# every further one within the window joins it and restarts the wait. The
# chat window still shows each bubble as it was typed.
USER_MESSAGE_MERGE_WINDOW_S = 5.0
# Experiment (2026-10-11): the fast lane starts on a held VOICE message while the merge
# window is still running. The window is 5s in which nothing else happens, and live the
# fast lane then took another 1.8-5s: 12.9s from the question to the first spoken word
# of a tool turn. Now its answer -- or its decline, with the first spoken remark already
# synthesized -- is ready when the window closes. The window itself is untouched: a
# further message within it restarts this on the joined text, at the price of one
# wasted fast-lane call. Typed messages are left out, they are the ones that come in
# bursts. False runs the fast lane after the window, as before.
FAST_LANE_DURING_MERGE_WINDOW = True
# Per explicit instruction (2026-10-10): a compaction loses whatever the
# model learned and never saved, so an hourly/growth compaction is preceded
# by a service turn asking it to save first (see _check_forced_compaction).
MEMORY_FLUSH_BEFORE_COMPACTION_TEXT = (
    "[Your context is about to be compacted: much of the detail of what you have done and learned since your "
    "last save_info will be gone afterwards. Save to memory now, with save_info, everything new and specific "
    "from that work that may matter later -- people, contacts, links, numbers, file locations, what was decided "
    "or done and its result, what is still open. Then reply with exactly [[NO_UPDATE]].]"
)
# Bug fix (2026-09-15): replaced the old byte-based growth threshold (see
# tokens_at_last_forced_compaction's own __init__ comment for why bytes was
# the wrong signal) -- this is real context tokens, checked against
# last_known_context_tokens - tokens_at_last_forced_compaction.
#
# Raised (2026-09-27), per explicit instruction, after measuring the real cost live: 60k
# was an own-choice guess made back when native auto-compaction (see FORCED_COMPACTION_
# HOURLY_MS's own comment) had NEVER fired even once (0 observed) -- the whole reason this
# workaround exists at all. It has since fired for real 6 times, at pre_tokens ranging
# 504,971 to 1,255,769 (median ~744k) -- the model's own real threshold is nowhere near
# 60k. Meanwhile this "growth" reason alone had forced 210 of 323 total compactions,
# median pre_tokens 79,455 -- i.e. this app was paying for a full-context compaction call
# (itself real, billed tokens, not free housekeeping) 6-15x more conservatively, and about
# 35x more often, than the model actually needed, which is the direct, measured cause of
# both "compaction happens more than it should" and "tokens burn faster than the VS Code
# extension" (which has no forced compaction at all and just lets the model's own real
# threshold govern). Also, this fires alongside today's separate reconnect-frequency
# fixes (the hang-ceiling reset, the WinError-206 system-prompt-file change) -- fewer
# involuntary reconnects is exactly the condition the original 0-observed-auto-compactions
# finding blamed for auto-compaction never getting a long enough uninterrupted stretch to
# fire on its own, so native auto-compaction firing reliably now is also the reason the
# margin below can be this narrow with the SAME confidence the original 60k had.
# 400k keeps a ~100k safety margin under the lowest real threshold observed (504,971) --
# compacts again well before context gets that large -- while cutting how often this
# fires by roughly the same 6-15x the threshold itself was too conservative by.
FORCED_COMPACTION_GROWTH_TOKENS_THRESHOLD = 400_000
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
# Fallback-compaction threshold (2026-10-17): native/forced compaction (the
# "/compact" sent above) is confirmed to sometimes achieve only a weak
# reduction (observed live: ~14%, once even negative) on a badly bloated
# tab -- see app/compaction.py's own module doc for the local, no-model
# second pass this triggers instead. Checked against tokens_at_last_forced_
# compaction, which compact_boundary_observed refreshes to the authoritative
# post-compaction count on EVERY compaction (native or forced) -- so this
# fires the tick right after a compaction's own boundary event, if and only
# if that compaction still left things this large. Deliberately reuses
# FORCED_COMPACTION_GROWTH_TOKENS_THRESHOLD's own already-vetted number
# rather than inventing a new one -- both describe the same "this is a lot
# of context" line, just checked against a different baseline.
FALLBACK_COMPACTION_THRESHOLD_TOKENS = FORCED_COMPACTION_GROWTH_TOKENS_THRESHOLD

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
    # Bug fix (2026-09-26), confirmed live: same shape as the auto-compaction preamble
    # above -- the underlying Claude Code CLI's own native `/loop` autonomous-check
    # feature resubmits a fixed English instructional prompt as a genuine role="user"
    # turn every time a scheduled wakeup fires, completely independent of Caroline's own
    # submit()/inject_proactive() (so _SYNTHETIC_TURN_MARKER never tags it either).
    # Confirmed live as the actual cause of a real incident: tab 1's last-5-real-user-
    # lines sample held two 7822-char copies of this English prompt against 185 total
    # characters of the user's own (furious) Russian messages, and the "majority
    # language" resolver correctly, faithfully picked English -- the sample itself was
    # the bug, not the resolver.
    #
    # Widened (2026-09-27), confirmed live, same tab, same day: this originally matched
    # only the literal heading "# /loop — autonomous default with dynamic pacing" (the
    # ScheduleWakeup/dynamic-pacing variant) -- a SECOND /loop variant fired with a
    # different heading, "# Autonomous loop check" (the CronCreate/autonomous-mode
    # variant), 5921 chars of English against 90 total chars of real Russian, same exact
    # failure. Rather than add a second literal string and keep chasing every future
    # heading wording one at a time, match the STRUCTURE instead: any top-level markdown
    # heading whose own line mentions both "autonomous" and "loop" -- true of every /loop
    # variant's own heading, by construction, regardless of exact phrasing, and not
    # something a real user's own first line would plausibly say by coincidence.
    re.compile(r"^#\s(?=.{0,80}\bautonomous\b)(?=.{0,80}\bloop\b).*$", re.IGNORECASE | re.MULTILINE),
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
_ATTACHMENT_NOTE_PREFIX_TUPLE = tuple(_ATTACHMENT_NOTE_PREFIXES) + (
    # Bridge only (2026-09-26), same convention as the "Reminder due" pair above: the
    # extracted-PDF-text note wasn't in history.py's list, and entries already on disk
    # from before history.ATTACHMENT_NOTE_MARKER existed carry no marker (confirmed live: a
    # 22K-char one sat in a Russian tab's language sample). New notes are covered
    # structurally by the marker; delete this once no such entry is recent enough to matter.
    "[Attached PDF",
)


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
    # Bug fix (2026-10-07), confirmed live as the actual cause of a real
    # incident: an all-Russian conversation got a forced-translation pass
    # that targeted ENGLISH, because current_language_name's underlying
    # resolve had flipped to English. Root cause -- _attachment_to_blocks
    # prepends ATTACHMENT_NOTE_MARKER to EVERY note it generates (see
    # history.py's own doc comment on that marker, "tags EVERY note block
    # ... at the single place that creates them"), but this check here
    # only ever looked for the PREFIX TEXT ("[This image is also saved
    # at ", ...) at the very start of `text` -- which, for every marked
    # note, actually starts with the marker's own invisible characters
    # FIRST, so startswith(_ATTACHMENT_NOTE_PREFIX_TUPLE) was always False
    # for anything written after the marker existed. Caroline's own
    # always-English auto-generated attachment note sailed straight into
    # the "last 5 real user lines" language-detection sample as if the
    # user had typed it. Strip the marker the same way history.py's own
    # _extract_attachment_note already does before checking the prefixes
    # -- the prefix tuple now only matters as the documented bridge for
    # old, pre-marker entries already on disk.
    text = text.removeprefix(ATTACHMENT_NOTE_MARKER)
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


def _strip_no_update_from_wire(wire: dict[str, Any], tab_id: str | None = None) -> dict[str, Any] | None:
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
        dropped = [
            b for b in content
            if isinstance(b, dict) and b.get("type") == "text"
            and isinstance(b.get("text"), str)
            and (_NO_UPDATE_SENTINEL in b["text"] or _is_silent_reply_paraphrase(b["text"]))
        ]
        if not dropped:
            return wire
        kept = [b for b in content if b not in dropped]
        # Per explicit instruction (2026-10-13): this strip had NO logging
        # at all -- a false-positive match (the sentinel/paraphrase check
        # catching real, substantive content) would silently drop it with
        # zero trace, while the model's own context still shows it as
        # having been said. Logged here, not just on the terminal "whole
        # message dropped" branch below, so a partial strip (one text
        # block removed alongside real tool_use/other blocks that still
        # go out) is just as visible.
        log_event(
            "engine", "no_update_stripped_from_wire", tab_id=tab_id,
            dropped_texts=[b.get("text") for b in dropped], remaining_blocks=len(kept),
        )
        if not any(isinstance(b, dict) and b.get("type") in ("text", "tool_use") for b in kept):
            log_event("engine", "wire_dropped_entirely_after_strip", tab_id=tab_id)
            return None  # nothing left the user should see
        wire["message"]["content"] = kept
        return wire
    if kind == "result":
        result_text = wire.get("result")
        if isinstance(result_text, str) and (_NO_UPDATE_SENTINEL in result_text or _is_silent_reply_paraphrase(result_text)):
            log_event("engine", "no_update_stripped_from_wire", tab_id=tab_id, dropped_texts=[result_text], remaining_blocks=0, kind="result")
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
                "text": f"{ATTACHMENT_NOTE_MARKER}[This image is also saved at {saved_path} -- use that path (e.g. to copy/move it somewhere "
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
                "text": f"{ATTACHMENT_NOTE_MARKER}[Attached PDF saved to {saved_path} -- could not extract its page text ({exc}); "
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
            f"{ATTACHMENT_NOTE_MARKER}[Attached PDF, {total_pages} page(s), also saved at {saved_path} -- parsed page-by-page, "
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
    return [{"type": "text", "text": f"{ATTACHMENT_NOTE_MARKER}[Attached file saved to {saved_path} -- read it if relevant to the request.]"}]


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


def _write_system_prompt_file(workspace_dir: str, tab_id: str, text: str) -> str:
    """Writes this tab's composed system-prompt append to a file the CLI reads via
    --append-system-prompt-file (see the call site for why it is not on the command line).
    Rewritten on every query() build -- the text carries per-query parts (language hint,
    continuity pointer). Written atomically (temp file + replace): a restart storm can start
    a new CLI while an older build of the file is still being read."""
    path = Path(workspace_dir) / f"system-prompt-{_sanitize_tab_id(tab_id)}.md"
    tmp = path.with_suffix(f".md.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)
    log_event("engine", "system_prompt_file_written", tab_id=tab_id, chars=len(text))
    return str(path)


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


def _ensure_small_model_session(workspace_dir: str, tab_id: str, session_id: str | None) -> str | None:
    """The session an sw turn belongs to, made to exist on disk: mints and
    saves a new id for a tab that has none, or starts the transcript file
    under an id the CLI issued but never wrote (a tab that connected in claude
    mode with no Claude login, or switched to sw right away -- seen live on
    the test box). Called at the start of an sw turn, since sw_history keys
    the turn's tool-call files by session id, and again when the turn is
    persisted. None if the file can't be created."""
    if not session_id:
        session_id = str(uuid_mod.uuid4())
        path = claude_project_dir(workspace_dir) / f"{session_id}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=False)
        except Exception as exc:
            log_event("engine", "small_model_session_mint_failed", tab_id=tab_id, error=str(exc))
            return None
        save_tab_session_id(workspace_dir, tab_id, session_id)
        log_event("engine", "small_model_session_minted", tab_id=tab_id, session_id=session_id)
        return session_id
    path = claude_project_dir(workspace_dir) / f"{session_id}.jsonl"
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
        except Exception as exc:
            log_event("engine", "small_model_turn_not_persisted_no_session_file", tab_id=tab_id, path=str(path), error=str(exc))
            return None
        log_event("engine", "small_model_session_file_started", tab_id=tab_id, session_id=session_id)
    return session_id


def _append_small_model_turn_to_session(workspace_dir: str, tab_id: str, session_id: str | None, user_text: str, assistant_text: str) -> str | None:
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

    A tab that has never run an SDK turn has no session yet. It used to be
    left unpersisted, which (seen with the sw-mode test harness, 2026-10-22)
    left a Claude-less tab with no history at all: the small model could not
    see its own previous turn ("remember 4711" -> "which number?" took four
    minutes and ended with "I didn't remember any number"), and language
    detection, which reads the same history, never ran, so replies came back
    in English. Per explicit instruction, such a tab now gets its own minted
    session id, with the file created in Claude Code's own project dir so a
    later switch to Claude mode can try to resume it; the entries carry the
    CLI's cwd/isSidechain/userType fields for that reason.

    Returns the session id the turn was written to (None if it wasn't), so
    the caller can adopt a newly minted one."""
    session_id = _ensure_small_model_session(workspace_dir, tab_id, session_id)
    if session_id is None:
        return None
    path = claude_project_dir(workspace_dir) / f"{session_id}.jsonl"
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
        cli_fields = {"cwd": workspace_dir, "isSidechain": False, "userType": "external"}
        user_entry = {
            "type": "user", "uuid": user_uuid, "parentUuid": last_uuid,
            "timestamp": now_iso, "sessionId": session_id, **cli_fields,
            "message": {"role": "user", "content": user_text},
        }
        assistant_entry = {
            "type": "assistant", "uuid": assistant_uuid, "parentUuid": user_uuid,
            "timestamp": now_iso, "sessionId": session_id, **cli_fields,
            "message": {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]},
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(user_entry, ensure_ascii=False) + "\n")
            f.write(json.dumps(assistant_entry, ensure_ascii=False) + "\n")
        log_event("engine", "small_model_turn_persisted", tab_id=tab_id, session_id=session_id)
        return session_id
    except Exception as exc:
        log_event("engine", "small_model_turn_persist_failed", tab_id=tab_id, error=str(exc))
        return None


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
    # Added 2026-10-17 for the progress narrator (see _narration_action_facts'
    # own comment) -- same json.dumps(input)[:120] preview small_model_engine.py's
    # emit() already uses for its own "Tool call X (arguments: ...)" activity
    # lines, so both paths feed the narrator prompt the exact shape its few-shot
    # examples were tuned against. Optional/defaulted: every OTHER reader of
    # ToolOutcome (the recovery-nudge summary) only ever needed name/is_error/
    # result_preview, so this stays additive rather than touching their call sites.
    arguments_preview: str = ""


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

__all__ = [
    'annotations',
    'asyncio',
    'base64',
    'json',
    'os',
    're',
    'subprocess',
    'sys',
    'threading',
    'time',
    'uuid_mod',
    'datetime',
    'timezone',
    'Path',
    'dataclass',
    'Any',
    'Awaitable',
    'Callable',
    'AssistantMessage',
    'ClaudeAgentOptions',
    'HookMatcher',
    'RateLimitEvent',
    'ResultMessage',
    'SystemMessage',
    'TaskNotificationMessage',
    'TaskStartedMessage',
    'TaskUpdatedMessage',
    'TERMINAL_TASK_STATUSES',
    'TextBlock',
    'ToolResultBlock',
    'ToolUseBlock',
    'UserMessage',
    'win_subprocess_patch',
    'chunk_archive',
    '_sanitize_tab_id',
    'claude_project_dir',
    'clear_pending_api_retry',
    'clear_pending_one_shot_followup',
    'clear_pending_turn',
    'clear_tab_continuity_archive',
    'clear_tab_session_id',
    'compute_foreign_tool_overlap',
    'dehydrated_dir',
    'find_most_recent_claude_session_id',
    'has_chat_mode',
    'load_chat_mode',
    'save_chat_mode',
    'chat_mode_eligible',
    'load_discovered_foreign_tool_overlap',
    'load_tab_continuity_archive',
    'load_tab_session_id',
    'save_discovered_foreign_tool_overlap',
    'save_pending_api_retry',
    'save_pending_one_shot_followup',
    'save_pending_turn',
    'save_tab_continuity_archive',
    'save_tab_session_id',
    'session_transcript_path',
    'openai_transcripts_dir',
    'compact_session_if_due',
    'AgentEngine',
    'ClaudeEngine',
    'CodexEngine',
    'build_codex_options',
    'openai_available',
    'rotate_transcript',
    'ATTACHMENT_NOTE_MARKER',
    '_ATTACHMENT_NOTE_PREFIXES',
    '_HISTORY_STAMP_PATTERN',
    'iter_entries_reversed',
    'iter_lines_reversed',
    'read_last_context_tokens',
    'CC_CLI_LIMIT_PATTERN',
    'CLASSIFIER_REFUSAL_PATTERN',
    'NOT_LOGGED_IN_PATTERN',
    'OVERSIZED_MESSAGE_PATTERN',
    'PROMPT_TOO_LONG_PATTERN',
    'SESSION_NOT_FOUND_PATTERN',
    'TOOL_CONCURRENCY_ERROR_PATTERN',
    'detect_balance_exhaustion',
    'extract_classifier_refusal_category',
    'log_event',
    'is_logged_in',
    'build_mcp_servers',
    'CASCADE_SKIP_TRANSLATION_WHEN_IN_LANGUAGE',
    'answer_is_in_language',
    'draft_narration_remark',
    'run_small_model_turn',
    'try_fast_answer',
    'supervise',
    'get_persona',
    'get_persona_gender',
    'persona_system_prompt_append',
    'get_owner_profile',
    'owner_profile_system_prompt_clause',
    'load_working_memory',
    'working_memory_system_prompt_clause',
    'memory_topics_system_prompt_clause',
    'agent_registry',
    'pseudo_tab',
    'caroline_agents',
    'ALWAYS_ON_INSTRUCTIONS',
    'continuity_pointer_instruction',
    'language_hint_instruction',
    'recent_dialogue_history_instruction',
    'running_agents_pointer_instruction',
    'pseudo_tab_pointer_instruction',
    'REGISTRY',
    'extract_pdf_page_texts',
    'ProcessActivityMonitor',
    'memory_nudge',
    'get_recent_real_user_directives',
    'register_inject_proactive',
    'register_permission_blocker',
    'register_recent_real_user_directives',
    'register_reply_watch',
    'register_send_action_guard',
    'register_file_transfer_guard',
    'register_send_for_tab',
    'set_cli_pid_sink',
    'set_inject_proactive',
    'set_send',
    'set_tab_id',
    'get_funds_exhausted_reason',
    'mark_funds_exhausted',
    'memory_turns',
    'kill_process_tree',
    'require_sw_or_prompt',
    'build_options_env',
    'get_model_override',
    'resolve_mode',
    'message_to_wire',
    'WORKSPACE_DIR',
    'SendFn',
    'PRIMARY_TAB_ID',
    'HANG_TIMEOUT_MS',
    'STARTUP_TIMEOUT_MS',
    'WATCHDOG_INTERVAL_MS',
    'HANG_ESCALATION_GRACE_MS',
    'HANG_TIMEOUT_WITH_TOOL_IN_FLIGHT_MS',
    'HANG_ABSOLUTE_CEILING_MS',
    'STOP_ESCALATION_DISCONNECT_TIMEOUT_S',
    'MAX_RESTARTS_PER_WINDOW',
    'RESTART_WINDOW_MS',
    'RESTART_BACKOFF_MS',
    'API_RETRY_INTERVAL_MS',
    'STOP_RESETS_CONN_STATES',
    'STOP_KEEPS_TOOLS',
    'STOP_NOTE_TEXT',
    'PERMISSION_NOTE_TEXT_TEMPLATE',
    'ONE_SHOT_FOLLOWUP_CHECK_DELAY_MS',
    'MCP_RECONNECT_INTERVAL_MS',
    'SILENT_USER_WAIT_NUDGE_MS',
    'PROGRESS_NARRATION_INTERVAL_MS',
    '_INTERIM_NOTES_HEADER',
    'MAX_CONSECUTIVE_NARRATION_COMMENTS',
    'NARRATION_GENERATION_RETRY_ATTEMPTS',
    'NARRATION_NETWORK_TIMEOUT_S',
    'NARRATION_FAILURE_RETRY_S',
    'VOICE_NARRATION_TICK_S',
    'VOICE_NARRATION_STALE_REPEAT_WINDOW',
    'VOICE_NARRATION_REVIEW',
    'VOICE_NARRATION_REVIEW_TIMEOUT_S',
    'VOICE_NARRATION_FIRST_S',
    'VOICE_NARRATION_PREPARE_FROM_S',
    'VOICE_NARRATION_AFTER_SPEECH_GAP_S',
    'VOICE_NARRATION_STALE_S',
    'VOICE_NARRATION_STALE_LEAD_S',
    'VOICE_NARRATION_STALE_STEP_S',
    '_spoken_duration',
    'VOICE_NARRATION_STALE_MAX_S',
    'VOICE_NARRATION_ANSWER_HOLD_S',
    'VOICE_NARRATION_SAY_ELSE',
    'VOICE_NARRATION_MAX_AGE_S',
    'VOICE_NARRATION_FAILURE_RETRY_S',
    'VOICE_NARRATION_EST_CHARS_PER_S',
    'VOICE_NARRATION_CLIENT_SPEAKING_STALE_S',
    'RECENT_DIALOGUE_WINDOW',
    'SW_HISTORY_WAIT_S',
    'sw_history',
    'PERMISSION_GUARD_DIALOGUE_WINDOW',
    'REPLY_WATCH_SCHEDULE',
    'POST_TURN_CHECK_MAX_ROUNDS',
    'LANGUAGE_DETECTION_USER_LINE_COUNT',
    'RECENT_HISTORY_FILE_WINDOW_HOURS',
    'RECENT_24H_REFRESH_MIN_AGE_S',
    'RESUMED_ANSWER_MAX_NUDGES',
    'ENGINE_ERROR_SUPPRESSED_KINDS',
    'RECENT_24H_REFRESH_TIMEOUT_S',
    '_RECENT_24H_REFRESH_SLOT',
    'FORCED_COMPACTION_HOURLY_MS',
    'COMPACTION_STUCK_TIMEOUT_S',
    'USER_MESSAGE_MERGE_WINDOW_S',
    'FAST_LANE_DURING_MERGE_WINDOW',
    'MEMORY_FLUSH_BEFORE_COMPACTION_TEXT',
    'FORCED_COMPACTION_GROWTH_TOKENS_THRESHOLD',
    'FORCED_COMPACTION_MIN_INTERVAL_MS',
    'FORCED_COMPACTION_LIMIT_PAUSE_MS',
    'FORCED_COMPACTION_MIN_CONTEXT_TOKENS',
    'FALLBACK_COMPACTION_THRESHOLD_TOKENS',
    'AUTH_RETRY_ESCALATION_THRESHOLD',
    'CONTINUE_OR_SILENT_NUDGE_TEMPLATE',
    'OPENAI_TOOL_HONESTY_INSTRUCTION',
    'STARTUP_GREETING_NUDGE_TEMPLATE',
    'BALANCE_EXHAUSTED_MESSAGE',
    '_SYNTHETIC_HISTORY_TEXT_PATTERNS',
    '_ATTACHMENT_NOTE_PREFIX_TUPLE',
    '_SYNTHETIC_TURN_MARKER',
    '_INLINE_HOUR_CONTEXT_BLOCK_PATTERN',
    '_is_synthetic_history_text',
    '_NO_UPDATE_SENTINEL',
    '_SILENT_REPLY_PARAPHRASE_PATTERNS',
    '_is_silent_reply_paraphrase',
    '_strip_no_update_from_wire',
    '_last_language_path',
    '_load_persisted_language',
    '_save_persisted_language',
    '_SUPPORTED_IMAGE_TYPES',
    '_ATTACHMENT_PDF_INLINE_CHAR_BUDGET',
    '_uploads_dir',
    '_save_attachment_to_uploads',
    '_attachment_to_blocks',
    '_usable_dialogue_lines',
    '_recent_usable_lines',
    '_recent_usable_lines_across_sessions',
    '_read_recent_dialogue_lines',
    '_read_recent_user_lines',
    '_gather_recent_usable_lines',
    '_recent_24h_dialogue_path',
    '_write_system_prompt_file',
    '_write_recent_24h_dialogue_file',
    '_append_small_model_turn_to_session',
    '_ensure_small_model_session',
    'current_language_name',
    'refresh_language_in_background',
    '_SETTINGS_FILE_NAME',
    '_SETTINGS_FILE_CONTENT',
    'clear_tab_disk_state',
    '_ensure_settings_file',
    'ToolOutcome',
    '_format_exception_chain',
    '_format_timestamp_for_model',
    '_uuid',
    'Action',
    'ActionRegistry',
    'load_action_state',
    'save_action_state',
    'clear_action_state',
    'register_action_closer',
    'get_action_closer',
    'register_sessions_getter',
    'get_sessions_getter',
]



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
