"""Primary path for simple, tool-using tasks (2026-09-12), per explicit
design discussion: try answering a real user turn through a small/cheap
model (Camerlengo's own resolve_agentic(), via reforce's AI.py) BEFORE
falling back to the full Claude Agent SDK session -- same persona, same
tools (the SAME PluginTool objects every plugin already declares, via
app/plugins/loader.py's to_openai_tool_def(); see that function's own doc
comment for why this is native dual-format support, not a translation
layer), just a cheaper engine for anything simple enough not to need
Claude's own reasoning.

Runs LOCALLY (2026-09-12, corrected back from a same-day redesign that had
routed every model-call STEP through a new squirrelwisdom.com v2 command,
ai:resolveAgenticStep -- per explicit instruction: "нужно эту часть вообще
переделать; подписка должна браться со squirrelwisdom.com, а не ключи"
meant the model-provider KEY should come from SW instead of being
hardcoded/vendored in cleartext, NOT that the dialogue itself should be
proxied through SW turn by turn. Camerlengo's own resolve_agentic() loop
(tool-calling, iteration, escalation judgment) runs entirely in this
process, exactly like every other in-process caller of that function --
the ONE thing that comes from SquirrelWisdom is the OpenRouter API key
itself, fetched once per process (model_key_provisioning.py), Fernet-
encrypted in transit with a key derived from the caller's own session so
it's never sent or stored in cleartext, and never written to disk. SW
login gates whether the small model is available at all (no login -> no
key -> straight to the full SDK), same as every other SW-gated feature in
this codebase (see sw_gate.py) -- but the actual conversation content
never crosses the network to SW.

Escalation to the full SDK happens in exactly two ways, per explicit
instruction -- deliberately NOT based on timing, iteration count, or
dispatch()'s own "running" status (that's just normal tool execution, not
a complexity signal):
  1. The model's OWN judgment, expressed as a content-level sentinel
     (ESCALATION_SENTINEL) in its final reply -- covers "this task is
     harder than it looked" AND "the user has already had to correct me
     more than once" (both are visible to the model via the recent-dialogue
     messages it's given, so no separate mechanism is needed for the
     second case).
  2. A mechanical safety check in the executor: the SAME (tool_name, args)
     pair called too many times in a row is a broken/looping execution,
     not a complexity judgment -- raises NeedsEscalation immediately,
     which aborts resolve_agentic()'s loop rather than letting the model
     "retry" into the exact same loop.
max_iterations is deliberately set to a value that should never fire
first in real use (see MAX_ITERATIONS's own comment) -- it exists in
resolve_agentic() purely as a runaway-loop backstop for callers who don't
have a better signal, not as a task-complexity budget; this caller has a
better signal (the two above) and doesn't want to rely on it.

Packaging (not yet resolved): reaches Camerlengo's AI.py via
_resolve_camerlengo_dir() below -- NO hardcoded absolute path anywhere. A
dev checkout resolves it via a sibling `reforce` repo checkout (this dev
machine's own layout: REPO/caroline and REPO/reforce side by side), or
CAROLINE_CAMERLENGO_PATH for a non-standard layout. Vendoring a copy into
real installs at packaging time (as originally planned) is a SEPARATE,
not-yet-resolved follow-up: AI.py's own hardcoded-secret defaults are now
fixed (Config.OPENAI_KEY/Config.OPENROUTER_KEY, never a literal -- see
that file's own history), but Config.py itself still holds real Partners
Solutions production secrets (admin/email passwords, a Google Maps key,
etc.) unrelated to this feature -- vendoring it as-is into a public
installer is not safe, and this module deliberately does not attempt
that yet. If neither the sibling checkout nor CAROLINE_CAMERLENGO_PATH
resolves (or the import itself fails for any reason), camerlengo_ai stays
None and run_small_model_turn() escalates immediately, every time -- this
path degrades to "always escalate to the SDK" rather than ever crashing
backend startup over a missing/broken dependency, per this module's own
"never a hard dependency" guarantee.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from app import session_context
from app.durability import load_tab_continuity_archive
from app.logging_setup import log_event
from app.model_key_provisioning import get_model_provider_key
from app.operations import REGISTRY as OPERATIONS_REGISTRY
from app.operations import _operation_to_dict, dispatch
from app.persona import Persona
from app.owner_profile import get_owner_profile, owner_profile_system_prompt_clause
from app.working_memory import load_working_memory, working_memory_system_prompt_clause
from app.memory_topics import memory_topics_system_prompt_clause
from app.plugins.loader import PluginTool, discover_plugins, to_openai_tool_def
from app.plugins.sw_api import clear_funds_exhausted, mark_funds_exhausted
from app.policies import (
    continuity_pointer_instruction,
    credentials_check_notes_first_instruction,
    event_memory_check_first_instruction,
    learn_from_mistakes_instruction,
    memory_check_first_instruction,
    no_internal_mechanics_to_user_instruction,
    no_unauthorized_secret_changes_instruction,
    no_unbounded_filesystem_scans_instruction,
    prefer_own_backend_tools_instruction,
    proactive_context_recovery_instruction,
    recent_dialogue_history_instruction,
    self_sufficiency_instruction,
    system_temp_dir_instruction,
    timestamp_awareness_instruction,
)

# Per explicit instruction (2026-09-13), after a real capability audit
# ("проверь, что маленькая модель имеет ВСЁ, что имеет полный путь"): every
# ALWAYS_ON_INSTRUCTIONS entry (policies.py) that doesn't assume a Claude-
# Code-CLI-native affordance this engine structurally lacks (Bash's own
# run_in_background/BashOutput, the Task subagent tool, TodoWrite, or the
# multi-bubble live narration the SDK path's own streaming turn produces).
# Deliberately NOT a curated subset for any other reason -- this list is
# ALWAYS_ON_INSTRUCTIONS minus exactly the entries that reference a tool
# this engine doesn't have, so a future addition to that list is included
# here automatically unless it hits the same structural limit.
_SHARED_ALWAYS_ON_INSTRUCTIONS = (
    no_unbounded_filesystem_scans_instruction,
    timestamp_awareness_instruction,
    no_internal_mechanics_to_user_instruction,
    proactive_context_recovery_instruction,
    no_unauthorized_secret_changes_instruction,
    prefer_own_backend_tools_instruction,
    learn_from_mistakes_instruction,
    self_sufficiency_instruction,
    system_temp_dir_instruction,
    credentials_check_notes_first_instruction,
    memory_check_first_instruction,
    event_memory_check_first_instruction,
)

# Mirrors chat_session.py's own "disallowed_tools": ["mcp__caroline-notes__
# notes_login"] on the full SDK path's ClaudeAgentOptions -- notes_login is
# an interactive device-code flow meant to be driven by squirrelwisdom-login
# (a Skill on the SDK path), not called directly by the model; kept
# consistent here rather than silently giving this engine MORE access to a
# plugin than the full path allows itself.
_DISALLOWED_TOOL_NAMES = {"notes_login"}

# Mirrors local_tts_launcher.py's/skills_seed.py's own shipped-vs-dev-tree
# resolution pattern exactly: a shipped copy under backend-py/ itself first
# (not yet populated by the Makefile -- see module docstring's "Packaging"
# note), then a dev-tree sibling-repo checkout as a fallback for a repo
# checkout that hasn't been packaged yet.
_SHIPPED_CAMERLENGO_DIR = Path(__file__).resolve().parent.parent / "camerlengo"
_DEV_TREE_CAMERLENGO_DIR = Path(__file__).resolve().parents[3] / "reforce"


def _resolve_camerlengo_dir() -> Path | None:
    override = os.environ.get("CAROLINE_CAMERLENGO_PATH")
    if override and (Path(override) / "AI.py").is_file():
        return Path(override)
    if (_SHIPPED_CAMERLENGO_DIR / "AI.py").is_file():
        return _SHIPPED_CAMERLENGO_DIR
    if (_DEV_TREE_CAMERLENGO_DIR / "AI.py").is_file():
        return _DEV_TREE_CAMERLENGO_DIR
    return None


camerlengo_ai: Any = None
_camerlengo_dir = _resolve_camerlengo_dir()
if _camerlengo_dir is None:
    log_event("engine", "small_model_engine_camerlengo_not_found",
              shipped=str(_SHIPPED_CAMERLENGO_DIR), dev_tree=str(_DEV_TREE_CAMERLENGO_DIR))
else:
    if str(_camerlengo_dir) not in sys.path:
        sys.path.insert(0, str(_camerlengo_dir))
    try:
        import AI as camerlengo_ai  # type: ignore[no-redef]  # noqa: E402
    except Exception as exc:  # noqa: BLE001 -- see module docstring: never a hard dependency
        log_event("engine", "small_model_engine_camerlengo_import_failed", path=str(_camerlengo_dir), error=str(exc))
        camerlengo_ai = None

ESCALATION_SENTINEL = "[[NEED_ESCALATION]]"

# Per explicit instruction (2026-09-12): NOT a task-complexity budget --
# resolve_agentic()'s own doc comment documents this same thing. Set high
# enough that real use should NEVER hit it; the two escalation mechanisms
# above are what actually decide when to hand off, not a step count.
MAX_ITERATIONS = 200

# How many times the SAME (tool_name, json-args) pair may repeat before the
# executor treats this as a broken/looping run and aborts -- a mechanical
# check, unrelated to task complexity (see module docstring point 2).
REPEATED_CALL_LIMIT = 3

# Per explicit instruction (2026-09-13), following a completeness audit
# against the full SDK path ("я хочу, чтобы это работало не хуже Claude
# SDK"): resolve_agentic() itself has THREE ways of giving up that return a
# plain string rather than raising or emitting ESCALATION_SENTINEL -- an
# API-level exception ("Agent error: ..."), a malformed/empty model
# response (""), and exhausting MAX_ITERATIONS ("Agent reached maximum
# iterations without completing."). None of these were ever caught here,
# so any of the three would have been shown to the user as if it were a
# genuine, completed answer -- a silent abandonment dressed up as success,
# exactly the failure mode the user was most emphatic about never wanting.
# Deliberately NOT fixed inside reforce's own resolve_agentic() (shared,
# multi-caller server code -- see AI.py's own history of what is and isn't
# safe to change there); caught here instead, on Caroline's own side of the
# boundary, and treated as a mandatory escalation regardless of how the
# small model itself would have judged the task.
_AGENT_ERROR_PREFIX = "Agent error:"
_AGENT_MAX_ITERATIONS_TEXT = "Agent reached maximum iterations without completing."


def _is_silent_infra_failure(text: str) -> str | None:
    """Returns a human-readable reason if `text` is one of resolve_agentic()'s
    own silent give-up strings, else None."""
    stripped = text.strip()
    if not stripped:
        return "model returned an empty response"
    if stripped.startswith(_AGENT_ERROR_PREFIX):
        return stripped
    if stripped == _AGENT_MAX_ITERATIONS_TEXT:
        return stripped
    return None


# Per explicit instruction (2026-10-03), after a live incident where the account's
# OpenRouter balance ran out mid-cascade (tier 3 and the final verification pass
# both got "Agent error: OpenRouter returned 402 Insufficient funds") and the
# system quietly swallowed that into the same generic "try again later" text used
# for any other failure -- hiding a concrete, fixable cause behind a dead end
# instead of a wait. This account-wide balance is the SAME one sw_api.py's own
# narration/translation calls already detect and surface on the status bar (see
# mark_funds_exhausted/get_funds_exhausted_reason there) -- wired in here too, and
# given the SAME "wait for it to clear" treatment the SDK path already gives a
# rate limit, instead of finalizing a failure on the very first hit.
_FUNDS_EXHAUSTED_RETRY_INTERVAL_S = 60.0
_FUNDS_EXHAUSTED_MAX_WAIT_S = 1800.0


class _FundsExhaustedGivingUp(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _funds_exhausted_reason_in(text: str) -> str | None:
    reason = _is_silent_infra_failure(text)
    if reason and ("insufficient funds" in reason.lower() or " 402" in reason):
        return reason
    return None


async def _call_with_funds_wait(call_fn: Callable[[], "asyncio.Future[str]"], tab_id: str, emit: Callable[[str], None]) -> str:
    """Wraps one resolve_agentic call: on a detected OpenRouter funds-exhaustion
    response, marks it on the shared status-bar channel and waits/retries instead
    of treating it as an ordinary failure. Gives up (raising _FundsExhaustedGivingUp)
    only after _FUNDS_EXHAUSTED_MAX_WAIT_S of polling -- a real top-up can take a
    while, but an indefinite silent wait would never surface to the user at all."""
    waited_s = 0.0
    while True:
        result = await call_fn()
        reason = _funds_exhausted_reason_in(result)
        if reason is None:
            clear_funds_exhausted()
            return result
        mark_funds_exhausted(reason)
        log_event("engine", "small_model_funds_exhausted", tab_id=tab_id, reason=reason, waited_s=round(waited_s))
        emit(f"OpenRouter balance is insufficient ({reason}) -- waiting for a top-up.")
        if waited_s >= _FUNDS_EXHAUSTED_MAX_WAIT_S:
            raise _FundsExhaustedGivingUp(reason)
        await asyncio.sleep(_FUNDS_EXHAUSTED_RETRY_INTERVAL_S)
        waited_s += _FUNDS_EXHAUSTED_RETRY_INTERVAL_S


def _funds_exhausted_final_answer(reason: str) -> str:
    return (
        f"I can't answer right now -- the OpenRouter balance has run out ({reason}). "
        "Once it is topped up, everything will work as before."
    )


# Per explicit instruction (2026-09-13), corrected same-day after a real
# live test: resolve_agentic() itself has no wall-clock ceiling of its own
# (MAX_ITERATIONS is a call-count backstop, not a time one). This used to
# be a single "give up after 300s total" cutoff -- confirmed live as WRONG:
# a real 8-mailbox check made genuine, continuous progress (declare_plan,
# one notes_get after another gathering credentials) the whole time, each
# gap between tool calls well under a minute, and still got killed at the
# 300s mark despite never actually stalling -- the exact same "check
# ELAPSED, not ACTIVITY" mistake already fixed once for the full SDK path's
# own hang detection (_check_hang uses last_activity, not total turn
# duration). Replaced with the same shape: STALL_TIMEOUT_S is measured from
# the last real progress event (a tool call or the model finishing), not
# from the turn's start -- a task making steady progress can run
# indefinitely; only a genuine stretch of silence trips this.
STALL_TIMEOUT_S = 120
# Absolute backstop on top of the stall detector, in case something keeps
# "progressing" (a call every stall-interval) without ever actually
# finishing -- deliberately generous, a safety net not a target.
ABSOLUTE_TURN_TIMEOUT_S = 1800

# OpenRouter's own unified reasoning-tokens parameter (forwarded through
# resolve_agentic()/OpenRouterAdapter._resolve() -- see their own doc
# comments) -- per explicit instruction (2026-09-13), following a
# completeness audit that found gpt-5.1 was being called with NO reasoning
# parameter at all, i.e. whatever OpenRouter defaults a bare chat-
# completions call to for that model, quite possibly not its own strongest
# mode. "high" costs more reasoning tokens per call than "medium"/"low";
# reconsider if this turns out to be a real latency/cost problem in
# practice once this path is re-enabled.
REASONING_EFFORT = {"effort": "high"}

# Per explicit instruction (2026-09-13): OpenRouter's "middle-out" transform
# (enabled by default inside OpenRouterAdapter._resolve() for every OTHER
# caller) silently compresses the MIDDLE of a long message list once it
# doesn't fit the model's context -- for a caller like this one running its
# own long-lived, multi-iteration tool-calling loop, that means the
# transform could erase the model's own memory of tool calls/results
# earlier in the SAME turn, on top of whatever context management this
# module already does itself (the recent-dialogue window, 8000-char
# per-result truncation inside resolve_agentic()). Disabled here; every
# OTHER existing caller of resolve_agentic()/OpenRouterAdapter is
# unaffected (this is passed explicitly only from THIS module).
DISABLE_MIDDLE_OUT: list[str] = []


class NeedsEscalation(Exception):
    """Raised by the executor_fn to abort resolve_agentic()'s loop
    immediately -- caught by run_small_model_turn() and turned into an
    {"status": "escalate", ...} result. Never let resolve_agentic() itself
    see this as a normal tool error (which the model might just retry into
    the same loop) -- it has to actually stop the loop."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _TurnStalled(Exception):
    """Raised by _resolve_agentic_with_watchdog() when it gives up WAITING
    on a resolve_agentic() call -- never means the underlying worker thread
    actually stopped (see _TurnTracker.cancelled's own doc comment: threads
    can't be preempted, only asked nicely via that flag, which the caller
    already set before raising this)."""

    def __init__(self, elapsed_s: float, kind: str) -> None:
        super().__init__(f"{kind} timeout after {elapsed_s:.0f}s")
        self.elapsed_s = elapsed_s
        self.kind = kind  # "stall" (no progress for STALL_TIMEOUT_S) or "absolute" (ABSOLUTE_TURN_TIMEOUT_S ceiling)


async def _resolve_agentic_with_watchdog(ai: Any, tracker: _TurnTracker, **resolve_kwargs: Any) -> str:
    """Runs ai.resolve_agentic(**resolve_kwargs) in a worker thread (it's a
    synchronous, uncancellable call from a third-party module) while
    polling tracker.last_progress_at instead of just awaiting a flat
    deadline -- per explicit correction (2026-09-14), confirmed live that a
    flat "give up after N seconds total" cutoff killed a turn that was
    making real, continuous (if slow) progress the whole time. Only a
    genuine STALL (no tool call/completion for STALL_TIMEOUT_S) or the far
    more generous ABSOLUTE_TURN_TIMEOUT_S backstop gives up. Giving up here
    only stops WAITING -- see _TurnTracker.cancelled for how the abandoned
    thread itself is kept from doing further real-world damage (email
    sends, logins, ...) once nobody is listening for its result anymore."""
    tracker.touch_progress()
    started = time.monotonic()
    task: asyncio.Task[str] = asyncio.create_task(asyncio.to_thread(ai.resolve_agentic, **resolve_kwargs))
    try:
        while True:
            try:
                done, _pending = await asyncio.wait({task}, timeout=5.0)
            except asyncio.CancelledError:
                # Bug fix (2026-09-15), confirmed live -- a manual Stop
                # cancels the OUTER task wrapping this whole coroutine
                # (chat_session.py's stop() -> ChatSession._small_model_task
                # .cancel()), which used to unwind straight out through
                # here without ever touching tracker.cancelled -- the exact
                # flag executor_fn (below) checks before every tool
                # dispatch to refuse acting for an abandoned turn. Result:
                # the UI flipped back to ready, but the orphaned worker
                # thread kept right on making real tool calls (an email
                # send, an IMAP login, ...) completely unsupervised, with
                # Stop having done nothing to it at all. Same "tell the
                # thread to stop doing further real-world damage" signal
                # the stall/absolute-timeout branches below already set --
                # a user-initiated Stop deserves it at least as much as an
                # automatic giveup does, arguably more.
                tracker.cancelled.set()
                raise
            if task in done:
                return task.result()
            now = time.monotonic()
            stalled_for = now - tracker.last_progress_at
            if stalled_for > STALL_TIMEOUT_S:
                tracker.cancelled.set()
                raise _TurnStalled(stalled_for, "stall")
            if now - started > ABSOLUTE_TURN_TIMEOUT_S:
                tracker.cancelled.set()
                raise _TurnStalled(now - started, "absolute")
    finally:
        # Best-effort only -- can't actually stop the underlying OS thread
        # (see this function's own doc comment), just avoids an "a Task was
        # destroyed but it is pending" warning if we're giving up on it.
        if not task.done():
            task.cancel()


def _persona_system_message(persona: Persona) -> str:
    """Standalone identity text for the small-model path -- deliberately
    NOT persona.persona_system_prompt_append(), which is written to
    override a preceding "you are Claude Code" preamble (see its own doc
    comment: "Everything above... describes the ENGINE running underneath
    you") that only exists when running through the real CLI. There is no
    such preamble here -- this model never sees anything Claude-Code-
    specific -- so that override text would reference nothing and just be
    confusing filler."""
    gender_lower = persona.gender.lower()
    if "female" in gender_lower:
        agreement = ('Use feminine self-referential grammatical forms ("поняла" not "понял", "сказала" not '
                     '"сказал", etc.) when replying in a language that grammatically marks gender.')
    elif "male" in gender_lower:
        agreement = ('Use masculine self-referential grammatical forms ("понял" not "поняла", "сказал" not '
                     '"сказала", etc.) when replying in a language that grammatically marks gender.')
    else:
        agreement = "Use grammatical forms matching your stated gender when replying in a language that grammatically marks gender."
    bio_line = persona.biography or persona.bio
    return (
        f"You are {persona.name}, {persona.age}, {persona.gender}. {persona.bio}\n"
        f"{bio_line}\n"
        f"{agreement}\n"
        "You are a real personal assistant handling this message directly and efficiently, using the tools "
        "available to you when needed. Don't narrate what tool you're using or how you work internally -- just "
        "do it and report the outcome, the same way a competent human assistant would."
    )


def _shared_policy_text(workspace_dir: str, tab_id: str) -> str:
    """The subset of policies.py's ALWAYS_ON_INSTRUCTIONS that applies here
    unchanged (see _SHARED_ALWAYS_ON_INSTRUCTIONS's own doc comment), plus
    continuity_pointer_instruction -- the SAME text the full SDK path
    appends to its system prompt (chat_session.py's system_prompt_parts),
    so a fact/rule the user only ever stated once doesn't quietly stop
    applying depending on which engine happens to answer a given turn."""
    parts = [fn() for fn in _SHARED_ALWAYS_ON_INSTRUCTIONS]
    archive_path = load_tab_continuity_archive(workspace_dir, tab_id)
    parts.append(continuity_pointer_instruction(archive_path))
    if archive_path:
        # Per explicit instruction (2026-09-13): confirmed live -- the
        # "recent dialogue" window you're given below is short (only the
        # last few exchanges) and can end up ENTIRELY made of internal
        # housekeeping (proactive nudges and their own replies) right after
        # a compaction, leaving you with what looks like a blank slate even
        # though this is a long-running, ongoing conversation. That is
        # never a reason to ask the user what's going on or to re-explain
        # something they already told you -- the complete prior transcript
        # is saved on disk; go read it yourself with read_file before you
        # ask the user anything or say you lack context. Do this
        # immediately and silently -- never tell the user you had to go
        # look something up.
        parts.append(
            "If the dialogue shown to you below looks thin, empty, or doesn't match what the user's message "
            "clearly presupposes you already know, that's because it was trimmed for space, not because "
            f"nothing happened -- the full conversation so far is saved at: {archive_path}\nRead that file "
            "yourself before asking the user to repeat or clarify anything. Never tell the user you had to go "
            "read a file to recall this."
        )
    # Bug fix (2026-10-03), confirmed live: recent_dialogue_history_instruction (the rolling
    # last-24h dialogue file, a SEPARATE safety net from continuity_pointer_instruction above --
    # see its own docstring) was never wired in here at all, only on the full SDK path -- this
    # engine had no pointer to any of its own memory/session-history files whatsoever. Deferred
    # import: chat_session.py imports this module at load time, so a top-level import would be
    # circular -- see this module's other deferred imports for the same reason.
    from app.chat_session import _recent_24h_dialogue_path
    dialogue_path = str(_recent_24h_dialogue_path(workspace_dir, tab_id))
    parts.append(recent_dialogue_history_instruction(dialogue_path))
    return "\n\n".join(p for p in parts if p)


def _engine_instructions(language: str) -> str:
    return (
        "You are being asked to help with something that looked simple enough to answer directly -- possibly "
        "using one or more of the tools available to you (including generic file read/write and running shell "
        "commands or Python scripts -- you have real tools for all of that, not just the specific plugins like "
        "email/notes/screenshots). Do so.\n\n"
        "If a tool call comes back with status \"running\" instead of a real result, that is not the end of the "
        "story -- call check_operation_status with its operation_id to find out what actually happened before "
        "you reply. Calling a tool is not the same thing as the task being done: before you tell the user "
        "something is finished, make sure you have actually seen the real result and it matches what they asked "
        "for -- never write a placeholder, a stub, or a summary of what you intend to fill in, and never say "
        "you're about to do something without actually doing it in this same turn. If a tool's own short "
        "description isn't enough to be sure how to use it correctly, call get_tool_instructions for it first.\n\n"
        "If the task has more than one concrete step (going through several accounts/files/items one by one, a "
        "multi-part request), call declare_plan FIRST and list every step, then actually carry each one out and "
        "call mark_step_done right after -- this is what proves the work happened, not the words you use to "
        "describe it. Don't declare a plan and then just describe what you're about to do instead of doing it. "
        "When a step depends on identifying or confirming something you're not already certain of (is X actually "
        "running, does Y exist, which of several candidates is the real one), don't bake your first guess into "
        "that step and act on it directly -- plan a broad discovery step first (list every plausible candidate, "
        "not just the one name you expect), then a step that actually narrows it down to the real one, before "
        "the step that acts. Skipping straight from a guess to the action step is how confident-sounding wrong "
        "answers happen.\n\n"
        f"Reply in {language} unless the user's own message is written in a different language -- then match "
        "theirs instead.\n\n"
        "If you judge that you are NOT coping with this task -- too many tool-call failures, the task turning "
        "out to be more complex than it looked once you got into it, the conversation history below shows the "
        "user already had to correct you or repeat themselves more than once, you conclude there is no tool "
        "available to you that can accomplish some necessary step, OR the task is the kind of large, independent "
        "sub-investigation a capable assistant would normally hand off to a separate helper/subagent to work on "
        "in isolation (extensive open-ended research, a large self-contained exploration with its own many steps) "
        "-- you have no such delegation capability here, so recognize that immediately rather than attempting it "
        "piecemeal and losing track partway through -- stop and reply with EXACTLY "
        f"{ESCALATION_SENTINEL} and nothing else, no explanation. In the last two cases specifically: never tell "
        "the user you're unable to do something and leave it at that -- a more capable assistant that picks up "
        "right after you may well have a way to do it, so hand off instead of declining on their behalf, and "
        "recognize the need for that hand-off up front rather than after struggling partway through. This hands "
        "the conversation off to a more capable assistant -- it is not a failure on your part, it's the right "
        "call the moment a task turns out to be bigger than it looked. Only do this as a genuine judgment call, "
        "not a first resort -- most things you'll be asked fall well within what you can handle directly."
    )



# OpenAI/Camerlengo-format tool_defs for the three generic operation-control
# tools (app/operations.py's build_operations_mcp_server(), same descriptions
# copied verbatim) -- these don't come from a plugin, so to_openai_tool_def()
# doesn't apply; hand-written once here instead. Handled specially in
# _make_executor_fn() below (never go through registry.lookup/dispatch()),
# since they operate ON operations.py's own process-wide REGISTRY rather than
# starting a new plugin operation themselves.
_OPERATIONS_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "check_operation_status",
            "description": (
                "Checks the status of a previously started long-running tool operation (one whose start "
                "returned status=\"running\" instead of \"done\"). Returns the current status "
                "(running/done/error/cancelled), any partial/intermediate data reported so far, and the final "
                "result once done."
            ),
            "parameters": {"type": "object", "properties": {"operation_id": {"type": "string"}}, "required": ["operation_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_operation",
            "description": "Cancels a previously started long-running tool operation by its operation_id.",
            "parameters": {"type": "object", "properties": {"operation_id": {"type": "string"}}, "required": ["operation_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tool_instructions",
            "description": (
                "Fetches the detailed usage guidance for a specific tool by name (not every tool has any -- "
                "most are self-explanatory from their own short description alone). Call this when you're about "
                "to use a tool whose behavior/conventions/gotchas you're not fully sure of, or when a tool's own "
                "short description hints there's more nuance. Cheap to call, no side effects."
            ),
            "parameters": {"type": "object", "properties": {"tool_name": {"type": "string"}}, "required": ["tool_name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_own_backend",
            "description": (
                "Returns a live, authoritative description of your own backend right now, on this exact "
                "install -- which plugins/tools it currently provides. Call this whenever you're unsure whether "
                "a specific tool belongs to your own backend or comes from somewhere else (a separately/"
                "independently-registered MCP server, which varies from machine to machine), or when the user "
                "asks about your own architecture, setup, or capabilities."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_OPERATIONS_TOOL_NAMES = {t["function"]["name"] for t in _OPERATIONS_TOOL_DEFS}

# Per explicit instruction (2026-09-13), Phase 2 of the small-model fix
# plan: give the model a real, trackable way to say "here's my plan"
# instead of prose ("сделаю так: ...") that nothing can check. Handled
# specially in _make_executor_fn() below, same pattern as the operations
# trio -- these mutate a per-turn _TurnTracker rather than going through
# dispatch(). Declaring a plan is optional (most single-step asks don't
# need one), but ONCE declared, the verification pass in run_small_model_turn()
# checks declared vs. actually-marked-done steps before the turn's text is shown.
_PLAN_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "declare_plan",
            "description": (
                "For any task that takes more than one concrete step (going through several accounts/files/"
                "items one by one, a multi-part request), call this FIRST and list every step before doing any "
                "of them -- this is what actually tracks your progress. Skip it only for a single, immediately-"
                "answerable request that needs no more than one step."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "stages": {
                        "type": "array",
                        "description": (
                            "Optional. Use instead of steps when parts of the work can run at the same time: "
                            "an ordered list of stages, each either 'sequential' (its steps run one after another) "
                            "or 'parallel' (its steps are independent and run concurrently, at most 4 at once). "
                            "Each stage starts only after the previous one finishes."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "mode": {"type": "string", "enum": ["sequential", "parallel"]},
                                "steps": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["mode", "steps"],
                        },
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_step_done",
            "description": (
                "Call this immediately after you have actually completed one step from your declare_plan list -- "
                "only after seeing its real result, never before or instead of doing it. index is 0-based; result "
                "is a short factual summary of what actually happened. Every declared step needs one of these "
                "before the overall task counts as finished."
            ),
            "parameters": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, "result": {"type": "string"}},
                "required": ["index", "result"],
            },
        },
    },
]
_PLAN_TOOL_NAMES = {t["function"]["name"] for t in _PLAN_TOOL_DEFS}

_STAGE_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_parallel_stage",
            "description": (
                "Runs one 'parallel' stage of the plan you declared with declare_plan: each of its steps is "
                "executed as its own independent sub-task, at most 4 at a time, and the results come back "
                "together. Call it only when every earlier stage is already done. Each step is marked done "
                "automatically if it succeeded; a step that failed (even after one retry) is reported as not done "
                "-- report that honestly, do not pretend it worked."
            ),
            "parameters": {
                "type": "object",
                "properties": {"stage": {"type": "integer", "description": "0-based index of the parallel stage in your plan"}},
                "required": ["stage"],
            },
        },
    },
]
_STAGE_TOOL_NAMES = {t["function"]["name"] for t in _STAGE_TOOL_DEFS}
PARALLEL_STAGE_CONCURRENCY = 4
PARALLEL_BRANCH_ATTEMPTS = 2

# Per explicit instruction (2026-10-03), after a live incident where a tier
# guessed at a fact (which Windows process name the Android emulator VM
# actually runs under) instead of checking -- confirmed live via a standalone
# test that camerlengo's existing AI.resolveOnline() (OpenRouter's ":online"
# web-search mode, Exa/native-backed) correctly answers exactly this kind of
# question with a cited source. Exposed here as a plain blocking tool (see
# executor_fn -- it's already running on its own worker thread, so calling
# the adapter's synchronous resolve() directly, with no main-loop hop, is
# correct and simplest) rather than a new plugin, since it needs no
# dispatch()/operation-registry machinery of its own and uses the SAME
# OpenRouter key already fetched for this turn -- no new credential. NOT
# available to the verifier (which stays tool-free by firm requirement).
_WEB_SEARCH_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Asks a web-search-grounded model a question and returns its answer based on live, current web "
                "results (not just training data), with a source where possible. Use this to confirm a fact "
                "you're genuinely NOT certain of before acting on a guess -- e.g. what name a process actually "
                "runs under, how a specific tool/API really behaves, a current version or syntax -- rather than "
                "assuming and finding out you were wrong after the fact. This costs real money per call -- use "
                "it when you're actually unsure, not for things you already know or every routine step."
            ),
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
]
_WEB_SEARCH_TOOL_NAMES = {t["function"]["name"] for t in _WEB_SEARCH_TOOL_DEFS}


def _run_web_search_tool(name: str, args: dict[str, Any], ai: Any, cfg: Any) -> str:
    question = str(args.get("question") or "").strip()
    if not question:
        return "ERROR: 'question' is required."
    # One retry (2026-10-03, confirmed live): the FIRST resolveOnline call sometimes
    # comes back empty while an identical immediate retry succeeds -- same ordinary
    # intermittent-empty-result shape already retried everywhere else in this file
    # (key fetch, verification, narration), not a sign anything is actually broken.
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            result = ai.resolveOnline(question, model=cfg.AI_MODEL_SMALL)
        except Exception as exc:  # noqa: BLE001 -- a failed search is a tool result, not a turn-ending error
            last_error = exc
            continue
        if result:
            return str(result)
    return f"ERROR: web_search failed: {last_error}" if last_error else "ERROR: web_search returned no answer (tried twice)."


@dataclass
class _TurnTracker:
    """Per-turn (not per-registry) mutable state -- one instance per
    run_small_model_turn() call, threaded through _make_executor_fn() so
    the executor can update it as tool calls actually happen. Read by the
    verification pass (see _build_verification_prompt)."""

    tool_call_count: int = 0
    plan_steps: list[str] | None = None
    # (mode, step indices) per stage, in order; indices refer to plan_steps.
    plan_stages: list[tuple[str, list[int]]] = field(default_factory=list)
    plan_done: dict[int, str] = field(default_factory=dict)
    parallel_stages_run: set[int] = field(default_factory=set)
    # Per-branch real tool calls and results from run_parallel_stage, kept
    # separately so the verifier sees each parallel branch's own evidence.
    branch_trace: list[str] = field(default_factory=list)
    # operation_ids seen with status "running" that were never subsequently
    # observed (via check_operation_status) to reach a terminal status --
    # see the "имитирует результат" incident this was built for: a real
    # async tool call was started, checked once, then abandoned while the
    # model told the user a plausible-sounding story about it.
    pending_operation_ids: set[str] = field(default_factory=set)
    # Per explicit correction (2026-09-14), after a real live test: WHEN
    # this turn last made real progress (a tool call actually starting, or
    # the model finishing) -- touched from both the worker thread
    # (executor_fn, at the top of every call) and the main-loop side
    # (on_progress) since resolve_agentic() runs in a separate OS thread.
    # Read by the watchdog in run_small_model_turn() to detect a genuine
    # STALL (no activity for STALL_TIMEOUT_S), replacing an earlier flat
    # "give up after N seconds total" cutoff that killed a turn making
    # real, if slow, continuous progress.
    last_progress_at: float = field(default_factory=time.monotonic)
    # Set once the watchdog gives up waiting on this turn -- checked at the
    # top of executor_fn so a resolve_agentic() thread that's already been
    # abandoned (its result no longer awaited by anyone) stops short of
    # actually dispatching further real tool calls (email sends, IMAP
    # logins with real passwords, ...) instead of racing on to completion
    # or failure on its own, unsupervised, after the fact -- confirmed live
    # as a real gap: an abandoned thread kept making real (doomed, since
    # its own bridge back to the main loop was already gone) email calls
    # with real credentials for minutes after this turn had already been
    # reported as escalated.
    cancelled: threading.Event = field(default_factory=threading.Event)

    def touch_progress(self) -> None:
        self.last_progress_at = time.monotonic()



def _run_plan_tool(name: str, args: dict[str, Any], tracker: _TurnTracker) -> str:
    if name == "declare_plan":
        stages_arg = args.get("stages")
        if stages_arg:
            steps: list[str] = []
            stages: list[tuple[str, list[int]]] = []
            for stage in stages_arg:
                if not isinstance(stage, dict):
                    return "ERROR: each stage must be an object with 'mode' and 'steps'."
                mode = stage.get("mode")
                if mode not in ("sequential", "parallel"):
                    return f"ERROR: stage mode must be 'sequential' or 'parallel', got {mode!r}."
                indices = []
                for step in stage.get("steps") or []:
                    indices.append(len(steps))
                    steps.append(str(step))
                if indices:
                    stages.append((mode, indices))
        else:
            steps = [str(s) for s in (args.get("steps") or [])]
            stages = [("sequential", list(range(len(steps))))] if steps else []
        tracker.plan_steps = steps
        tracker.plan_stages = stages
        tracker.plan_done = {}
        parallel_hints = [str(i) for i, (mode, _) in enumerate(stages) if mode == "parallel"]
        hint = (
            f" Parallel stage(s) {', '.join(parallel_hints)}: run each with run_parallel_stage(stage=N) once every earlier stage is done."
            if parallel_hints else ""
        )
        return f"Plan recorded with {len(steps)} step(s) in {len(stages)} stage(s).{hint} Call mark_step_done after each one actually completes."
    if name == "mark_step_done":
        index = args.get("index")
        result = args.get("result", "")
        if tracker.plan_steps is None:
            return "ERROR: no plan was declared yet -- call declare_plan first."
        if not isinstance(index, int) or not (0 <= index < len(tracker.plan_steps)):
            return f"ERROR: index {index} is out of range for a plan with {len(tracker.plan_steps)} step(s)."
        tracker.plan_done[index] = str(result)
        remaining = len(tracker.plan_steps) - len(tracker.plan_done)
        return f"Step {index} marked done. {remaining} step(s) remaining." if remaining else "All steps marked done."
    return f"ERROR: unknown plan tool '{name}'"


async def _run_parallel_stage(
    stage_index: int, *, tracker: _TurnTracker, tab_id: str | None, send: session_context.SendFn | None,
    main_loop: asyncio.AbstractEventLoop, registry: "ToolRegistry", ai: Any, cfg: Any,
    system_text: str, user_text: str, model: str,
) -> str:
    if tracker.plan_stages is None or not (0 <= stage_index < len(tracker.plan_stages)):
        return f"ERROR: the declared plan has no stage {stage_index}."
    mode, indices = tracker.plan_stages[stage_index]
    if mode != "parallel":
        return f"ERROR: stage {stage_index} is sequential -- do its steps yourself, one at a time."
    if stage_index in tracker.parallel_stages_run:
        return f"ERROR: stage {stage_index} already ran."
    tracker.parallel_stages_run.add(stage_index)

    branch_tools = [
        t for t in registry.tool_defs
        if t["function"]["name"] not in _PLAN_TOOL_NAMES and t["function"]["name"] not in _STAGE_TOOL_NAMES
    ]
    semaphore = asyncio.Semaphore(PARALLEL_STAGE_CONCURRENCY)

    async def run_branch(step_index: int) -> tuple[bool, str]:
        step = tracker.plan_steps[step_index]
        last_error = ""
        async with semaphore:
            for attempt in range(1, PARALLEL_BRANCH_ATTEMPTS + 1):
                log_event("engine", "small_model_branch_started", tab_id=tab_id, stage=stage_index, step=step_index, attempt=attempt)
                branch_tracker = _TurnTracker()
                base_executor = _make_executor_fn(registry, tab_id, send, main_loop, branch_tracker, ai, cfg)
                calls: list[str] = []

                def traced_executor(name: str, args: dict[str, Any], _base=base_executor, _calls=calls) -> str:
                    result = _base(name, args)
                    _calls.append(f"- {name}({json.dumps(args or {}, ensure_ascii=False, default=str)[:200]}) -> {str(result)[:300]}")
                    return result

                branch_messages = [
                    {"role": "system", "content": system_text},
                    {"role": "user", "content": (
                        f"The user's overall task (for context): {user_text}\n\n"
                        f"You are carrying out ONE step of the overall plan, in parallel with other steps. Step: {step}\n\n"
                        "Do only this step: call the tools it needs, wait for their real results, and reply with one "
                        "short factual summary. Do not do the other steps."
                    )},
                ]
                try:
                    text = await _call_with_funds_wait(
                        lambda: _resolve_agentic_with_watchdog(
                            ai, branch_tracker,
                            messages=branch_messages, tool_defs=branch_tools, executor_fn=traced_executor,
                            model=model, max_iterations=MAX_ITERATIONS,
                            transforms=DISABLE_MIDDLE_OUT, reasoning=REASONING_EFFORT,
                        ),
                        tab_id, lambda _line: None,
                    )
                    if _usable(text):
                        log_event("engine", "small_model_branch_finished", tab_id=tab_id, stage=stage_index, step=step_index, attempt=attempt, ok=True)
                        tracker.branch_trace.append(
                            f"Branch of step {step_index} (\"{step}\"), attempt {attempt}:\n" + ("\n".join(calls) or "  (no tools were called)")
                        )
                        return True, text.strip()
                    last_error = "empty reply"
                except _FundsExhaustedGivingUp:
                    raise
                except Exception as exc:  # noqa: BLE001 -- a failed branch is a result, not a turn-ending error
                    last_error = f"{type(exc).__name__}: {exc}"
                log_event("engine", "small_model_branch_finished", tab_id=tab_id, stage=stage_index, step=step_index, attempt=attempt, ok=False, error=last_error)
                tracker.branch_trace.append(
                    f"Branch of step {step_index} (\"{step}\"), attempt {attempt}, not done ({last_error}):\n"
                    + ("\n".join(calls) or "  (no tools were called)")
                )
        return False, last_error

    outcomes = await asyncio.gather(*(run_branch(i) for i in indices), return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, _FundsExhaustedGivingUp):
            raise outcome

    lines = [f"Stage {stage_index} (parallel) finished."]
    for step_index, outcome in zip(indices, outcomes):
        if isinstance(outcome, BaseException):
            ok, info = False, f"{type(outcome).__name__}: {outcome}"
        else:
            ok, info = outcome
        if ok:
            tracker.plan_done[step_index] = info[:500]
            lines.append(f"OK, step {step_index}: {info[:300]}")
        else:
            lines.append(f"NOT DONE, step {step_index}: {info}")
    return "\n".join(lines)


@dataclass
class ToolRegistry:
    tool_defs: list[dict[str, Any]]
    lookup: dict[str, tuple[str, PluginTool]]
    # tool_name -> that tool's plugin's usage_instructions, for the
    # get_tool_instructions tool -- see loader.py's Plugin.usage_instructions
    # own doc comment for why this is fetched on demand rather than injected
    # into the system prompt outright.
    tool_instructions: dict[str, str] = field(default_factory=dict)
    # Same own_plugins shape as plugins/loader.py's build_mcp_servers() builds
    # for the SDK path's describe_own_backend -- server_name + each tool's
    # name/description, one entry per plugin discovered THIS turn. See
    # policies.py's prefer_own_backend_tools_instruction for why this must
    # never be a fixed/hardcoded list.
    own_plugins: list[dict[str, Any]] = field(default_factory=list)


def build_tool_registry() -> ToolRegistry:
    """Every tool every plugin exposes, in OpenAI/Camerlengo format -- per
    explicit instruction, tools are fully shared between this path and the
    SDK path, not a curated subset; see to_openai_tool_def()'s own doc
    comment for why this needs no plugin-file changes at all. Also carries
    the same generic check_operation_status/stop_operation/
    get_tool_instructions trio the full SDK path gets via
    build_operations_mcp_server() (2026-09-13 capability audit) -- without
    these, a tool call that crosses dispatch()'s FAST_PATH_TIMEOUT_S and
    comes back status="running" was a dead end for this engine: no way to
    ever learn the real result, so it had to guess or just claim success.
    Confirmed live as the actual cause of a placeholder note being left
    behind while the model told the user it was "preparing" the real one."""
    tool_defs: list[dict[str, Any]] = list(_OPERATIONS_TOOL_DEFS) + list(_PLAN_TOOL_DEFS) + list(_STAGE_TOOL_DEFS) + list(_WEB_SEARCH_TOOL_DEFS)
    lookup: dict[str, tuple[str, PluginTool]] = {}
    tool_instructions: dict[str, str] = {}
    own_plugins: list[dict[str, Any]] = []
    for plugin in discover_plugins():
        plugin_tools_for_summary = []
        for t in plugin.tools:
            if t.name in _DISALLOWED_TOOL_NAMES:
                continue
            tool_defs.append(to_openai_tool_def(t))
            lookup[t.name] = (plugin.name, t)
            plugin_tools_for_summary.append({"name": t.name, "description": t.description})
            if plugin.usage_instructions:
                tool_instructions[t.name] = plugin.usage_instructions
        if plugin_tools_for_summary:
            own_plugins.append({"server_name": f"caroline-{plugin.name}", "tools": plugin_tools_for_summary})
    return ToolRegistry(tool_defs=tool_defs, lookup=lookup, tool_instructions=tool_instructions, own_plugins=own_plugins)


async def _run_operations_tool(name: str, args: dict[str, Any], registry: ToolRegistry, tracker: _TurnTracker) -> str:
    """Implements check_operation_status/stop_operation/get_tool_instructions
    directly against operations.py's own process-wide OPERATIONS_REGISTRY --
    NOT via dispatch() (these tools inspect/control an operation dispatch()
    already created, they don't start a new one of their own). Run on the
    main event loop (see _make_executor_fn's run_coroutine_threadsafe call)
    since Operation.task is a live asyncio.Task; .cancel() and reading task
    state should only ever happen on the loop that owns it. Also updates
    tracker.pending_operation_ids -- an operation reaching a terminal status
    here means it's no longer "dangling" for the verification pass."""
    if name == "check_operation_status":
        op = OPERATIONS_REGISTRY.get(args["operation_id"])
        if op is None:
            return "Unknown or already-completed operation_id."
        body = _operation_to_dict(op)
        if op.status in ("done", "error", "cancelled"):
            op.collected = True  # no completion notice repeating it (operations._notify_unless_collected)
            OPERATIONS_REGISTRY.forget(op.id)
            tracker.pending_operation_ids.discard(op.id)
        return str(body)
    if name == "stop_operation":
        op = OPERATIONS_REGISTRY.get(args["operation_id"])
        if op is None or op.task is None:
            return "Unknown or already-completed operation_id."
        op.task.cancel()
        tracker.pending_operation_ids.discard(op.id)
        return f"Cancelled {args['operation_id']}."
    if name == "get_tool_instructions":
        tool_name = args["tool_name"]
        text = registry.tool_instructions.get(tool_name)
        if text is None:
            return f'No detailed usage instructions for "{tool_name}" -- its own short description is all there is.'
        return text
    if name == "describe_own_backend":
        body = {
            "architecture": (
                "You run on a Python backend with a plugin system. Every plugin is exposed to you as its own "
                "tool group, named \"caroline-<plugin-name>\" (see \"plugins\" below for the exact current "
                "list, name and tools included) -- rebuilt fresh from this backend's actual plugin set on "
                "every turn, so this is always accurate for right now, not something memorized or stale. Any "
                "OTHER MCP server you can see that is NOT in this list comes from somewhere else entirely -- "
                "registered independently of this backend, on this specific machine, outside this backend's "
                "knowledge or control, and not guaranteed to even be working. Whenever a task can be done by "
                "a tool listed here, always prefer it over a same-purpose tool from an unlisted server, even "
                "if the other one looks more convenient, is already connected, or seems more familiar."
            ),
            "plugins": registry.own_plugins,
        }
        return str(body)
    return f"ERROR: unknown operations tool '{name}'"


def _make_executor_fn(
    registry: ToolRegistry, tab_id: str | None, send: session_context.SendFn | None,
    main_loop: asyncio.AbstractEventLoop, tracker: _TurnTracker, ai: Any, cfg: Any,
    stage_runner: Callable[[int], Awaitable[str]] | None = None,
) -> Callable[[str, dict[str, Any]], str]:
    """Bridges resolve_agentic()'s synchronous, worker-thread-side
    executor_fn(name, args) -> str callback into Caroline's own async
    plugin dispatch -- runs the REAL dispatch() (app/operations.py, the
    same uniform start/status/stop + auto-logging contract the SDK path's
    wrap_tool() already uses for every tool call) on the MAIN event loop
    via run_coroutine_threadsafe, blocking only this call's own worker
    thread (never the main loop) until it resolves. tab_id/send are
    captured explicitly and re-set via session_context inside the
    dispatched coroutine's own context -- contextvars set inside a
    coroutine only affect that coroutine's own (isolated, discarded-after)
    context, so there's deliberately no reset/cleanup dance needed here.
    tracker records plan/pending-operation state as calls actually happen
    -- see _TurnTracker's own doc comment."""
    seen_calls: dict[tuple[str, str], int] = {}

    def executor_fn(name: str, args: dict[str, Any]) -> str:
        args = args or {}
        if tracker.cancelled.is_set():
            # This turn's own watchdog already gave up waiting on us (a
            # genuine stall, see STALL_TIMEOUT_S) and reported that to our
            # caller -- nobody is listening for this call's real result
            # anymore. Refuse instantly rather than actually dispatching
            # (a real email send, an IMAP login with a real password,
            # etc.) unsupervised after the fact; see _TurnTracker.cancelled
            # own doc comment for the incident this fixes.
            return "ERROR: this turn was already abandoned (stalled/timed out) -- stop, do not attempt further tool calls."
        tracker.touch_progress()
        call_key = (name, json.dumps(args, sort_keys=True, default=str))
        seen_calls[call_key] = seen_calls.get(call_key, 0) + 1
        count = seen_calls[call_key]
        log_event("engine", "small_model_tool_call", tab_id=tab_id, tool=name, args=args, repeat_count=count)
        if count > REPEATED_CALL_LIMIT:
            log_event("engine", "small_model_repeated_call_detected", tab_id=tab_id, tool=name, args=args, count=count)
            raise NeedsEscalation(f"tool '{name}' called with identical arguments {count} times in a row -- likely a stuck loop")
        tracker.tool_call_count += 1

        if name in _PLAN_TOOL_NAMES:
            return _run_plan_tool(name, args, tracker)

        if name in _STAGE_TOOL_NAMES:
            if stage_runner is None:
                return "ERROR: parallel stages are not available in this context."
            stage_index = args.get("stage")
            if not isinstance(stage_index, int):
                return "ERROR: 'stage' must be an integer index."
            return asyncio.run_coroutine_threadsafe(stage_runner(stage_index), main_loop).result()

        if name in _WEB_SEARCH_TOOL_NAMES:
            # Already running on this turn's own worker thread (resolve_agentic calls
            # executor_fn off the main loop) -- a plain blocking call here is correct,
            # same as _run_plan_tool, no main-loop hop needed (unlike dispatch()'d
            # plugin tools below, which need the main loop for session_context/Operation).
            return _run_web_search_tool(name, args, ai, cfg)

        if name in _OPERATIONS_TOOL_NAMES:
            return asyncio.run_coroutine_threadsafe(
                _run_operations_tool(name, args, registry, tracker), main_loop,
            ).result()

        entry = registry.lookup.get(name)
        if entry is None:
            log_event("engine", "small_model_unknown_tool", tab_id=tab_id, tool=name)
            return f"ERROR: unknown tool '{name}'"
        plugin_name, plugin_tool = entry

        async def _run() -> dict[str, Any]:
            session_context.set_tab_id(tab_id)
            if send is not None:
                session_context.set_send(send)
            # Waits for the real outcome (see dispatch()'s wait_to_end): this
            # thread blocks on the result anyway, and a "running" answer only
            # handed the model an internal operation to narrate to the user.
            return await dispatch(plugin_name, plugin_tool.name, plugin_tool.handler, args, wait_to_end=True)

        envelope = asyncio.run_coroutine_threadsafe(_run(), main_loop).result()
        log_event("engine", "small_model_tool_result", tab_id=tab_id, tool=name, status=envelope.get("status"))
        note = f"\n{envelope['memory_note']}" if envelope.get("memory_note") else ""
        if envelope.get("status") == "error":
            return f"ERROR: {envelope.get('error')}{note}"
        if envelope.get("status") == "cancelled":
            return f"ERROR: this tool call was cancelled before it finished.{note}"
        if envelope.get("status") == "running":
            # Genuinely normal (a slow tool call, e.g. a network-bound
            # plugin) -- NOT a signal of anything wrong. resolve_agentic()
            # has no polling concept of its own, so the best honest answer
            # right now is "still working"; the model can decide whether
            # to wait (call a cheap tool to pass a beat) or conclude. This
            # is expected to be rare given FAST_PATH_TIMEOUT_S. Tracked as
            # "dangling" until a later check_operation_status observes a
            # terminal status (see _run_operations_tool) -- confirmed live
            # as the actual shape of the "имитирует результат" incident:
            # a real operation started, checked once, then abandoned.
            op_id = envelope.get("operation_id")
            if op_id:
                tracker.pending_operation_ids.add(op_id)
            return f"Operation {op_id} is still running."
        result = envelope.get("result")
        return (result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)) + note

    return executor_fn


def _build_verification_prompt(tracker: _TurnTracker, final_text: str, can_escalate: bool, tier_trace: list[str] | None = None) -> str:
    """Verification only (2026-10-03): the verifier has NO tools and must not do
    any work. It checks the candidate against the mechanical facts below.

    `can_escalate` (2026-10-03): True for every tier except the last -- per explicit
    instruction ("если задача не доделана, то нужна эскалация"), finding the task
    genuinely unfinished is itself an escalation trigger, same as a tier's own
    ESCALATION_SENTINEL/NeedsEscalation/_TurnStalled signals -- the verifier is not
    the end of the line while a stronger tier is still available. Only once the last
    tier has also been verified and found wanting does the verifier write a final
    "could not complete" message instead of escalating further.

    `tier_trace` (2026-10-03 bug fix, confirmed live, "дай верификатору информацию о
    проделанных действиях и их результатах"): verification_messages (the call site)
    is built from a COPY of `messages` -- but the tier's own tool-calling conversation
    happens inside resolve_agentic()'s own internal loop and is never written back to
    that outer list, only the continuation-note SUMMARY is (and only once, on
    cross-tier escalation). Without this, the verifier saw nothing but the user's
    question and the candidate's own claimed answer -- no evidence any tool was ever
    called -- and (confirmed live) concluded the reply was fabricated even when 8 real
    IMAP calls had just happened. This is the SAME tier_trace the tier loop already
    tracks for its own continuation notes, passed straight through before it's
    cleared."""
    parts = [
        "The reply above is from another assistant that worked on the user's request. You are a verifier only: "
        "you have no tools and must not try to do or finish any work. Check the reply against the facts below."
    ]
    if tier_trace:
        parts.append(
            "Real tool calls made while producing that reply, with their real results (this is ground truth -- "
            "the reply above is a separate, later summary of this, not the source of it):\n" + "\n".join(tier_trace[-40:])
        )
    if tracker.branch_trace:
        parts.append(
            "Parallel-stage branches, each with its own real tool calls and results (ground truth, same rule):\n"
            + "\n".join(tracker.branch_trace[-40:])
        )
    elif not can_escalate:
        # No tier_trace at all on the terminal, non-escalating pass usually means no
        # tier ever got far enough to call anything -- worth saying explicitly, since
        # its absence could otherwise read as "nothing to check" rather than "nothing
        # was ever actually done".
        parts.append("No tool calls were recorded for this attempt at all.")
    if tracker.plan_steps is not None:
        undone = [s for i, s in enumerate(tracker.plan_steps) if i not in tracker.plan_done]
        if undone:
            parts.append(
                f"A plan of {len(tracker.plan_steps)} step(s) was declared; these were never marked done: "
                + "; ".join(f'"{s}"' for s in undone)
            )
    if tracker.pending_operation_ids:
        parts.append(
            "These tool operations were started but never confirmed as finished: "
            + ", ".join(sorted(tracker.pending_operation_ids))
        )
    if can_escalate:
        parts.append(
            "If, based on the reply and the facts above, the task is genuinely NOT finished yet -- real work "
            f'still needed, not just a wording issue -- reply with EXACTLY this and nothing else: "{ESCALATION_SENTINEL} '
            '<one short sentence saying what is still missing or wrong>". A stronger model will pick it up from '
            "exactly that point, with the full tool-call history so far. Only do this for genuinely unfinished "
            "work -- if it's actually done, write the final reply instead, as described below."
        )
    parts.append(
        "Your reply is the final message the user will read. Write it fresh, clearly and to the point, directly "
        "answering their request, even if the earlier reply was already fine. Never describe the other assistant's "
        "work as a report. Do not claim anything is done unless the reply and facts above show it."
        + (
            ""
            if can_escalate
            else (
                " If the task genuinely could not be completed, say so plainly -- what was done, what was not, "
                "and why -- and stop there. Do not tell the user what to do, do not suggest steps for them to "
                "take, and do not give any instruction, task, or advice, even phrased gently or as an offer, "
                "unless they explicitly asked for suggestions. This was the last attempt -- there is no one left "
                "to hand it to but the user, and piling an unrequested to-do list on them on top of an unfinished "
                "task is not a neutral close-out, it's offloading your own unfinished work onto them."
            )
        )
        + " You have no tools and this is the only message you get to send -- never promise to check, follow up, "
        "or give it a minute: nothing will act on that promise after this reply, so it would just be a lie."
    )
    return "\n\n".join(parts)


# English like every text in the code; the answer is translated into the
# conversation's language on its way out (_finish_small_model_turn_answered).
_UNAVAILABLE_ANSWER = "I can't answer this right now -- please try again a little later."


def _usable(text: str) -> bool:
    return bool(text.strip()) and ESCALATION_SENTINEL not in text and _is_silent_infra_failure(text) is None


def _answer(text: str) -> dict[str, Any]:
    return {"status": "answered", "text": text}


def _continuation_note(tier_trace: list[str]) -> str:
    return (
        "[Internal: a previous attempt at this same request already ran these tool calls, with their real "
        "results. Continue from exactly where it stopped -- do not repeat these calls, do not restart from scratch.]\n"
        + "\n".join(tier_trace[-25:])
    )


COMPLEXITY_TIMEOUT_S = 12.0

_COMPLEXITY_PROMPT = (
    "Classify a request for an assistant that has tools (shell commands, files, apps, windows, email, notes). "
    "Reply with exactly one word: COMPLEX if it needs several dependent tool steps (check, then act, then verify), "
    "otherwise SIMPLE.\n\nRequest:\n"
)


async def _start_tier_index(ai: Any, cfg: Any, user_text: str) -> int:
    """Pre-assessment: a complex multi-step tool task starts directly at the second
    tier (mini) instead of the first (nano). AI_MODEL_SMALL is used only for this
    one-word service call -- it's also tier 0 of the real cascade, just invoked here
    tool-free and in isolation."""
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(ai.resolve, _COMPLEXITY_PROMPT + user_text[:2000], model=cfg.AI_MODEL_SMALL, no_fallback=True),
            timeout=COMPLEXITY_TIMEOUT_S,
        )
        verdict = str(raw or "").strip().upper()
        start = 1 if verdict.startswith("COMPLEX") else 0
    except Exception as exc:  # noqa: BLE001 -- classification must never block the turn
        log_event("engine", "small_model_complexity_failed", error=str(exc), error_type=type(exc).__name__)
        start = 0
    log_event("engine", "small_model_complexity", start_tier=start)
    return start


async def run_small_model_turn(
    tab_id: str,
    workspace_dir: str,
    persona: Persona,
    user_text: str,
    recent_dialogue_lines: list[str],
    language: str,
    send: session_context.SendFn | None,
    on_live_dialogue_update: Callable[[list[str]], None] | None = None,
    get_new_user_comments: Callable[[], list[str]] | None = None,
    on_activity: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Answers `user_text` through the OpenRouter-only path -- never hands off
    to the Claude SDK (2026-10-03). Always returns {"status": "answered", "text"}.

    Three-tier cascade (nano -> mini -> AI_MODEL_ALTERNATE/Sonnet), carrying the
    previous tier's real tool-call trace forward on every escalation. A tier
    escalates mechanically (ESCALATION_SENTINEL, NeedsEscalation, _TurnStalled)
    same as before, AND a verifier (AI_MODEL_MINI, tool-free -- a separate role)
    checks every tier's
    "done" candidate against the mechanical facts (undone plan steps, unconfirmed
    operations): if it finds the task genuinely unfinished and a later tier still
    exists, that is ALSO an escalation, not a final answer. Only once the last tier
    has been tried and verified does the verifier write the actual final user-facing
    text -- a real answer if it's done, or a plain, honest "could not complete this"
    if it still isn't, with no unsolicited instructions/tasks for the user either way.

    on_live_dialogue_update(lines), if given, is called every time this turn's
    own live exchange changes -- feeds the progress narrator. get_new_user_comments(),
    if given, is polled once per resolve_agentic() iteration so a long turn stays
    responsive to what the user says while it runs.
    """
    log_event("engine", "small_model_turn_started", tab_id=tab_id, text_len=len(user_text))
    if camerlengo_ai is None:
        log_event("engine", "small_model_engine_unavailable", tab_id=tab_id)
        return _answer(_UNAVAILABLE_ANSWER)

    api_key = await get_model_provider_key()
    if not api_key:
        log_event("engine", "small_model_no_key_available", tab_id=tab_id)
        return _answer(_UNAVAILABLE_ANSWER)

    # Explicit adapter with the freshly-fetched key -- never rely on
    # camerlengo_ai.AI()'s own default (Config.OPENROUTER_KEY, server-only)
    # from this process; Caroline always passes its own, per-session key.
    # Built here (moved up 2026-10-03) rather than right before the tier loop
    # so executor_fn (built below) can use `ai`/`cfg` directly for the
    # web_search tool, which is a plain blocking call, not a dispatch()'d
    # plugin operation.
    adapter = camerlengo_ai.OpenRouterAdapter(api_key=api_key)
    ai = camerlengo_ai.AI(adapter=adapter)
    cfg = camerlengo_ai.Config

    registry = build_tool_registry()
    system = "\n\n".join([
        _persona_system_message(persona),
        owner_profile_system_prompt_clause(get_owner_profile(workspace_dir)),
        working_memory_system_prompt_clause(load_working_memory(workspace_dir)),
        memory_topics_system_prompt_clause(workspace_dir),
        _engine_instructions(language),
        _shared_policy_text(workspace_dir, tab_id),
    ])
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for line in recent_dialogue_lines:
        is_caroline = line.startswith("Caroline:")
        content = line.split(":", 1)[-1].strip()
        messages.append({"role": "assistant" if is_caroline else "user", "content": content})
    # Deferred import: chat_session imports this module at load time, so a
    # top-level import here would be circular -- see this module's own
    # module-level comments for other examples of this pattern.
    from app.chat_session import _format_timestamp_for_model
    from datetime import datetime, timezone
    sent_line = f"[Sent: {_format_timestamp_for_model(datetime.now(timezone.utc).astimezone())}]"
    messages.append({"role": "user", "content": f"{sent_line}\n{user_text}"})

    main_loop = asyncio.get_running_loop()
    tracker = _TurnTracker()
    current_model = {"name": cfg.AI_MODEL_SMALL}

    async def run_stage(stage_index: int) -> str:
        return await _run_parallel_stage(
            stage_index, tracker=tracker, tab_id=tab_id, send=send, main_loop=main_loop,
            registry=registry, ai=ai, cfg=cfg, system_text=messages[0]["content"],
            user_text=user_text, model=current_model["name"],
        )

    executor_fn = _make_executor_fn(registry, tab_id, send, main_loop, tracker, ai, cfg, stage_runner=run_stage)

    live_dialogue = [f"User: {user_text}"]
    if on_live_dialogue_update:
        on_live_dialogue_update(list(live_dialogue))

    # Real tool calls of the CURRENT tier, rendered as text -- handed to the next
    # tier on escalation so it continues instead of redoing them. Cleared after each
    # handoff so no tier ever receives the same calls twice.
    tier_trace: list[str] = []

    def emit(line: str) -> None:
        # Real facts for the narrator (see ChatSession.on_activity). Never lets a failure reach the turn.
        if on_activity:
            try:
                on_activity(line)
            except Exception:  # noqa: BLE001
                pass

    def on_progress(evt: dict[str, Any]) -> None:
        tracker.touch_progress()
        log_event(
            "engine", "small_model_progress", tab_id=tab_id, event_type=evt.get("type"),
            tool=evt.get("name"), iteration=evt.get("iteration"),
        )
        if evt.get("type") == "tool_call":
            log_event("engine", "small_model_tool_output", tab_id=tab_id, tool=evt.get("name"), args=evt.get("args"), result=str(evt.get("result", "")))
            args = json.dumps(evt.get("args") or {}, ensure_ascii=False, default=str)[:300]
            emit(f"Tool call {evt.get('name')} (arguments: {args[:120]}); result: {str(evt.get('result', ''))[:160]}")
            tier_trace.append(f"- {evt.get('name')}({args}) -> {str(evt.get('result', ''))[:300]}")
        if evt.get("type") == "done" and on_live_dialogue_update:
            text = evt.get("text", "")
            if ESCALATION_SENTINEL not in text:
                live_dialogue.append(f"Caroline: {text}")
                on_live_dialogue_update(list(live_dialogue))

    def get_new_messages() -> list[dict[str, Any]] | None:
        if not get_new_user_comments:
            return None
        comments = get_new_user_comments()
        if not comments:
            return None
        out = []
        for c in comments:
            log_event("engine", "small_model_live_comment_injected", tab_id=tab_id, text_len=len(c))
            live_dialogue.append(f"User: {c}")
            out.append({"role": "user", "content": c})
        if on_live_dialogue_update:
            on_live_dialogue_update(list(live_dialogue))
        return out

    def on_usage(usage: dict[str, Any]) -> None:
        log_event("engine", "small_model_usage", tab_id=tab_id, **usage)

    # Final tier: AI_MODEL_ALTERNATE (Sonnet) used here WITH tools as a real
    # escalation worker -- a distinct role from AI_MODEL_MINI's OTHER, tool-free
    # use below as the verifier.
    tiers = [cfg.AI_MODEL_SMALL, cfg.AI_MODEL_MINI, cfg.AI_MODEL_ALTERNATE]
    log_event("engine", "small_model_resolved", tab_id=tab_id, tiers=tiers, tool_count=len(registry.tool_defs))

    # Verification (AI_MODEL_MINI, no tools) runs after every tier that
    # thinks it's done. Per explicit instruction (2026-10-03, "если задача не
    # доделана, то нужна эскалация"): finding the task genuinely unfinished is
    # itself an escalation trigger while a later tier still exists -- verification
    # is no longer only a final polish pass tacked on after the loop.
    async def run_verifier(candidate_text: str, can_escalate: bool, trace: list[str]) -> str:
        verification_messages = list(messages)
        verification_messages.append({"role": "assistant", "content": candidate_text or "(no answer was produced)"})
        verification_messages.append({"role": "user", "content": _build_verification_prompt(tracker, candidate_text, can_escalate, trace)})
        return await _call_with_funds_wait(
            lambda: _resolve_agentic_with_watchdog(
                ai, tracker,
                messages=verification_messages, tool_defs=None, executor_fn=executor_fn,
                model=cfg.AI_MODEL_MINI, max_iterations=MAX_ITERATIONS, on_progress=on_progress,
                get_new_messages=get_new_messages, transforms=DISABLE_MIDDLE_OUT,
                reasoning=REASONING_EFFORT, on_usage=on_usage,
            ),
            tab_id, emit,
        )

    async def verify_candidate(candidate_text: str, can_escalate: bool, trace: list[str] | None = None) -> tuple[str, bool]:
        """Returns (text, needs_escalation). A technical verifier failure (empty
        reply/exception on both attempts) is never treated as an escalation signal --
        it just falls back to the candidate as-is, same as before this change.
        _FundsExhaustedGivingUp is NOT a technical failure -- let it propagate to
        the caller, which turns it into the same honest final answer regardless of
        whether funds ran out on a tier call or here."""
        emit("Checking the result (no tools).")
        verified = ""
        for attempt in (1, 2):
            log_event("engine", "small_model_verification_started", tab_id=tab_id, attempt=attempt, has_candidate=bool(candidate_text), can_escalate=can_escalate)
            try:
                verified = await run_verifier(candidate_text, can_escalate, trace or [])
            except _FundsExhaustedGivingUp:
                raise
            except Exception as exc:  # noqa: BLE001 -- verifier failure is a retry, never silence
                log_event("engine", "small_model_verification_failed", tab_id=tab_id, attempt=attempt, error=str(exc), error_type=type(exc).__name__)
                verified = ""
            if verified.strip():
                break
        if not verified.strip():
            log_event("engine", "small_model_verification_fallback_candidate", tab_id=tab_id, text_len=len(candidate_text))
            return candidate_text, False
        if can_escalate and ESCALATION_SENTINEL in verified:
            log_event("engine", "small_model_verification_escalating", tab_id=tab_id, note=verified.replace(ESCALATION_SENTINEL, "").strip()[:200])
            return verified, True
        log_event("engine", "small_model_verification_done", tab_id=tab_id, text_len=len(verified), text=verified)
        return verified, False

    start_index = await _start_tier_index(ai, cfg, user_text)
    final_text = ""
    for index, model in enumerate(tiers):
        if index < start_index:
            continue
        log_event("engine", "small_model_tier_started", tab_id=tab_id, tier=index, model=model)
        emit(f"Tier {index + 1} ({model}) is working.")
        current_model["name"] = model
        candidate = ""
        try:
            candidate = await _call_with_funds_wait(
                lambda: _resolve_agentic_with_watchdog(
                    ai, tracker,
                    messages=messages, tool_defs=registry.tool_defs, executor_fn=executor_fn,
                    model=model, max_iterations=MAX_ITERATIONS, on_progress=on_progress,
                    get_new_messages=get_new_messages, transforms=DISABLE_MIDDLE_OUT,
                    reasoning=REASONING_EFFORT, on_usage=on_usage,
                ),
                tab_id, emit,
            )
        except _TurnStalled as exc:
            # Not escalated: the stalled worker thread may still be running, and
            # tracker.cancelled (shared with every later tier) is what keeps it from
            # acting -- continuing here would either let it act or block the next tier.
            log_event("engine", "small_model_tier_stalled", tab_id=tab_id, tier=index, model=model, kind=exc.kind, elapsed_s=round(exc.elapsed_s))
            break
        except _FundsExhaustedGivingUp as exc:
            log_event("engine", "small_model_funds_exhausted_giving_up", tab_id=tab_id, tier=index, model=model, reason=exc.reason)
            return _answer(_funds_exhausted_final_answer(exc.reason))
        except NeedsEscalation as exc:
            log_event("engine", "small_model_tier_escalation_mechanical", tab_id=tab_id, tier=index, model=model, reason=exc.reason)
        except Exception as exc:  # noqa: BLE001 -- the cascade must never raise into chat
            log_event("engine", "small_model_tier_failed", tab_id=tab_id, tier=index, model=model, error=str(exc), error_type=type(exc).__name__)

        if _usable(candidate):
            log_event("engine", "small_model_tier_answered", tab_id=tab_id, tier=index, model=model, text_len=len(candidate), text=candidate)
            emit(f"Tier {index + 1} ({model}) produced an answer; moving on to checking it.")
            can_escalate = index < len(tiers) - 1
            try:
                verified_text, needs_escalation = await verify_candidate(candidate, can_escalate, tier_trace)
            except _FundsExhaustedGivingUp as exc:
                log_event("engine", "small_model_funds_exhausted_giving_up", tab_id=tab_id, tier=index, model=model, reason=exc.reason)
                return _answer(_funds_exhausted_final_answer(exc.reason))
            if not needs_escalation:
                final_text = verified_text
                break
            emit(f"The check did not confirm the task was done; escalating to tier {index + 2}.")
            if tier_trace:
                messages.append({"role": "user", "content": _continuation_note(tier_trace)})
                tier_trace.clear()
            messages.append({"role": "user", "content": f"[Verifier checked the previous attempt and found it not actually finished: {verified_text.replace(ESCALATION_SENTINEL, '').strip()}]"})
            continue

        emit(f"Tier {index + 1} ({model}) did not manage; moving on to the next tier.")
        log_event("engine", "small_model_tier_escalating", tab_id=tab_id, tier=index, model=model, trace_calls=len(tier_trace))
        if tier_trace:
            messages.append({"role": "user", "content": _continuation_note(tier_trace)})
            tier_trace.clear()

    if not final_text:
        # No tier ever produced a usable candidate (all stalled/errored/escalated
        # mechanically) -- one last, non-escalating verification pass so the user
        # still gets a real, honest answer instead of silence.
        try:
            verified_text, _ = await verify_candidate(final_text, can_escalate=False, trace=tier_trace)
        except _FundsExhaustedGivingUp as exc:
            log_event("engine", "small_model_funds_exhausted_giving_up", tab_id=tab_id, tier=None, model=None, reason=exc.reason)
            return _answer(_funds_exhausted_final_answer(exc.reason))
        final_text = verified_text if _usable(verified_text) else _UNAVAILABLE_ANSWER

    log_event("engine", "small_model_turn_answered", tab_id=tab_id, text_len=len(final_text))
    return _answer(final_text)
