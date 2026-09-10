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
    load_tab_continuity_archive,
    load_tab_session_id,
    save_pending_turn,
    save_tab_continuity_archive,
    save_tab_session_id,
)
from app.history import _extract_entries_from_jsonl, read_archived_entries
from app.failure_classification import (
    CC_CLI_LIMIT_PATTERN,
    CLASSIFIER_REFUSAL_PATTERN,
    NOT_LOGGED_IN_PATTERN,
    PROMPT_TOO_LONG_PATTERN,
    SESSION_NOT_FOUND_PATTERN,
    TOOL_CONCURRENCY_ERROR_PATTERN,
    detect_balance_exhaustion,
    extract_classifier_refusal_category,
)
from app.logging_setup import log_event
from app.plugins.loader import build_mcp_servers
from app.persona import get_persona, persona_system_prompt_append
from app.policies import ALWAYS_ON_INSTRUCTIONS, continuity_pointer_instruction, language_hint_instruction
from app.operations import REGISTRY
from app.session_context import set_send, set_tab_id
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
MAX_RESTARTS_PER_WINDOW = 5
RESTART_WINDOW_MS = 10 * 60_000
RESTART_BACKOFF_MS = 60_000
API_RETRY_INTERVAL_MS = 90_000
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

# How many consecutive "authentication_failed" system/api_retry messages on
# one connection before forcing a clean restart (re-resolves the mode and
# rebuilds Options.env) instead of retrying the same doomed request forever.
# Own choice, not a specified value.
AUTH_RETRY_ESCALATION_THRESHOLD = 3

CONTINUE_OR_SILENT_NUDGE_TEMPLATE = (
    "Continue any unfinished work, if there is any. If not, do nothing and reply with exactly [[NO_UPDATE]], with "
    "no explanation. Reply in {language}."
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
_HISTORY_STAMP_PATTERN = re.compile(r"^\[(Sent: |(Sun|Mon|Tue|Wed|Thu|Fri|Sat), )[^\]]*\]\s*", re.IGNORECASE)
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
]


def _is_synthetic_history_text(raw_text: str) -> bool:
    text = _HISTORY_STAMP_PATTERN.sub("", raw_text).strip()
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


def _read_recent_history_texts(session_id: str | None, limit: int = 50) -> list[str]:
    if not session_id:
        return []
    path = claude_project_dir(WORKSPACE_DIR) / f"{session_id}.jsonl"
    try:
        lines = [l for l in path.read_text(encoding="utf-8").split("\n") if l]
    except Exception:
        return []
    texts: list[str] = []
    for line in lines[-limit:]:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
    return texts


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
    """Fire-and-forget: gathers the last 5 non-synthetic history entries and
    asks resolve_user_language (Camerlengo's ai:resolve, NOT
    ai:detectLanguage) what language the user is actually writing in. Never
    awaited by any caller and carries no timeout of its own beyond
    resolve_user_language's own leak-prevention ceiling -- whatever it
    manages to persist simply becomes visible on the NEXT query()
    construction via language_hint_instruction/current_language_name, for
    THIS SAME tab only."""
    from app.plugins.voice_api import resolve_user_language

    async def _run() -> None:
        try:
            texts = _read_recent_history_texts(session_id, 50)
            recent_texts: list[str] = []
            for text in reversed(texts):
                stripped = text.strip()
                if stripped and not _is_synthetic_history_text(stripped):
                    recent_texts.insert(0, stripped)
                if len(recent_texts) >= 5:
                    break
            if not recent_texts:
                return
            name = await resolve_user_language("\n---\n".join(recent_texts))
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
        self.silent_turn = True
        self.classifier_refusal_retry_count = 0
        self.turn_pending = False
        self.pending_user_text: str | None = None
        self.pending_attachments: list[Any] = []

        # activity / hang tracking
        self.last_activity = time.monotonic()
        self.last_user_activity = time.monotonic()
        self.has_seen_init = False
        self.hang_interrupted_at: float | None = None
        self.hang_count = 0

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

        # restart budget
        self.restart_timestamps: list[float] = []

        # session-id tracking. Context ageing is Claude's own native
        # auto-compaction now (see _run_loop's options: autoCompactEnabled)
        # -- no per-turn transcript rewrite, no per-turn CLI restart.
        self.last_saved_session_id: str | None = None

        self.current_chat_source: str | None = None
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
        self.last_rate_limit_info: dict[str, Any] | None = None
        self.last_api_retry_error: str | None = None
        self.mcp_reconnect_timers: dict[str, asyncio.TimerHandle] = {}

        self._run_loop_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------- lifecycle --

    async def start(self) -> None:
        self._run_loop_task = asyncio.create_task(self._run_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    def dispose(self) -> None:
        log_event("engine", "dispose", tab_id=self.tab_id)
        self.ended = True
        if self._watchdog_task:
            self._watchdog_task.cancel()
        self._clear_api_retry_timer()
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

    # --------------------------------------------------------------- submit --

    def submit(self, text: str, attachments: list[Any] | None = None, is_real_user: bool = True, silent: bool = False, is_voice: bool = False) -> None:
        attachments = attachments or []
        self.silent_turn = self.silent_turn and silent
        self.classifier_refusal_retry_count = 0
        self.pending_user_text = text
        self.pending_attachments = attachments
        self.turn_pending = True
        self.last_activity = time.monotonic()
        if is_real_user:
            self.last_user_activity = time.monotonic()
            self.last_real_user_turn_at = time.monotonic()
            self.real_user_turn_answered = False
            self.silence_nudge_sent_for_turn = False
            self.last_visible_output_at = time.monotonic()
            self.last_real_user_question = text
        save_pending_turn(self.workspace_dir, self.tab_id, text, attachments)
        self._push_message(text, attachments, is_voice)

    def inject_proactive(self, text: str, silent: bool = False) -> bool:
        if self.ended:
            return False
        log_event("engine", "proactive_inject", tab_id=self.tab_id, silent=silent, text_len=len(text))
        asyncio.create_task(self.send({"type": "proactive_turn_queued"}))
        self.submit(text, [], False, silent)
        return True

    def stop(self) -> None:
        if not self.turn_pending:
            return
        log_event("engine", "user_stop", tab_id=self.tab_id)
        self.user_stop_requested = True
        if self.client:
            asyncio.create_task(self._safe_interrupt())
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
        prev_kind = self.conn_state.get("kind")
        self.conn_state = {"kind": kind, "reason": reason}
        log_event("engine", "conn_state", tab_id=self.tab_id, prev=prev_kind, new=kind, reason=reason)
        if kind == "connected":
            await self.send({"type": "caroline_status", "status": "connected"})
        elif kind in ("restarting", "restart_backoff"):
            await self.send({"type": "caroline_status", "status": kind, "reason": reason})
        elif kind == "limited":
            # Bug fix (2026-09-10): this used to reuse cls "restarting" --
            # same value the routine internal-churn caroline_status path
            # produces, which chat.js deliberately keeps the lamp green
            # for (a hang/dehydration restart the socket survives, self-
            # resolves in seconds). "limited" is categorically different:
            # a real block (rate limit / usage-window cap) where no turn
            # CAN complete until it clears, possibly hours away. Its own
            # distinct cls lets chat.js tell the two apart and show yellow
            # ("work is impossible right now, but waiting on it") instead
            # of green ("everything's fine").
            await self.send({"type": "system_notice", "text": reason or "", "cls": "limited"})
        elif kind == "billing_blocked":
            await self.send({"type": "system_notice", "text": reason or ""})

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
            self.submit(
                "[Internal: automatic recheck after an API/subscription limit blocked a previous turn -- continue "
                "from wherever you left off.]",
                [], False, True, is_voice,
            )

        loop = asyncio.get_event_loop()
        self.api_retry_timer = loop.call_later(API_RETRY_INTERVAL_MS / 1000, _fire)

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
        """Fires just before Claude's own auto-compaction summarises older
        turns. Copies the current transcript to workspace/dehydrated/ and
        points the continuity pointer at it, so if the summary ever drops
        something the user asks about, Caroline has a real file to Read
        (main.py's expand_dehydrated_ref serves it back). Best-effort;
        never blocks or fails compaction."""
        try:
            trigger = hook_input.get("trigger") if isinstance(hook_input, dict) else None
            transcript_path = hook_input.get("transcript_path") if isinstance(hook_input, dict) else None
            if trigger != "auto" or not transcript_path or not Path(transcript_path).exists():
                return {}
            import shutil
            directory = dehydrated_dir(self.workspace_dir)
            directory.mkdir(parents=True, exist_ok=True)
            archive_path = str(directory / f"{uuid_mod.uuid4()}.txt")
            shutil.copyfile(transcript_path, archive_path)
            save_tab_continuity_archive(self.workspace_dir, self.tab_id, archive_path)
            log_event("engine", "pre_compact_archived", tab_id=self.tab_id, archive_path=archive_path)
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
                await self._check_hang()
                self._check_user_wait_nudge()
                await self._check_progress_narration()
        except asyncio.CancelledError:
            pass

    def _gather_recent_dialogue_for_narration(self, limit: int = 8) -> str:
        """Per explicit correction (2026-09-10): the first version of
        _check_progress_narration fed generate_progress_comment only the
        single original question plus a list of currently-running tool
        names -- confirmed live (screenshots), this produced bland,
        near-identical, operation-sounding remarks every time ("I'm
        checking the phone-related messages...", "let me pull up that
        operation...") instead of anything that actually engaged with the
        conversation, and stayed generic turn after turn since none of its
        inputs ever changed mid-turn. Rebuilt to hand the model the REAL
        recent back-and-forth instead. Deliberately does NOT reuse
        history.read_recent_history() (keyed only by workspace_dir, picks
        whichever .jsonl in the shared project dir was modified most
        recently) -- with multiple tabs sharing one workspace dir, that
        could silently pull another tab's transcript. Stays scoped to
        THIS tab's own resumed session id throughout, same as
        refresh_language_in_background/_read_recent_history_texts.
        When the live session file alone is thin (right after a native
        auto-compaction, whose isCompactSummary replaces older turns), it
        falls back to this tab's own continuity-archive file (the
        pre-compaction transcript the PreCompact hook saved, per-tab,
        never shared)."""
        def _usable_lines(entries: list[dict[str, Any]]) -> list[str]:
            out: list[str] = []
            for entry in entries:
                raw_text = str(entry.get("text") or "")
                clean_text = _HISTORY_STAMP_PATTERN.sub("", raw_text).strip()
                if not clean_text or _is_synthetic_history_text(raw_text):
                    continue
                # A clean_text that is one lone bracketed expression is a
                # placeholder/marker (a bare "[<timestamp>]" line, a
                # compaction stub), never real conversational content.
                if clean_text.startswith("[") and clean_text.endswith("]") and "\n" not in clean_text:
                    continue
                speaker = "User" if entry.get("role") == "user" else "Caroline"
                out.append(f"{speaker}: {clean_text}")
            return out

        lines: list[str] = []
        if self.last_saved_session_id:
            path = claude_project_dir(self.workspace_dir) / f"{self.last_saved_session_id}.jsonl"
            try:
                lines = _usable_lines(_extract_entries_from_jsonl(path.read_text(encoding="utf-8"), str(path)))
            except Exception as exc:
                log_event("engine", "progress_narration_history_read_failed", tab_id=self.tab_id, error=str(exc))
        if len(lines) < limit:
            archive_path = load_tab_continuity_archive(self.workspace_dir, self.tab_id)
            if archive_path:
                try:
                    lines = _usable_lines(read_archived_entries(archive_path)) + lines
                except Exception as exc:
                    log_event("engine", "progress_narration_archive_read_failed", tab_id=self.tab_id, path=archive_path, error=str(exc))

        # Bug fix (2026-09-10): confirmed live -- the disk-persisted
        # transcript doesn't yet contain a real user message that's merely
        # QUEUED (submitted but not yet consumed/flushed by the CLI, e.g.
        # because this tab's query() is still stuck resuming). The
        # narrator then only ever saw Caroline's OWN earlier turn and
        # reacted to nothing the user actually just said -- reading as if
        # she'd lost track of their answer entirely. Always make sure the
        # most recent real question is represented as the LAST line,
        # appending it if the disk read didn't already surface it.
        if self.last_real_user_question:
            question = self.last_real_user_question.strip()
            if not lines or question not in lines[-1]:
                lines.append(f"User: {question}")

        return "\n".join(lines[-limit:])

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
        if self.ended or not self.turn_pending or self.silent_turn or not self.last_real_user_question:
            return
        now = time.monotonic()
        if self.last_visible_output_at is not None and now - self.last_visible_output_at < PROGRESS_NARRATION_INTERVAL_MS / 1000:
            return
        self.last_visible_output_at = now  # claim this tick immediately -- a slow ai:resolve call must not let a second tick double-fire
        from app.plugins.voice_api import generate_progress_comment

        # _gather_recent_dialogue_for_narration always includes
        # last_real_user_question (guaranteed non-empty here, checked
        # above), so dialogue is never empty by construction.
        dialogue = self._gather_recent_dialogue_for_narration()
        log_event("engine", "progress_narration_context", tab_id=self.tab_id, dialogue_chars=len(dialogue), dialogue_preview=dialogue[-300:])
        try:
            comment = await generate_progress_comment(dialogue, current_language_name(self.tab_id))
        except Exception as exc:
            log_event("engine", "progress_narration_failed", tab_id=self.tab_id, error=str(exc))
            return
        if not comment:
            return
        log_event("engine", "progress_narration_sent", tab_id=self.tab_id, comment=comment)
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
            [], False, True, False,
        )

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
            if self.hang_count >= 2:
                log_event("engine", "hang_repeat_force_close", tab_id=self.tab_id, hang_count=self.hang_count)
                try:
                    if self.client:
                        await self.client.disconnect()
                except Exception as exc:
                    log_event("engine", "hang_force_close_failed", tab_id=self.tab_id, error=str(exc))
                return
            log_event("engine", "hang_detected_soft_interrupt", tab_id=self.tab_id)
            self.hang_interrupted_at = time.monotonic()
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

    async def _handle_failure(self, exc: BaseException) -> None:
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
        watchdog_note = (
            f"[System note: this session just recovered from an internal failure (hangCount={self.hang_count}): "
            f"{exc}. This is Caroline's own infrastructure self-healing, already handled -- for your own "
            "situational awareness only. Do not mention this or sound any alarm about it to the user unless they "
            "specifically ask what happened just now.]"
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
                self.inject_proactive(watchdog_note, True)
            else:
                log_event("engine", "handle_failure_continue_or_silent_nudge", tab_id=self.tab_id, lang=lang)
                refresh_language_in_background(self.last_saved_session_id, self.tab_id)
                self.inject_proactive(f"{watchdog_note}\n\n{CONTINUE_OR_SILENT_NUDGE_TEMPLATE.format(language=lang)}", False)
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
        }

    # ------------------------------------------------------------- run loop --

    async def _run_loop(self) -> None:
        set_send(lambda message: self.send(message))
        set_tab_id(self.tab_id)
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
                    "disallowed_tools": ["mcp__caroline-notes__notes_login"],
                    "stderr": _stderr_handler,
                    # Claude's own native auto-compaction handles context
                    # ageing now -- explicitly on, and one long-lived client
                    # across turns (no per-turn transcript rewrite/restart).
                    # settings wants a FILE PATH, not raw JSON -- see
                    # _ensure_settings_file's own docstring for the bug this
                    # fixes.
                    "settings": _ensure_settings_file(self.workspace_dir),
                    "hooks": {"PreCompact": [HookMatcher(hooks=[self._pre_compact_hook])]},
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
                            continue

                    # --- CC CLI usage-cap message ---
                    if isinstance(message, AssistantMessage):
                        text_blocks = [b.text for b in message.content if isinstance(b, TextBlock)]
                        limit_text = next((t for t in text_blocks if CC_CLI_LIMIT_PATTERN.search(t)), None)
                        if limit_text:
                            log_event("engine", "cc_cli_limit_message", tab_id=self.tab_id)
                            self._set_conn_state("limited", limit_text, arm_ignore_next_result=True)
                            self._schedule_api_retry("cc_cli_limit_message", self.turn_is_voice)
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
                        self.turn_pending = False
                        self.pending_user_text = None
                        self.pending_attachments = []
                        clear_pending_turn(self.workspace_dir, self.tab_id)
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
                    if not self.silent_turn and wire is not None:
                        wire = _strip_no_update_from_wire(wire)
                        if wire is not None:
                            # Bug fix (2026-09-10): confirmed live -- voice-reply
                            # auto-play/animation (chat.js checks evt.isVoice on
                            # the "result" event) never fired, because this send
                            # site never attached isVoice at all. server.ts's
                            # original does: { type: "sdk_message", message,
                            # isVoice: this.turnIsVoice } specifically on the
                            # "result" message -- ported that shape here; every
                            # other message type is sent exactly as before.
                            envelope: dict[str, Any] = {"type": "sdk_message", "message": wire}
                            if wire.get("type") == "result":
                                envelope["isVoice"] = self.turn_is_voice
                            await self.send(envelope)
                            if wire.get("type") in ("assistant", "result"):
                                if not self.real_user_turn_answered:
                                    self.real_user_turn_answered = True
                                if wire.get("type") == "assistant":
                                    self.last_visible_output_at = time.monotonic()

                    if isinstance(message, ResultMessage):
                        # One long-lived client now -- the turn is done, but
                        # the stream stays open for the next queued turn
                        # (_input_stream yields it). No per-turn restart.
                        self.silent_turn = True
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
                    self.turn_pending = False
                    self.pending_user_text = None
                    self.pending_attachments = []
                    await self.send({"type": "caroline_status", "status": "stopped"})
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
                        self.submit(replay_text, replay_attachments, True, False, self.turn_is_voice)
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
                        self.submit(replay_text, replay_attachments, True, False, self.turn_is_voice)
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
