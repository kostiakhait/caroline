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
import re
import subprocess
import time
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
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
    dehydrated_dir,
    find_most_recent_claude_session_id,
    load_chat_mode,
    load_tab_continuity_archive,
    load_tab_session_id,
    save_pending_turn,
    save_tab_continuity_archive,
    save_tab_session_id,
)
from app.history import _extract_entries_from_jsonl, _HISTORY_STAMP_PATTERN, read_archived_entries
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
from app.persona import get_persona, persona_system_prompt_append
from app.policies import ALWAYS_ON_INSTRUCTIONS, continuity_pointer_instruction, language_hint_instruction, recent_dialogue_history_instruction
from app.operations import REGISTRY
from app.session_context import set_inject_proactive, set_send, set_tab_id
from app.sw_gate import require_sw_or_prompt
from app.subscription_mode import (
    build_options_env,
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
FORCED_COMPACTION_GROWTH_BYTES_THRESHOLD = 100_000
# Minimum gap between two forced compactions on the same tab, regardless of
# which trigger fires -- keeps the three triggers from stacking (e.g. the
# hourly clock and the growth threshold both crossing within the same
# watchdog tick) into back-to-back /compact calls.
FORCED_COMPACTION_MIN_INTERVAL_MS = 300_000

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
    return any(p.match(text) for p in _SYNTHETIC_HISTORY_TEXT_PATTERNS)


_NO_UPDATE_SENTINEL = "[[NO_UPDATE]]"


def _strip_no_update_from_wire(wire: dict[str, Any]) -> dict[str, Any] | None:
    """Per explicit instruction (2026-09-10): NOTHING containing the
    [[NO_UPDATE]] sentinel (see no_update_sentinel_instruction /
    CONTINUE_OR_SILENT_NUDGE_TEMPLATE) may ever reach the user-visible
    dialog -- filter it out server-side here, not only in chat.js, so an
    old/cached client can't leak it either. Substring match, not exact
    equality: the model doesn't always reply with ONLY the sentinel.
    Returns the wire with offending text blocks removed, or None if that
    empties an assistant message of everything worth showing."""
    kind = wire.get("type")
    if kind == "assistant":
        content = wire.get("message", {}).get("content", [])
        kept = [
            b for b in content
            if not (isinstance(b, dict) and b.get("type") == "text"
                    and isinstance(b.get("text"), str) and _NO_UPDATE_SENTINEL in b["text"])
        ]
        if len(kept) == len(content):
            return wire
        if not any(isinstance(b, dict) and b.get("type") in ("text", "tool_use") for b in kept):
            return None  # nothing left the user should see
        wire["message"]["content"] = kept
        return wire
    if kind == "result":
        result_text = wire.get("result")
        if isinstance(result_text, str) and _NO_UPDATE_SENTINEL in result_text:
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
        return [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": attachment["dataBase64"]}},
            {"type": "text", "text": f"[This document is also saved at {saved_path}.]"},
        ]
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
        clean_text = _HISTORY_STAMP_PATTERN.sub("", raw_text).strip()
        if not clean_text or _is_synthetic_history_text(raw_text):
            continue
        if clean_text.startswith("[") and clean_text.endswith("]") and "\n" not in clean_text:
            continue
        speaker = "User" if entry.get("role") == "user" else "Caroline"
        out.append(f"{speaker}: {clean_text}")
    return out


def _read_recent_dialogue_lines(session_id: str | None, tab_id: str, workspace_dir: str, limit: int) -> list[str]:
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
    too thin, e.g. right after a native auto-compaction."""
    lines: list[str] = []
    if session_id:
        path = claude_project_dir(workspace_dir) / f"{session_id}.jsonl"
        try:
            lines = _usable_dialogue_lines(_extract_entries_from_jsonl(path.read_text(encoding="utf-8"), str(path)))
        except Exception as exc:
            log_event("engine", "recent_dialogue_read_failed", tab_id=tab_id, error=str(exc))
    if len(lines) < limit:
        archive_path = load_tab_continuity_archive(workspace_dir, tab_id)
        if archive_path:
            try:
                lines = _usable_dialogue_lines(read_archived_entries(archive_path)) + lines
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

    def _user_only(lines: list[str]) -> list[str]:
        return [line[len("User: "):] for line in lines if line.startswith("User: ")]

    user_lines: list[str] = []
    if session_id:
        path = claude_project_dir(workspace_dir) / f"{session_id}.jsonl"
        try:
            user_lines = _user_only(_usable_dialogue_lines(_extract_entries_from_jsonl(path.read_text(encoding="utf-8"), str(path))))
        except Exception as exc:
            log_event("engine", "recent_user_lines_read_failed", tab_id=tab_id, error=str(exc))
    if len(user_lines) < count:
        archive_path = load_tab_continuity_archive(workspace_dir, tab_id)
        if archive_path:
            try:
                user_lines = _user_only(_usable_dialogue_lines(read_archived_entries(archive_path))) + user_lines
            except Exception as exc:
                log_event("engine", "recent_user_lines_archive_read_failed", tab_id=tab_id, path=archive_path, error=str(exc))
    return user_lines[-count:]


def _recent_24h_dialogue_path(workspace_dir: str, tab_id: str) -> Path:
    return Path(workspace_dir) / f"recent-24h-dialogue-{_sanitize_tab_id(tab_id)}.txt"


def _write_recent_24h_dialogue_file(session_id: str | None, tab_id: str, workspace_dir: str) -> str:
    """Per explicit instruction (2026-09-14): see recent_dialogue_history_
    instruction's own docstring (policies.py) for the full feature this
    backs. Refreshed synchronously (plain local file I/O, no network call
    -- unlike refresh_language_in_background, this can't be fire-and-forget
    since the whole point is that it's current by the time THIS turn's
    system prompt gets built) from submit() on every real user turn.
    Gathers real dialogue (_usable_dialogue_lines -- both speakers, real
    content, synthetic/service text already dropped) from the last
    RECENT_HISTORY_FILE_WINDOW_HOURS, from the same two sources
    _read_recent_dialogue_lines/_read_recent_user_lines already draw from
    (the live session file, plus this tab's own continuity archive for
    anything a compaction already aged out of the live file within the
    window). Returns the file's own path unconditionally (even on a
    read/write failure -- an empty or stale file is still a valid, if
    unhelpful, thing to point the model at; a missing return value would
    just make the pointer instruction silently vanish instead)."""
    out_path = _recent_24h_dialogue_path(workspace_dir, tab_id)
    cutoff_ms = (time.time() - RECENT_HISTORY_FILE_WINDOW_HOURS * 3600) * 1000
    lines: list[str] = []
    if session_id:
        path = claude_project_dir(workspace_dir) / f"{session_id}.jsonl"
        try:
            lines = _usable_dialogue_lines(_extract_entries_from_jsonl(path.read_text(encoding="utf-8"), str(path)), min_ts_ms=cutoff_ms)
        except Exception as exc:
            log_event("engine", "recent_24h_dialogue_read_failed", tab_id=tab_id, error=str(exc))
    archive_path = load_tab_continuity_archive(workspace_dir, tab_id)
    if archive_path:
        try:
            lines = _usable_dialogue_lines(read_archived_entries(archive_path), min_ts_ms=cutoff_ms) + lines
        except Exception as exc:
            log_event("engine", "recent_24h_dialogue_archive_read_failed", tab_id=tab_id, path=archive_path, error=str(exc))
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(lines) if lines else "(No real messages between you and this user in the last 24 hours.)"
        out_path.write_text(body, encoding="utf-8")
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
        existing = path.read_text(encoding="utf-8")
        for line in reversed(existing.rstrip("\n").split("\n")):
            if not line.strip():
                continue
            try:
                last_uuid = json.loads(line).get("uuid")
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

# Hard block (2026-09-14), per explicit instruction: "caroline-browser" is a
# leftover from the old Node backend (workspace.ts's ensureWorkspace()) --
# a real, separate Playwright/Node browser process, still registered as a
# user-scope MCP server in ~/.claude.json (never removed during the Python
# rewrite), so the `claude` CLI subprocess this class spawns picks it up
# automatically regardless of what this backend's own mcp_servers dict
# contains. prefer_embedded_browser_instruction (policies.py) already tells
# the model to prefer open_app_browser/app_browser_* over this -- promoted
# to ALWAYS_ON_INSTRUCTIONS on 2026-09-11 after a single sentence in the
# tool description alone wasn't enough -- and confirmed live (2026-09-13)
# that even ALWAYS_ON prompting still isn't a strong enough guarantee: the
# model kept reaching for caroline-browser anyway, opening a real, separate
# Chrome/Chromium window instead of Caroline's own embedded one. A prompt
# is advisory; disallowed_tools is enforced by the SDK/CLI itself and
# cannot be talked around, so exclude these outright rather than continue
# to just ask nicely. Exact tool names (no wildcard support confirmed for
# this SDK's disallowed_tools), one per tool this external server exposes.
_DISALLOWED_CAROLINE_BROWSER_TOOLS = [
    f"mcp__caroline-browser__{name}"
    for name in (
        "browser_click", "browser_evaluate", "browser_file_upload", "browser_find",
        "browser_navigate", "browser_press_key", "browser_resize", "browser_restart_daemon",
        "browser_run_code_unsafe", "browser_snapshot", "browser_tabs",
        "browser_take_screenshot", "browser_type", "browser_wait_for",
    )
]


def _ensure_settings_file(workspace_dir: str) -> str:
    path = Path(workspace_dir) / _SETTINGS_FILE_NAME
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != _SETTINGS_FILE_CONTENT:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_SETTINGS_FILE_CONTENT, encoding="utf-8")
    except Exception as exc:
        log_event("engine", "settings_file_write_failed", error=str(exc))
    return str(path)


class ChatSession:
    def __init__(self, tab_id: str, workspace_dir: str, send: SendFn) -> None:
        self.tab_id = tab_id
        self.workspace_dir = workspace_dir
        self.send = send

        self.client: ClaudeSDKClient | None = None
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
        # Same "this ResultMessage/whatever precedes it isn't real, don't
        # show it or let it touch turn state" shape as
        # hang_interrupt_result_pending/ignore_next_result_recovery -- see
        # result_is_fake's own computation in the message loop.
        self.forced_compaction_result_pending: bool = False

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

        # silent user-wait nudge (see SILENT_USER_WAIT_NUDGE_MS) -- tracks
        # only REAL user-typed messages (submit()'s is_real_user=True),
        # not proactive/reminder/retry turns
        self.last_real_user_turn_at: float | None = None
        self.real_user_turn_answered = False
        self.silence_nudge_sent_for_turn = False

        # periodic mid-turn progress narration (see PROGRESS_NARRATION_INTERVAL_MS)
        # -- when a real user's own question was last actually shown something
        # (a real reply OR a generated stand-in progress comment), and what
        # that question was, so a comment (if generated) can tie back to it.
        self.last_visible_output_at: float | None = None
        self.last_real_user_question: str | None = None
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
        # Bug fix (2026-09-14), same incident: _schedule_one_shot_followup_
        # check's own "Deliberately no rescheduling here" guarantee only
        # holds within ONE firing -- if the follow-up turn it injects ALSO
        # concludes by reporting the same still-active usage cap, the CC-
        # CLI-limit-message handler re-enters _schedule_one_shot_followup_
        # check fresh (self.one_shot_followup_timer is None again, cleared
        # at the top of the PREVIOUS firing) and re-arms, unboundedly, for
        # as long as the cap stays active -- confirmed live, this is
        # exactly how a single genuine cap hit turned into a self-
        # perpetuating retry loop. True one-shot PER cap episode now: set
        # once a follow-up is scheduled, only cleared by a genuine new real
        # user message (submit()'s is_real_user branch) -- not by the
        # follow-up firing itself.
        self.one_shot_followup_used_for_limit = False

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
        self.restart_for_unrecoverable_session = False
        self.unrecoverable_session_replay_text: str | None = None
        self.unrecoverable_session_replay_attachments: list[Any] = []
        self.skip_migration_fallback_once = False

        # conn state / api retry / rate limit memory
        self.conn_state: dict[str, Any] = {"kind": "connected"}
        self.ignore_next_result_recovery = False
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

    async def _safe_interrupt(self) -> None:
        try:
            if self.client:
                await self.client.interrupt()
        except Exception as exc:
            log_event("engine", "dispose_interrupt_failed", tab_id=self.tab_id, error=str(exc))

    async def _safe_disconnect(self, client: ClaudeSDKClient) -> None:
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
        # Every one of the ~9 places in this file that flips turn_pending
        # now republishes status automatically -- no call site has to
        # remember to do it itself (that "remember to do it everywhere"
        # pattern is exactly what produced tonight's whole run of bugs).
        asyncio.create_task(self._publish_status())

    def _compute_public_status(self) -> tuple[str, str]:
        kind = self.conn_state.get("kind")
        reason = self.conn_state.get("reason") or ""
        if kind in ("billing_blocked", "not_logged_in"):
            return "error", reason
        if kind in ("restarting", "restart_backoff", "limited"):
            return "recovering", reason
        if self.turn_pending:
            return "working", ""
        return "ready", ""

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

    def submit(self, text: str, attachments: list[Any] | None = None, is_real_user: bool = True, is_voice: bool = False) -> None:
        # Per standing instruction ("ВЕЗДЕ логируем и ВСЁ"): this is the
        # single choke point EVERY turn goes through -- real user messages,
        # every proactive/internal nudge (inject_proactive already logs
        # its own text_len separately, but not is_real_user/is_voice), and
        # every replay -- confirmed live tonight this had NO logging of
        # its own at all, unlike inject_proactive.
        attachments = attachments or []
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
        self.pending_is_real_user = is_real_user
        self.pending_attachments = attachments
        self.turn_pending = True
        self.last_activity = time.monotonic()
        if is_real_user:
            self.last_user_activity = time.monotonic()
            self.last_real_user_turn_at = time.monotonic()
            self.real_user_turn_answered = False
            self.silence_nudge_sent_for_turn = False
            self.last_visible_output_at = time.monotonic()
            # Bug fix (2026-09-14): a genuine new real message is the one
            # thing that should give both of these a clean slate -- see
            # their own __init__ comments (consecutive_narration_count,
            # one_shot_followup_used_for_limit) for the incident this fixes.
            self.consecutive_narration_count = 0
            self.one_shot_followup_used_for_limit = False
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
            # Per explicit instruction (2026-09-14): synchronous (plain
            # local file I/O, not a network call) so this tab's 24h-dialogue
            # file is genuinely current before this same turn dispatches --
            # see _write_recent_24h_dialogue_file's own docstring.
            self._recent_24h_dialogue_file_path = _write_recent_24h_dialogue_file(self.last_saved_session_id, self.tab_id, self.workspace_dir)
        save_pending_turn(self.workspace_dir, self.tab_id, text, attachments)
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
        self.real_user_turn_answered = False
        self.silence_nudge_sent_for_turn = False
        self.last_visible_output_at = time.monotonic()
        self.consecutive_narration_count = 0
        self.one_shot_followup_used_for_limit = False
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
            translated = await translate_text(text, current_language_name(self.tab_id))
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

    def inject_proactive(self, text: str, attachments: list[Any] | None = None, is_voice: bool = False) -> bool:
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
        self.submit(text, attachments, False, is_voice)
        return True

    def stop(self) -> None:
        if not self.turn_pending:
            return
        log_event("engine", "user_stop", tab_id=self.tab_id)
        self.user_stop_requested = True
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
        genuinely cannot fail to land."""
        if self.client:
            asyncio.create_task(self._safe_interrupt())
        log_event("engine", "user_stop_force_close", tab_id=self.tab_id)
        try:
            if self.client:
                await asyncio.wait_for(self.client.disconnect(), timeout=STOP_ESCALATION_DISCONNECT_TIMEOUT_S)
        except Exception as exc:
            log_event("engine", "user_stop_close_failed", tab_id=self.tab_id, error=str(exc))
            self._force_kill_underlying_cli_process()

    def _force_kill_underlying_cli_process(self) -> None:
        """Per explicit instruction (2026-09-15): mirrors BackendProcess.
        cs's own taskkill fallback for the identical class of problem (a
        graceful kill that doesn't reliably land) -- see
        _escalate_stop_if_still_pending's own comment for the incident.
        Reaches into claude_agent_sdk's private transport/process
        attributes for the real OS PID and kills it directly, bypassing
        whatever broken internal state made client.disconnect() itself
        throw. Best-effort and silent on failure -- the private attribute
        chain can legitimately not exist (a different SDK version, a
        custom transport), and this is already the last-resort branch of a
        last-resort escalation; there is nothing further to fall back to
        here."""
        transport = getattr(self.client, "_transport", None)
        process = getattr(transport, "_process", None)
        pid = getattr(process, "pid", None)
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
        or an in-flight rate-limit rejection -- see _schedule_api_retry,
        which correctly keeps retrying every 90s forever for THAT case,
        since nothing costs anything until a request actually gets
        through) from "the operation genuinely completed" (a real
        AssistantMessage came through -- e.g. the CC CLI's own "you've hit
        your session limit" reply -- the turn is over, just concluded by
        reporting a cap). For the second case, _schedule_api_retry's
        forever-retry is wrong: confirmed live, it kept firing every 90s
        for HOURS after a tab's real task was already finished, because it
        couldn't tell "completed" from "never ran" apart. This is the
        correct shape for "completed" instead: ONE follow-up check, fired
        once, that never re-arms itself no matter what the model replies
        (a genuine [[NO_UPDATE]], a real answer, or nothing at all) --
        unlike api_retry_timer, which reschedules itself every time it
        fires until something clears it.

        Bug fix (2026-09-14), confirmed live: "never re-arms itself" above
        was only ever true WITHIN one firing (the timer handle itself is
        cleared at the top of _fire(), before anything else happens) -- if
        the follow-up turn it injects ALSO concludes by reporting the SAME
        still-active usage cap, the CC-CLI-limit-message handler calls
        this method again fresh, sees one_shot_followup_timer is None
        (already cleared), and re-arms -- unboundedly, once per
        ONE_SHOT_FOLLOWUP_CHECK_DELAY_MS, for as long as the cap stays
        active. That's exactly the forever-retry shape this function's own
        docstring says it's deliberately NOT supposed to have. See
        one_shot_followup_used_for_limit's own __init__ comment: true
        one-shot per CAP EPISODE now, not per firing -- only a genuine new
        real user message clears it for another try."""
        if self.one_shot_followup_timer:
            log_event("engine", "one_shot_followup_already_pending", tab_id=self.tab_id, reason=reason)
            return
        if self.one_shot_followup_used_for_limit:
            log_event("engine", "one_shot_followup_already_used_for_this_limit_episode", tab_id=self.tab_id, reason=reason)
            return
        self.one_shot_followup_used_for_limit = True
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
                f"[Internal: one-time follow-up check -- your previous turn concluded by reporting a usage cap.] "
                f"{CONTINUE_OR_SILENT_NUDGE_TEMPLATE.format(language=current_language_name(self.tab_id))}",
                is_voice,
            )
            # Deliberately no rescheduling here -- this is the whole point
            # (one_shot_followup_used_for_limit, set above, is what actually
            # enforces that now, since this timer handle alone wasn't enough).

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

    def _schedule_mcp_reconnect(self, client: ClaudeSDKClient, name: str, attempt: int = 0) -> None:
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

    async def _do_mcp_reconnect(self, client: ClaudeSDKClient, name: str, attempt: int) -> None:
        try:
            await client.reconnect_mcp_server(name)
            log_event("engine", "mcp_server_reconnected", tab_id=self.tab_id, server=name, attempt=attempt)
        except Exception as exc:
            log_event("engine", "mcp_server_reconnect_failed", tab_id=self.tab_id, server=name, attempt=attempt, retry_in_ms=MCP_RECONNECT_INTERVAL_MS, error=str(exc))
            self._schedule_mcp_reconnect(client, name, attempt + 1)

    def _resolve_resume_session_id(self) -> str | None:
        stored = load_tab_session_id(self.workspace_dir, self.tab_id)
        if stored:
            return stored
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

    async def _pre_compact_hook(self, hook_input: Any, tool_use_id: Any, context: Any) -> dict[str, Any]:
        """Fires just before Claude's own compaction summarises older turns
        -- either its own native auto-compaction, or our forced "/compact"
        (see FORCED_COMPACTION_HOURLY_MS; trigger is "manual" for that one,
        live-confirmed). Copies the current transcript to
        workspace/dehydrated/ and points the continuity pointer at it, so
        if the summary ever drops something the user asks about, Caroline
        has a real file to Read (main.py's expand_dehydrated_ref serves it
        back). Best-effort; never blocks or fails compaction."""
        try:
            trigger = hook_input.get("trigger") if isinstance(hook_input, dict) else None
            transcript_path = hook_input.get("transcript_path") if isinstance(hook_input, dict) else None
            if trigger not in ("auto", "manual") or not transcript_path or not Path(transcript_path).exists():
                return {}
            import shutil
            directory = dehydrated_dir(self.workspace_dir)
            directory.mkdir(parents=True, exist_ok=True)
            archive_path = str(directory / f"{uuid_mod.uuid4()}.txt")
            shutil.copyfile(transcript_path, archive_path)
            save_tab_continuity_archive(self.workspace_dir, self.tab_id, archive_path)
            log_event("engine", "pre_compact_archived", tab_id=self.tab_id, archive_path=archive_path, trigger=trigger)
        except Exception as exc:
            log_event("engine", "pre_compact_hook_failed", tab_id=self.tab_id, error=str(exc))
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

        lines = _read_recent_dialogue_lines(self.last_saved_session_id, self.tab_id, self.workspace_dir, limit)

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
                translated = await translate_text(text, language)
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
        self.last_visible_output_at = now  # claim this tick immediately -- a slow ai:resolve call must not let a second tick double-fire
        from app.plugins.voice_api import generate_progress_comment

        dialogue = self._gather_recent_dialogue_for_narration()
        if not dialogue.strip():
            # Nothing to narrate about yet (first-ever turn, nothing on disk,
            # no pending text either) -- skip rather than call ai:resolve with
            # empty context for a comment that couldn't mean anything.
            return
        log_event("engine", "progress_narration_context", tab_id=self.tab_id, dialogue_chars=len(dialogue), dialogue_preview=dialogue[-300:])
        try:
            comment = await generate_progress_comment(dialogue, current_language_name(self.tab_id))
        except Exception as exc:
            log_event("engine", "progress_narration_failed", tab_id=self.tab_id, error=str(exc))
            return
        if not comment:
            return
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
        chain into firing the proactive-empty branch above again on itself
        -- fires at most once per originating turn, never an infinite loop
        of self-nudges even if the reply is ALSO empty.

        Still asks the model directly rather than trusting our own
        bookkeeping (confirmed live that bookkeeping alone can be wrong --
        see hang_interrupt_result_pending's own comment) -- just event-
        driven now instead of polling on a clock."""
        if self.ended:
            return
        lang = current_language_name(self.tab_id)
        log_event("engine", "post_turn_completion_check", tab_id=self.tab_id)
        self._awaiting_post_turn_check_reply = True
        self.inject_proactive(CONTINUE_OR_SILENT_NUDGE_TEMPLATE.format(language=lang))

    def _current_session_file_size(self) -> int | None:
        """Bytes on disk for this tab's currently-resumed session transcript,
        or None if there's nothing resolvable yet (no session id, or the
        file genuinely isn't there). Used by _check_forced_compaction's
        growth trigger -- see FORCED_COMPACTION_GROWTH_BYTES_THRESHOLD."""
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
        own). Three triggers, checked in priority order: a pending startup
        compaction (main.py sets needs_startup_compaction once per tab per
        process), the hourly clock, or on-disk growth past
        FORCED_COMPACTION_GROWTH_BYTES_THRESHOLD since the last forced
        compaction. Never runs while a real turn is in flight or the
        connection isn't fully settled -- this is maintenance, not
        something to inject into or race with actual work."""
        if self.ended or self.turn_pending or self.forced_compaction_result_pending:
            return
        if self.conn_state.get("kind") != "connected":
            return
        if not self.last_saved_session_id:
            return
        now = time.monotonic()
        if self.last_forced_compaction_at is not None and now - self.last_forced_compaction_at < FORCED_COMPACTION_MIN_INTERVAL_MS / 1000:
            return

        reason: str | None = None
        if self.needs_startup_compaction:
            reason = "startup"
        elif self.last_forced_compaction_at is None or now - self.last_forced_compaction_at >= FORCED_COMPACTION_HOURLY_MS / 1000:
            reason = "hourly"
        else:
            size = self._current_session_file_size()
            baseline = self.size_at_last_forced_compaction or 0
            if size is not None and size - baseline >= FORCED_COMPACTION_GROWTH_BYTES_THRESHOLD:
                reason = "growth"

        if reason is None:
            return

        self.needs_startup_compaction = False
        self.last_forced_compaction_at = now
        self.size_at_last_forced_compaction = self._current_session_file_size() or 0
        self.forced_compaction_result_pending = True
        log_event(
            "engine", "forced_compaction_triggered", tab_id=self.tab_id, reason=reason,
            session_size_bytes=self.size_at_last_forced_compaction,
        )
        self._push_internal_command("/compact")

    async def _check_hang(self) -> None:
        effective_timeout_s = (HANG_TIMEOUT_MS if self.has_seen_init else STARTUP_TIMEOUT_MS) / 1000
        elapsed = time.monotonic() - self.last_activity
        log_event(
            "engine", "check_hang_tick", tab_id=self.tab_id, turn_pending=self.turn_pending,
            last_activity_s=round(elapsed, 1), hang_count=self.hang_count,
            hang_interrupted_at=self.hang_interrupted_at, has_seen_init=self.has_seen_init,
            effective_timeout_s=effective_timeout_s,
        )
        if not self.turn_pending and self.has_seen_init:
            self.hang_interrupted_at = None
            return
        if elapsed < effective_timeout_s:
            return

        if self.hang_interrupted_at is None:
            self.hang_count += 1
            # Bug fix (2026-09-10): capture what was actually running,
            # before interrupting it, so _handle_failure's replay nudge can
            # tell the model specifically what got force-terminated (per
            # explicit instruction) rather than a generic note -- lets it
            # try something else instead of blindly repeating the same
            # slow/stuck call. Only meaningful if a tool call started more
            # recently than this hang's own elapsed window (otherwise it's
            # a stale name from an earlier, already-finished call).
            if self.last_tool_use_started_at is not None and time.monotonic() - self.last_tool_use_started_at <= elapsed + 1:
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
            # wiping turn_pending/pending_user_text AND resetting
            # silent_turn=True -- so even when _handle_failure's replay
            # genuinely succeeded a few seconds later, its real answer was
            # silently swallowed (silent_turn never got reset back to
            # False, since the replay goes through _push_message directly,
            # not submit()). See the ResultMessage handler below for the
            # other half of this fix.
            self.hang_interrupt_result_pending = True
            try:
                if self.client:
                    await self.client.interrupt()
            except Exception as exc:
                log_event("engine", "hang_soft_interrupt_failed", tab_id=self.tab_id, error=str(exc))
            # Same reasoning as stop()'s own fix -- a hang is plausibly
            # caused by exactly a detached background operation that never
            # completes/never gets polled again, so cancel this tab's
            # in-flight operations here too, not just on an explicit user
            # Stop.
            cancelled = REGISTRY.cancel_for_tab(self.tab_id)
            if cancelled:
                log_event("engine", "hang_soft_interrupt_cancelled_operations", tab_id=self.tab_id, count=cancelled)
            return

        if time.monotonic() - self.hang_interrupted_at < HANG_ESCALATION_GRACE_MS / 1000:
            return
        log_event("engine", "hang_escalation_force_close", tab_id=self.tab_id)
        self.hang_interrupted_at = None
        try:
            if self.client:
                await self.client.disconnect()
        except Exception as exc:
            log_event("engine", "hang_escalation_close_failed", tab_id=self.tab_id, error=str(exc))

    # ------------------------------------------------------------- failure --

    async def _handle_failure(self, exc: BaseException, extra_note: str | None = None) -> None:
        log_event("engine", "handle_failure_entered", tab_id=self.tab_id, hang_count=self.hang_count, turn_pending=self.turn_pending, error=str(exc))
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
        # Per explicit instruction (2026-09-10): don't raise the hang
        # timeout itself (a genuinely slow-but-alive tool call, e.g. a
        # recursive grep/filesystem scan over a large repo under Windows,
        # can legitimately exceed it -- confirmed live, HANG_TIMEOUT_MS
        # stays as-is) -- instead, when it DOES fire, tell the model
        # exactly what got force-terminated so it can try a different
        # approach on retry instead of blindly repeating the same
        # slow/stuck call.
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

                mode = await resolve_mode(self.workspace_dir, self.tab_id)
                self.current_chat_source = mode.chat_source
                if mode.chat_source == "none":
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

                mcp_servers = build_mcp_servers()
                self._system_prompt_language = current_language_name(self.tab_id)
                system_prompt_parts = [
                    persona_system_prompt_append(get_persona(self.workspace_dir)),
                    *[fn() for fn in ALWAYS_ON_INSTRUCTIONS],
                    continuity_pointer_instruction(load_tab_continuity_archive(self.workspace_dir, self.tab_id)),
                    recent_dialogue_history_instruction(self._recent_24h_dialogue_file_path),
                    language_hint_instruction(self._system_prompt_language),
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
                    "disallowed_tools": ["mcp__caroline-notes__notes_login", *_DISALLOWED_CAROLINE_BROWSER_TOOLS],
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
                if system_prompt_append:
                    options_kwargs["system_prompt"] = {"type": "preset", "preset": "claude_code", "append": system_prompt_append}
                options = ClaudeAgentOptions(**options_kwargs)

                query_started_at = time.monotonic()
                log_event("engine", "query_creating", tab_id=self.tab_id, resume=resume_session_id, chat_source=mode.chat_source)
                self.client = ClaudeSDKClient(options=options)
                await self.client.connect(self._input_stream())

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

                    # --- CC CLI usage-cap message ---
                    if isinstance(message, AssistantMessage):
                        text_blocks = [b.text for b in message.content if isinstance(b, TextBlock)]
                        limit_text = next((t for t in text_blocks if CC_CLI_LIMIT_PATTERN.search(t)), None)
                        if limit_text:
                            log_event("engine", "cc_cli_limit_message", tab_id=self.tab_id)
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
                        save_tab_session_id(self.workspace_dir, self.tab_id, sid)

                    if isinstance(message, ResultMessage):
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
                        result_is_fake = self.hang_interrupt_result_pending or self.ignore_next_result_recovery or self.forced_compaction_result_pending
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
                            if was_real_user_turn:
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
                                # for the genuinely-empty case, and the
                                # was_awaiting_post_turn_check_reply guard means
                                # it can fire at most once per originating turn,
                                # never chain into itself if its own reply is
                                # ALSO empty.
                                log_event("engine", "post_turn_completion_check_proactive_empty", tab_id=self.tab_id)
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
                            if self.conn_state.get("kind") != "connected":
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
