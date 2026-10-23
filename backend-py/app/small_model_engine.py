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
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from app import session_context, sw_tier_stats
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
from app.plugins.sw_api import CAROLINE_SW_KEY, _post_json, clear_funds_exhausted, mark_funds_exhausted
from app import policies
from app.policies import (
    continuity_pointer_instruction,
    recent_dialogue_history_instruction,
)

# Per explicit instruction (2026-09-13), after a real capability audit
# ("проверь, что маленькая модель имеет ВСЁ, что имеет полный путь"): every
# ALWAYS_ON_INSTRUCTIONS entry (policies.py) that doesn't assume a Claude-
# Code-CLI-native affordance this engine structurally lacks (Bash's own
# run_in_background/BashOutput, the Task subagent tool, TodoWrite, or the
# multi-bubble live narration the SDK path's own streaming turn produces).
#
# Rewritten (2026-10-15), per explicit instruction, after a real incident
# traced to this list's OWN lie: this comment has always claimed the list
# below is "ALWAYS_ON_INSTRUCTIONS minus exactly the entries that reference
# a tool this engine doesn't have, so a future addition is included here
# automatically" -- but the list used to be a plain hand-written tuple that
# never actually read ALWAYS_ON_INSTRUCTIONS at all, so that claim was false.
# Four entries had silently drifted out and were simply never noticed:
# no_update_sentinel_instruction (sw's own output IS checked for the
# [[NO_UPDATE]] sentinel -- chat_session.py's _finish_small_model_turn_
# answered -- so sw genuinely needs to know the convention, unlike the other
# CLI-only exclusions below), prefer_command_line_and_scripting_instruction
# (almost entirely generic -- only references bash_background_instruction
# in passing), document_opening_preference_instruction (open_in_viewer vs
# open_file, both ordinary plugin tools, no CLI dependency at all), and
# no_unrequested_send_confirmation_instruction (a general conversational
# rule, added 2026-10-13, postdating this list's last manual curation).
# Rewritten as an actual derivation so that gap can't recur silently --
# the direction of error changes from "silently missing" (unsafe) to
# "silently included" (safe default; a genuinely CLI-only future addition
# needs a human to notice and add it to the maps below, same effort as
# before, but now an OMISSION is the thing that requires a conscious
# decision, not an inclusion).
_SW_VEHICLE_SUBSTITUTIONS: dict[Callable[[], str], Callable[[], str]] = {
    # complex_task_execution_instruction's non-core bullets reference Task/
    # TodoWrite/run_in_background by name -- not just "a CLI affordance sw
    # lacks" the way the four entries below are, but a genuinely different
    # VEHICLE for the same underlying advice (decompose, don't do a big task
    # as one undifferentiated blob). sw has its own real vehicle for exactly
    # this (declare_plan's "parallel" stage + run_parallel_stage -- see this
    # real incident: a sw-mode turn asked to check every mailbox called
    # request_info once and answered as if done, no escalation, because it
    # never saw ANY decompose-first framing in any form), so it gets a
    # substituted variant instead of a plain exclusion. See
    # complex_task_execution_instruction_for_sw's own docstring.
    policies.complex_task_execution_instruction: policies.complex_task_execution_instruction_for_sw,
}
_CLI_NATIVE_ONLY_EXCLUSIONS: dict[Callable[[], str], str] = {
    policies.bash_background_instruction: "no backgrounding primitive of its own (no run_in_background/BashOutput)",
    policies.compact_continuation_is_genuine_instruction: "refers to Claude Code's own auto-compaction, which this single resolve_agentic() call never goes through",
    policies.usage_cap_self_awareness_instruction: "refers to the Claude subscription usage cap; this engine has its own OpenRouter funds-exhausted handling (mark_funds_exhausted/get_funds_exhausted_reason)",
    policies.progress_narration_instruction: "covered at the engine level already (emit()/on_activity-driven progress narration), not something the model's own text needs to produce",
}


def _derive_shared_always_on_instructions() -> tuple[Callable[[], str], ...]:
    return tuple(
        _SW_VEHICLE_SUBSTITUTIONS.get(fn, fn)
        for fn in policies.ALWAYS_ON_INSTRUCTIONS
        if fn not in _CLI_NATIVE_ONLY_EXCLUSIONS
    )


_SHARED_ALWAYS_ON_INSTRUCTIONS = _derive_shared_always_on_instructions()

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

# Per explicit instruction (2026-10-15): the inverse of ESCALATION_SENTINEL --
# the verifier's own judgment that what's LEFT to do, specifically, is simple
# enough for a weaker tier, not a mechanical signal. Only the verifier emits
# this (see _build_verification_prompt/verify_candidate); a tier's own worker
# run never does, since there's no mid-task handoff point within a single
# resolve_agentic() call.
DEESCALATION_SENTINEL = "[[CAN_DEESCALATE]]"

# Anti-bounce safety valve for the de-escalation mechanic: caps how many times
# a single turn may step back down the ladder, so a tier A -> tier B -> tier A
# -> tier B cycle (B judges "simple enough for A", A then re-escalates back to
# B) cannot loop indefinitely -- same spirit as REPEATED_CALL_LIMIT below, just
# for cross-tier bouncing instead of same-tier tool-call repetition.
MAX_DEESCALATIONS = 2

# Per explicit instruction (2026-10-15), a real incident: a sw-mode turn asked
# to check every mailbox called request_info once and answered as if done --
# no escalation, no further tool calls, task silently abandoned. The gap:
# ESCALATION_SENTINEL/DEESCALATION_SENTINEL both hand the SAME unfinished work
# to a DIFFERENT model tier; neither covers the verifier judging that the SAME
# tier just stopped short (used tools fine, then quit before covering what was
# asked) rather than being genuinely incapable -- the correct response there is
# a same-tier retry with a concrete correction, not a model swap. Per explicit
# instruction on the fix's OWN philosophy ("не выжимать из маленьких моделей
# то, что они не могут... параллелить работу, если ее можно параллелить"):
# this sentinel's real job is NOT "try harder, do more sequential work" --
# it's specifically for the case where the missing work should have been
# split into a declare_plan "parallel" stage + run_parallel_stage and wasn't,
# which makes the retry genuinely EASIER for the same tier, not harder. See
# _looks_multi_target/_build_verification_prompt's own red-flag fact, which is
# what actually points the verifier at this diagnosis.
RETRY_SENTINEL = "[[RETRY_SAME_TIER]]"


# Per explicit instruction (2026-09-12): NOT a task-complexity budget --
# resolve_agentic()'s own doc comment documents this same thing. Set high
# enough that real use should NEVER hit it; the two escalation mechanisms
# above are what actually decide when to hand off, not a step count.
MAX_ITERATIONS = 200

# How many times the SAME (tool_name, json-args) pair may repeat before the
# executor treats this as a broken/looping run and aborts -- a mechanical
# check, unrelated to task complexity (see module docstring point 2).
REPEATED_CALL_LIMIT = 3

# Soft/semantic loop detection (2026-10-15), per explicit instruction following a
# real SW e2e test: a mailbox-sweep task had the model fetch a message uid from
# ONE account, then call email_get_message with that SAME uid against 7 OTHER
# accounts, failing "not found" every single time for several minutes straight --
# REPEATED_CALL_LIMIT above never caught it, since every one of those calls had a
# DIFFERENT "address" argument and so never hit the same exact call_key twice.
# This is the semantic layer REPEATED_CALL_LIMIT can't be: once the SAME tool
# NAME (regardless of its arguments) has been called this many times, a cheap
# model judges whether the real arguments/results across those calls form a
# stuck, unproductive pattern -- the same underlying mistake repeated against
# different inputs -- rather than legitimate different attempts (e.g. the exact
# same tool called once per account, each with its own real, different result,
# is NOT a loop). Purely additive: the exact-match check above still runs first
# and is free: this only ever adds coverage, never removes any.
SOFT_LOOP_CHECK_MIN_CALLS = 4
# Re-checked every this-many FURTHER calls past the minimum (4, 7, 10, ...)
# rather than on every single call once the minimum is crossed -- a genuine loop
# still gets caught quickly, but a long legitimate multi-account sweep (the exact
# shape of the task that found this gap) doesn't pay for an extra model call on
# every one of its own many, perfectly normal, same-tool-different-account calls.
SOFT_LOOP_RECHECK_INTERVAL = 3

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

# Per tier (2026-10-10) -- live evidence for the "reconsider" note above. At "high",
# gpt-5-nano spent 24-34s and 3.5-5.3k completion tokens on EVERY agentic step of a
# plain "what can you do" turn: 117s of a 164s voice turn. The cheap tiers run at
# low; a weak first attempt is still caught -- the verifier escalates an
# unfinished answer to the next tier -- and the last tier keeps REASONING_EFFORT.
TIER_REASONING_EFFORT = ({"effort": "low"}, {"effort": "low"}, REASONING_EFFORT)
# The verifier only checks a finished candidate against facts it is handed; at "high"
# it was another 7-10s on the end of every turn.
VERIFIER_REASONING_EFFORT = {"effort": "low"}
# One-word SIMPLE/COMPLEX call (_start_tier_index): with no reasoning parameter at
# all it ran at the model's default and took ~10s live before any real work began.
COMPLEXITY_REASONING_EFFORT = {"effort": "minimal"}

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
        # "stall" (no progress for STALL_TIMEOUT_S), "absolute" (ABSOLUTE_TURN_TIMEOUT_S
        # ceiling) or "parent" (whatever started this attempt was itself given up).
        self.kind = kind


# The cancellation flag of the resolve_agentic() attempt running on the current
# worker thread (2026-10-22). A tier attempt that stalls has only ITS flag set, so
# its abandoned thread stops acting while the next tier, on a fresh attempt, still
# works; tracker.cancelled stays the whole-turn flag (Stop, the absolute ceiling).
_attempt_local = threading.local()


def _attempt_cancelled() -> bool:
    flag = getattr(_attempt_local, "cancel", None)
    return flag is not None and flag.is_set()


class _AttemptAbandoned(BaseException):
    """Ends the worker thread of an attempt nobody waits for anymore (2026-10-22).
    resolve_agentic() catches every Exception from its tools and callbacks and goes
    on to the next model call, so a refusal alone kept an abandoned attempt calling
    the model -- seen on the test box: after nano and codex-mini were given up as
    stalled, both went on calling their models (~29,500 prompt tokens a call) until
    the backend was restarted. A BaseException is not caught there: raised from a
    tool call or a callback, it ends the loop, and at most the model call already
    under way is paid for."""


def _abandon_if_cancelled(tracker: "_TurnTracker", parent_cancels: tuple[threading.Event, ...] = ()) -> None:
    if tracker.cancelled.is_set() or _attempt_cancelled() or any(flag.is_set() for flag in parent_cancels):
        raise _AttemptAbandoned()


async def _resolve_agentic_with_watchdog(
    ai: Any, tracker: _TurnTracker, *, parent_cancels: tuple[threading.Event, ...] = (), **resolve_kwargs: Any,
) -> str:
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
    sends, logins, ...) once nobody is listening for its result anymore.

    A stall cancels only this attempt (2026-10-22): its thread's own flag is set,
    so the caller can hand the work to the next tier instead of ending the turn.
    parent_cancels: the flags of whatever started this attempt (a parallel-stage
    branch's turn and tier attempt) -- once one of them is set, this attempt is
    given up too, so an abandoned branch stops waiting and stops acting."""
    tracker.touch_progress()
    started = time.monotonic()
    attempt_cancel = threading.Event()
    tracker.current_attempt_cancel = attempt_cancel

    def run_attempt() -> str:
        _attempt_local.cancel = attempt_cancel
        try:
            return ai.resolve_agentic(**resolve_kwargs)
        except _AttemptAbandoned:
            log_event("engine", "small_model_attempt_thread_ended", model=resolve_kwargs.get("model"))
            return ""
        finally:
            _attempt_local.cancel = None

    task: asyncio.Task[str] = asyncio.create_task(asyncio.to_thread(run_attempt))
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
                attempt_cancel.set()
                raise
            if task in done:
                return task.result()
            now = time.monotonic()
            if any(flag.is_set() for flag in parent_cancels):
                attempt_cancel.set()
                raise _TurnStalled(now - started, "parent")
            # A tool call still waiting for its result is not a stall (2026-10-22, explicit
            # instruction): a memory lookup that took minutes had every tier given up on
            # in turn while it waited, nano to codex-mini to Sonnet, none of them stalled.
            # Such a call is bounded by its own timeout and ABSOLUTE_TURN_TIMEOUT_S.
            if tracker.tools_in_flight > 0:
                tracker.touch_progress()
            stalled_for = now - tracker.last_progress_at
            if stalled_for > STALL_TIMEOUT_S:
                attempt_cancel.set()
                raise _TurnStalled(stalled_for, "stall")
            if now - started > ABSOLUTE_TURN_TIMEOUT_S:
                tracker.cancelled.set()
                attempt_cancel.set()
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


# Per explicit instruction (2026-10-22: "Действие. Вопросы разрешены только при полной
# невозможности действий"). Seen the same day: asked to find a letter in a mailbox whose
# credentials were saved, nano asked for the credentials and codex-mini asked to confirm
# using the account -- neither called a tool, and the task climbed to the top tier.
_ACT_DO_NOT_ASK_INSTRUCTION = (
    "ACT, DO NOT ASK. This is the most important rule you have. Do the task with your tools. Your first "
    "move is a tool call, never a question to the user.\n"
    "- Never ask the user for permission to read, search, open, look up or check anything.\n"
    "- Never ask for something you can find yourself. When something is MISSING -- a password, a server, a "
    "port, an address, an account, earlier work, anything the user told you before: call request_info first -- "
    "that is your memory. When the task already gives you everything it needs, do not search memory at all: "
    "start the work. "
    "A mailbox the user has used before needs only its address: the email tools fill in its saved "
    "password and servers themselves, so just call them with the address. Files and folders: look at them.\n"
    "- Never hand the user a list of options instead of doing the work; when one way fails, try another.\n"
    "- Ask the user only when the action is genuinely impossible without them -- a fact only they know, "
    "after request_info and every tool you have came back without it.\n"
    "(Whether a message or payment on the user's behalf needs their approval first is checked separately "
    "when you send it; when you are told it does, ask for exactly that.)"
)

# Added to every retry nudge (2026-10-22, explicit instruction: a weak model may ask anything; the
# check catches it and tells it where to look). Seen the same day: twice nano answered a mailbox
# task by asking for the password and servers, with the credentials saved and no tool ever called.
_LOOK_IT_UP_FIRST = (
    "If you were about to ask the user for something, look it up yourself first: call request_info "
    "(your memory) for credentials, addresses, earlier work or anything the user said before; call the "
    "email tools with just the mailbox address (they fill in its saved password and servers); look at "
    "the files. Ask the user only if all of that comes back without it."
)


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


_SOFT_LOOP_PROMPT = (
    "An assistant with tools is calling the SAME tool repeatedly, with different arguments each time. Below are "
    "its last attempts at this tool -- the real arguments and the real result, oldest first. Judge: is this a "
    "STUCK, UNPRODUCTIVE pattern -- the same underlying mistake repeated against different inputs, with no real "
    "progress -- or are these legitimate, meaningfully different attempts (e.g. the same tool used once per "
    "account/item, each with its own distinct real outcome)? Reply with exactly one word: LOOP or PROGRESS.\n\n"
)


def _check_soft_repeat_loop(tool_name: str, history: list[str], ai: Any, cfg: Any) -> str | None:
    """Returns a reason string if `history` (this tool name's own real calls so
    far, oldest first) looks like a stuck/unproductive loop, else None. Never
    raises into the turn -- a failed classification call just means no soft-loop
    signal this time, same as every other "never a hard dependency" check in
    this file (see SOFT_LOOP_CHECK_MIN_CALLS's own comment for the incident this
    closes)."""
    prompt = _SOFT_LOOP_PROMPT + f"Tool: {tool_name}\n\n" + "\n".join(history[-8:])
    try:
        raw = ai.resolve(prompt, model=cfg.AI_MODEL_SMALL, no_fallback=True)
    except Exception as exc:  # noqa: BLE001 -- classification must never block the turn
        log_event("engine", "small_model_soft_loop_check_failed", tool=tool_name, error=str(exc), error_type=type(exc).__name__)
        return None
    verdict = str(raw or "").strip().upper()
    if verdict.startswith("LOOP"):
        return f"tool '{tool_name}' called {len(history)} times with different arguments but the same underlying mistake each time -- likely a stuck loop"
    return None


# web_search really searches, or says it could not (2026-10-11, per explicit instruction:
# the main model must have real access to the web through OpenRouter's own mechanism).
# It did go through ":online" before -- via AI.resolveOnline, which on ANY exception
# silently asks the same model again WITHOUT the web. That model then answers "I can't
# access live weather data", and this came back as the search result: live, the same
# question about tomorrow's weather took 41-180s, once ended in "нет доступа к живым
# данным", and once sent the tier to the browser and the command line. Probed with
# Caroline's own key the same day, the search itself works: the web plugin with the Exa
# engine returns in 3.2-3.8s for $0.007, with the pages it read.
#
# So the call is made here, where its outcome can be seen: Exa, because it returns its
# sources (url, title, excerpt) and a reply without a single source is then known not to
# be grounded; the sources themselves go to the main model, not only another model's
# retelling of them; and a failure is reported as a failure.
WEB_SEARCH_ENGINE = "exa"
WEB_SEARCH_MAX_RESULTS = 5
WEB_SEARCH_TIMEOUT_S = 40.0
WEB_SEARCH_EXCERPT_CHARS = 700
WEB_SEARCH_REASONING = {"effort": "minimal"}
_WEB_SEARCH_SYSTEM = (
    "Today is {today}. Answer ONLY from the web search results you are given: state figures exactly as the "
    "sources do, with their units, and say which date each one is for. If the results do not contain the "
    "answer, say so plainly -- never fill in from general knowledge or typical values."
)


# Where the model is called from.
#
# Per explicit instruction (2026-10-11): "Кэролайн не должна НИ В КАКОМ СЛУЧАЕ напрямую
# обращаться к OpenRouter и тем более сама получать ключ -- вызов модели должен идти через
# SquirrelWisdom", and (2026-10-12) through ai:resolveAgenticStep specifically -- the
# server's own tool-calling step command -- which is to be extended where it lacks
# something, rather than a second route built beside it.
#
# What exists since 2026-09-12 is the opposite: ai:getModelKey hands this process the
# provider key, and Camerlengo's loop (AI.resolve_agentic) calls the provider with it.
# This branch added more callers of the same kind (fast lane and its gate, the narrator's
# ladder, the web-search plugin call, the speech-recognition check). All of them now get
# their AI object from _model_ai(), so there is one switch for the lot.
#
# With the switch on, the loop stays Camerlengo's and the tools still run here, but the
# adapter under the loop is _SquirrelWisdomStepAdapter: each model call is one
# ai:resolveAgenticStep request made with the user's own session, and no provider key is
# fetched or held by this process at all.
#
# The switch is OFF until the server side is in place; turned on today, nothing answers:
#   1. Caroline's API key is refused the command -- errcode 997, "Key scope/resource ACL
#      does not permit this call" (probed 2026-10-11 and again 2026-10-12);
#   2. the command forwards neither reasoning, transforms, plugins, max_tokens nor
#      temperature, and returns neither annotations (web-search sources) nor usage. The
#      change for it was handed over as a patch against reforce's microagents branch
#      (resolveAgenticStep-options.patch); this adapter already speaks that form, and a
#      server without it simply ignores the extra fields.
# With both done: set this to True, and ai:getModelKey has no caller left.
MODEL_CALLS_VIA_SQUIRRELWISDOM = False
SW_STEP_TIMEOUT_S = 300.0
# login_api.get_v2_session() logs in afresh on every call (0.5-0.8s): one session serves
# the calls of the next minute, and a call refused for its session gets a new one.
SW_SESSION_REUSE_S = 60.0
_sw_session: str | None = None
_sw_session_at = 0.0
_SW_ERRCODE_SESSION = "202"
_SW_ERRCODE_FUNDS = "402"


def _step_as_response(step: dict[str, Any]) -> Any:
    """ai:resolveAgenticStep's reply in the shape AI.resolve_agentic reads off a provider
    response: choices[0].message (.content, .tool_calls[].id / .function.name /
    .function.arguments, .annotations) and .usage."""
    from types import SimpleNamespace as NS

    usage = step.get("usage")
    usage_ns = None
    if isinstance(usage, dict):
        usage_ns = NS(
            prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"), cost=usage.get("cost"),
            prompt_tokens_details=NS(cached_tokens=usage.get("cached_tokens")),
        )
    if step.get("type") == "tool_calls":
        calls = [
            NS(id=call.get("id"), type="function", function=NS(name=call.get("name"), arguments=call.get("arguments") or "{}"))
            for call in step.get("calls") or []
        ]
        message = NS(content=(step.get("assistant_message") or {}).get("content") or "", tool_calls=calls, annotations=None)
    else:
        message = NS(content=step.get("text") or "", tool_calls=None, annotations=step.get("annotations") or None)
    return NS(choices=[NS(message=message)], usage=usage_ns)


class _SquirrelWisdomStepAdapter:
    """Stands where Camerlengo's OpenRouterAdapter stands under AI.resolve_agentic():
    the same _resolve(), answered by the server's ai:resolveAgenticStep.

    _resolve is called on the worker thread resolve_agentic runs in. The request itself
    is made on the main loop (sw_api._post_json is a coroutine), which is free at that
    moment: whoever started the loop is awaiting that very thread."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _on_loop(self, coro: Any, timeout: float) -> Any:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            coro.close()
            raise RuntimeError("a model call must not be made on the event loop itself")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _session(self, fresh: bool = False) -> str:
        global _sw_session, _sw_session_at
        if fresh or _sw_session is None or time.monotonic() - _sw_session_at > SW_SESSION_REUSE_S:
            from app.login_api import get_v2_session

            _sw_session = self._on_loop(get_v2_session(), 30.0)
            _sw_session_at = time.monotonic()
        return _sw_session

    def _resolve(
        self, model: str, messages: list[dict[str, Any]], max_tokens: int | None = None, temperature: float = 0.2,
        transforms: list[str] | None = None, stop: Any = None, tools: list[dict[str, Any]] | None = None,
        reasoning: dict[str, Any] | None = None, timeout: float | None = None, plugins: list[dict[str, Any]] | None = None,
    ) -> Any:
        body: dict[str, Any] = {
            "command": "ai:resolveAgenticStep", "key": CAROLINE_SW_KEY, "messages": messages, "model": model,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
        if transforms is not None:
            body["transforms"] = transforms
        if reasoning:
            body["reasoning"] = reasoning
        if plugins:
            body["plugins"] = plugins
        if max_tokens:
            body["max_tokens"] = max_tokens
        wait = float(timeout or SW_STEP_TIMEOUT_S)
        data: Any = None
        for attempt in (1, 2):
            body["session"] = self._session(fresh=attempt == 2)
            data = self._on_loop(_post_json(body, timeout=wait), wait + 10.0)
            expired = isinstance(data, dict) and data.get(".status") == "error" and str(data.get(".errcode")) == _SW_ERRCODE_SESSION
            if not expired:
                break
        if not isinstance(data, dict) or data.get(".status") != "ok":
            code = str(data.get(".errcode")) if isinstance(data, dict) else "?"
            reason = data.get(".reason") if isinstance(data, dict) else "no reply"
            if code == _SW_ERRCODE_FUNDS:
                # The words the engine's own funds handling looks for (_call_with_funds_wait).
                raise RuntimeError(f"SquirrelWisdom returned 402 Insufficient funds: {reason}")
            raise RuntimeError(f"SquirrelWisdom ai:resolveAgenticStep failed (errcode {code}): {reason}")
        return _step_as_response(data)


async def _model_ai() -> Any | None:
    """The AI object every model call in this file is made with -- see the note above
    MODEL_CALLS_VIA_SQUIRRELWISDOM. None when there is no way to call a model right now
    (Camerlengo missing; or, on the old route, not logged in / no key)."""
    if camerlengo_ai is None:
        return None
    if MODEL_CALLS_VIA_SQUIRRELWISDOM:
        return camerlengo_ai.AI(adapter=_SquirrelWisdomStepAdapter(asyncio.get_running_loop()))
    api_key = await get_model_provider_key()
    if not api_key:
        return None
    # base_url points this at Camerlengo's own /v1/chat/completions proxy
    # (not openrouter.ai directly) -- see model_key_provisioning.py's own
    # docstring: api_key here is the composite "<CAROLINE_SW_KEY>.<session>"
    # token that proxy expects, not a real OpenRouter key.
    return camerlengo_ai.AI(adapter=camerlengo_ai.OpenRouterAdapter(
        api_key=api_key, base_url="https://www.squirrelwisdom.com/v1"))


async def prewarm_model_access() -> None:
    """What the first model call would otherwise wait for, done ahead: the login, and on
    the old route the key (2026-10-10: the first fast-lane call after a start took 4.9s
    instead of ~1.8s for exactly that)."""
    if MODEL_CALLS_VIA_SQUIRRELWISDOM:
        adapter = _SquirrelWisdomStepAdapter(asyncio.get_running_loop())
        await asyncio.to_thread(adapter._session)
    else:
        await get_model_provider_key()


def _web_search_once(question: str, ai: Any, cfg: Any) -> tuple[str, list[dict[str, Any]]]:
    """(the search model's answer, the pages it was given). Raises on any API failure."""
    search_messages = [
        {"role": "system", "content": _WEB_SEARCH_SYSTEM.format(today=time.strftime("%A, %Y-%m-%d"))},
        {"role": "user", "content": question},
    ]
    search_plugins = [{"id": "web", "engine": WEB_SEARCH_ENGINE, "max_results": WEB_SEARCH_MAX_RESULTS}]
    if isinstance(ai.adapter, _SquirrelWisdomStepAdapter):
        via_server = ai.adapter._resolve(
            model=cfg.AI_MODEL_SMALL, messages=search_messages, max_tokens=1200, reasoning=WEB_SEARCH_REASONING,
            plugins=search_plugins, timeout=WEB_SEARCH_TIMEOUT_S,
        ).choices[0].message
        found = [a.get("url_citation") or {} for a in (via_server.annotations or []) if isinstance(a, dict) and a.get("type") == "url_citation"]
        return str(via_server.content or "").strip(), [s for s in found if s.get("url")]
    response = ai.adapter.client.with_options(timeout=WEB_SEARCH_TIMEOUT_S, max_retries=0).chat.completions.create(
        model=cfg.AI_MODEL_SMALL,
        messages=[
            {"role": "system", "content": _WEB_SEARCH_SYSTEM.format(today=time.strftime("%A, %Y-%m-%d"))},
            {"role": "user", "content": question},
        ],
        max_tokens=1200,
        extra_body={
            "plugins": [{"id": "web", "engine": WEB_SEARCH_ENGINE, "max_results": WEB_SEARCH_MAX_RESULTS}],
            "reasoning": WEB_SEARCH_REASONING,
        },
    )
    message = response.model_dump()["choices"][0]["message"]
    sources = [a.get("url_citation") or {} for a in (message.get("annotations") or []) if a.get("type") == "url_citation"]
    return str(message.get("content") or "").strip(), [s for s in sources if s.get("url")]


def _run_web_search_tool(name: str, args: dict[str, Any], ai: Any, cfg: Any) -> str:
    question = str(args.get("question") or "").strip()
    if not question:
        return "ERROR: 'question' is required."
    # One retry, as before (2026-10-03: the first online call sometimes comes back empty
    # while an identical immediate retry succeeds).
    reason = "no result"
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            answer, sources = _web_search_once(question, ai, cfg)
        except Exception as exc:  # noqa: BLE001 -- a failed search is a tool result, not a turn-ending error
            reason = f"{type(exc).__name__}: {exc}"[:300]
            log_event("engine", "small_model_web_search_failed", attempt=attempt, error=reason,
                      ms=int((time.monotonic() - started) * 1000))
            continue
        log_event("engine", "small_model_web_search", attempt=attempt, sources=len(sources), answer_len=len(answer),
                  ms=int((time.monotonic() - started) * 1000), question=question[:200])
        if not sources:
            reason = "the search returned no pages"
            continue
        lines = [f"Web search results for: {question}"]
        for index, source in enumerate(sources, 1):
            excerpt = " ".join(str(source.get("content") or "").split())[:WEB_SEARCH_EXCERPT_CHARS]
            lines.append(f"{index}. {source.get('title') or '(untitled)'} -- {source['url']}" + (f"\n   {excerpt}" if excerpt else ""))
        if answer:
            lines.append("Summary by the search model (it read the same pages; where it differs from them, trust the pages):\n" + answer)
        return "\n".join(lines)
    return (
        f"ERROR: the web search did not work ({reason}); nothing was found out. Do not answer from memory as if it "
        "were current -- say that the search failed, or get the information another way."
    )


@dataclass
class _TurnTracker:
    """Per-turn (not per-registry) mutable state -- one instance per
    run_small_model_turn() call, threaded through _make_executor_fn() so
    the executor can update it as tool calls actually happen. Read by the
    verification pass (see _build_verification_prompt)."""

    tool_call_count: int = 0
    # Computed once at the start of run_small_model_turn() from the user's own
    # request text (_looks_multi_target) -- a mechanical fact for the verifier
    # (_build_verification_prompt), not something a tool call sets. See that
    # function's own comment for the real incident this closes off.
    looks_multi_target: bool = False
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
    # The flag of the tier attempt currently running (see _attempt_local), so a
    # parallel stage started from it can hand that flag to its branches.
    current_attempt_cancel: threading.Event | None = None
    # Plugins whose usage instructions this turn already got (see executor_fn).
    instructed_plugins: set[str] = field(default_factory=set)
    # Tool calls of this turn now running (2026-10-22): a call still waiting for its
    # result is not a stall -- see _resolve_agentic_with_watchdog.
    tools_in_flight: int = 0
    # The task as the changing-action check reads it (the formalized request, else the
    # user's words) and the latest calls of the turn, for the same check.
    task_text: str = ""
    recent_calls: list[str] = field(default_factory=list)

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
    system_text: str, user_text: str, model: str, recorder: Any = None,
) -> str:
    if tracker.plan_stages is None or not (0 <= stage_index < len(tracker.plan_stages)):
        return f"ERROR: the declared plan has no stage {stage_index}."
    mode, indices = tracker.plan_stages[stage_index]
    if mode != "parallel":
        return f"ERROR: stage {stage_index} is sequential -- do its steps yourself, one at a time."
    if stage_index in tracker.parallel_stages_run:
        return f"ERROR: stage {stage_index} already ran."
    tracker.parallel_stages_run.add(stage_index)
    # The turn's flag and the flag of the tier attempt that started this stage: if
    # either is set, every branch stops (see _resolve_agentic_with_watchdog).
    parent_cancels = tuple(flag for flag in (tracker.cancelled, tracker.current_attempt_cancel) if flag is not None)

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
                base_executor = _make_executor_fn(
                    registry, tab_id, send, main_loop, branch_tracker, ai, cfg,
                    parent_cancels=parent_cancels, parent_tracker=tracker,
                )
                if recorder is not None:
                    base_executor = recorder.wrap(base_executor, skip=_PLAN_TOOL_NAMES)
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
                            ai, branch_tracker, parent_cancels=parent_cancels,
                            messages=branch_messages, tool_defs=branch_tools, executor_fn=traced_executor,
                            model=model, max_iterations=MAX_ITERATIONS,
                            transforms=DISABLE_MIDDLE_OUT, reasoning=REASONING_EFFORT,
                            app="caroline",
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
    on_tool_start: Callable[[str, dict[str, Any]], None] | None = None,
    parent_cancels: tuple[threading.Event, ...] = (),
    parent_tracker: _TurnTracker | None = None,
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
    # Per-tool-name (not per exact-args) call history for the soft loop check
    # below -- see SOFT_LOOP_CHECK_MIN_CALLS's own comment.
    tool_call_history: dict[str, list[str]] = {}

    def executor_fn(name: str, args: dict[str, Any]) -> str:
        args = args or {}
        # The turn, this attempt, or -- for a parallel-stage branch -- whatever
        # started it (parent_cancels, 2026-10-22: an abandoned branch used to keep
        # acting after its turn ended, e.g. a save_info after the reply went out).
        # This turn's own watchdog already gave up waiting on us (a genuine stall,
        # see STALL_TIMEOUT_S) and reported that to our caller -- nobody is listening
        # for this call's real result anymore. Never dispatch (a real email send, an
        # IMAP login with a real password, etc.) unsupervised after the fact, and end
        # the thread itself (_AttemptAbandoned) -- a refusal alone kept it calling the
        # model.
        _abandon_if_cancelled(tracker, parent_cancels)
        tracker.touch_progress()
        tracker.tools_in_flight += 1
        try:
            return _execute(name, args)
        finally:
            tracker.tools_in_flight -= 1
            tracker.touch_progress()

    def _execute(name: str, args: dict[str, Any]) -> str:
        if parent_tracker is not None:
            # A branch's work is progress of the tier that is waiting on the stage
            # (2026-10-22: the waiting tier was declared stalled at 120s while its
            # branches were busy).
            parent_tracker.touch_progress()
        call_key = (name, json.dumps(args, sort_keys=True, default=str))
        seen_calls[call_key] = seen_calls.get(call_key, 0) + 1
        count = seen_calls[call_key]
        log_event("engine", "small_model_tool_call", tab_id=tab_id, tool=name, args=args, repeat_count=count)
        # Ported from a teammate's branch (afb8e0f): the narrator used to
        # learn about a tool call only once it RETURNED -- live, the model's
        # own thinking plus a web search left it nothing to say for ~16s.
        # A failure here must never cost the actual call.
        if on_tool_start is not None:
            try:
                on_tool_start(name, args)
            except Exception:  # noqa: BLE001
                pass
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
        task_text = (parent_tracker or tracker).task_text
        if task_text and _changes_something(name, plugin_tool.description, plugin_tool.input_schema, tab_id):
            # Not part of the task -> not done (2026-10-22, explicit instruction: a checker
            # for actions nobody asked for). See app/microagents/sw_action_scope.py.
            in_scope, why = _action_in_scope(task_text, name, args, list(tracker.recent_calls), tab_id)
            if not in_scope:
                return (
                    f"NOT DONE: {name} is not part of the user's task ({why}). Do only what the task asks; "
                    "do not create, move, delete, rename or change anything else."
                )
            if tracker.recent_calls:
                consistent, use_account, why_account = _account_consistent(task_text, name, args, list(tracker.recent_calls), tab_id)
                if not consistent:
                    return (
                        f"NOT DONE: {name} would act as a different identity than the rest of this work ({why_account}). "
                        f"Use the same one ({use_account}) -- an answer goes out from the identity that received the "
                        "message -- unless the task itself says to act as the other."
                    )
            grounded, invented = _content_grounded(task_text, name, args, list(tracker.recent_calls), tab_id)
            if not grounded:
                return (
                    f"NOT DONE: the content of this {name} call states things the task and your results do not "
                    f"support: {invented}. Remove them -- write only what the task gave you and what your tool "
                    "results showed (totals and comparisons of the given numbers are fine) -- and call again."
                )

        # The checks above take seconds, and the turn can be cancelled meanwhile (Stop, a
        # closed session, a given-up attempt): a call is never made after that (2026-10-23;
        # seen on the test box: a stopped turn's run_python still ran 5 s after the stop --
        # for a send it would have been a letter nobody wanted any more).
        _abandon_if_cancelled(tracker, parent_cancels)

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
            text = f"ERROR: {envelope.get('error')}{note}"
        elif envelope.get("status") == "cancelled":
            text = f"ERROR: this tool call was cancelled before it finished.{note}"
        elif envelope.get("status") == "running":
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
            text = f"Operation {op_id} is still running."
        else:
            result = envelope.get("result")
            # Bug fix (2026-10-14), confirmed live: the established plugin-handler
            # contract (every plugin returns {"text": ...}, see loader.py's own
            # _envelope_to_mcp_response for the SDK/Claude path's equivalent) means
            # `result` is almost always a dict whose "text" value is the string the
            # model should read -- and for memory_plugin's structured tools
            # (request_info in particular), that string is itself ALREADY a
            # JSON-formatted blob. json.dumps()-ing the whole dict here re-escapes
            # that already-JSON string a second time, producing a backslash-heavy
            # mess a cheap model (this engine runs tier-1 on openai/gpt-5.1-codex-
            # mini) could not parse -- confirmed live: a real IEEE-credentials
            # lookup had its answer sitting right there in the tool result, but the
            # model told the user it "couldn't be extracted". Extracting the plain
            # "text" string, same as the SDK path effectively does, removes that
            # extra escaping layer entirely.
            if isinstance(result, str):
                text = result
            elif isinstance(result, dict) and isinstance(result.get("text"), str):
                text = result["text"]
            else:
                text = json.dumps(result, ensure_ascii=False, default=str)
            text = text + note

        # A plugin's usage instructions come with the result of its first tool call in
        # the turn (2026-10-22, per explicit instruction) -- still on demand, never in the
        # system prompt, but no longer depending on the model asking for them: on the
        # test box no small model ever called get_tool_instructions, and an email task
        # left the mailbox password unsaved because the plugin's own rule for that was
        # never read.
        instructions = registry.tool_instructions.get(name)
        instructed = (parent_tracker or tracker).instructed_plugins
        if instructions and plugin_name not in instructed:
            instructed.add(plugin_name)
            text += (
                f"\n\n[How the {plugin_name} tools are meant to be used -- shown once, with this plugin's first "
                f"call in this turn:\n{instructions}]"
            )

        history = tool_call_history.setdefault(name, [])
        history.append(f"- args={json.dumps(args, ensure_ascii=False, default=str)[:200]} -> {text[:200]}")
        call_count = len(history)
        if call_count >= SOFT_LOOP_CHECK_MIN_CALLS and (call_count - SOFT_LOOP_CHECK_MIN_CALLS) % SOFT_LOOP_RECHECK_INTERVAL == 0:
            loop_reason = _check_soft_repeat_loop(name, history, ai, cfg)
            if loop_reason is not None:
                log_event("engine", "small_model_soft_loop_detected", tab_id=tab_id, tool=name, call_count=call_count, reason=loop_reason)
                raise NeedsEscalation(loop_reason)
        return text

    return executor_fn


def _build_verification_prompt(
    tracker: _TurnTracker, final_text: str, can_escalate: bool, tier_trace: list[str] | None = None,
    can_deescalate: bool = False, can_retry: bool = False, evidence: str = "",
) -> str:
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
    cleared.

    `can_deescalate` (2026-10-15, "если продолжение по мнению самой модели может
    быть сделано более простой моделью - спускаемся на один уровень по лестнице"):
    True for every tier except the first one actually tried this turn. The mirror
    image of `can_escalate` -- unfinished work isn't automatically harder than
    what was just tried; sometimes a stronger tier already did the hard part and
    what's left is mechanical.

    `can_retry` (2026-10-15, RETRY_SENTINEL's own real incident): True for a tier's
    first attempt and after every attempt that made progress (since 2026-10-22,
    see the tier loop). Distinct from escalating
    up: the verifier picks this when the gap looks like the SAME tier simply
    stopped short (it used tools fine for part of the work, then quit before
    covering the rest of what was named) rather than being unable to reason
    through it -- most often because repeated work across independent targets
    should have been split into a declare_plan "parallel" stage + run_
    parallel_stage and wasn't (see tracker.looks_multi_target's own red flag,
    added unconditionally below). Mutually exclusive with the other three
    outcomes for any given verifier reply: the verifier picks exactly one of
    "still hard, escalate up", "what's left is simple, step down", "this tier
    stopped short, retry it with what's missing", or "it's actually done"."""
    parts = [
        "The reply above is from another assistant that worked on the user's request. You are a verifier only: "
        "you have no tools and must not try to do or finish any work. Check the reply against the facts below."
    ]
    if tier_trace:
        parts.append(
            "Real tool calls made while producing that reply, with their real results (this is ground truth -- "
            "the reply above is a separate, later summary of this, not the source of it):\n" + _trace_text(tier_trace, 40)
        )
    if tracker.branch_trace:
        parts.append(
            "Parallel-stage branches, each with its own real tool calls and results (ground truth, same rule):\n"
            + "\n".join(tracker.branch_trace[-40:])
        )
    if evidence:
        parts.append(
            "The files this task was to make, READ BY THE SYSTEM ITSELF just now -- what is in them at this "
            "moment (ground truth; the system read them, so nobody has to read them back):\n" + evidence
        )
    if not tier_trace and not tracker.branch_trace:
        # Said on every pass, not only the last one (2026-10-22): its absence read as
        # "nothing to check", and "Подключаю почту ..., проверю соединение и сообщу"
        # passed as a finished answer with no tool ever called.
        parts.append(
            "No tool calls were made in this attempt at all. So any action the reply says is done, is being "
            "done right now, or is about to be done (connecting, checking, saving, sending, \"I'll ...\") did "
            "NOT happen. And a reply that asks the user for something the assistant could have found or tried "
            "itself with its tools -- saved credentials, a file, a mailbox, what is in memory -- is not finished "
            "either: nothing was tried yet -- say in your one sentence where to look instead of asking: "
            "request_info (memory) for credentials, addresses or anything said before, the email tools with "
            "just the mailbox address, the files. (2026-10-22: asked to check a mailbox whose credentials were already "
            "saved, the reply asked which credentials to use, with no tool ever called.)"
        )
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
    # Per explicit instruction (2026-10-15), a real incident: unconditional (not
    # gated on can_escalate/can_retry, not dependent on a plan ever having been
    # declared) -- the absence of a parallel stage on an apparently multi-target
    # request has to be a mechanical fact the verifier is TOLD, same as undone
    # plan steps or unconfirmed operations above, not something it has to notice
    # by its own reading comprehension alone (which is exactly what missed the
    # real incident this closes off).
    if tracker.looks_multi_target and not tracker.parallel_stages_run and len(tier_trace or []) <= 2:
        parts.append(
            "This request appears to name multiple independent targets or items to go through one by one "
            "(e.g. several accounts, mailboxes, files, or addresses), but no parallel plan stage was ever run "
            "(declare_plan with a \"parallel\" stage, then run_parallel_stage), and only "
            f"{len(tier_trace or [])} real tool call(s) were made. Do not take a plausible-sounding reply as "
            "proof the task is finished -- check specifically whether it accounts for EVERY target the request "
            "named, with real results for each, not just one. The most likely explanation for a trace this "
            "shallow is that the task needed to be split into a parallel stage (one step per target) and "
            "wasn't -- not that it is impossible."
        )
    # The verifier's reply is a verdict a program reads, in one of up to four forms
    # (2026-10-11, agreed: "OK / эскалация / исправленный текст в теге"; de-escalation
    # added 2026-10-15, same shape as escalation -- see DEESCALATION_SENTINEL's own
    # comment). Until then it was told to write the final message itself, "even if the
    # earlier reply was already fine" -- and four times in two days what it wrote was
    # its opinion OF the reply, shown or read out to the user as the answer (see
    # _looks_like_verdict). A good reply now goes out as the tier wrote it; the verifier
    # writes text only when the reply has to change, and then inside a tag, so a comment
    # outside it is never the answer.
    has_candidate = bool(final_text.strip())
    forms = []
    if has_candidate:
        forms.append(
            f'- "{VERIFIER_OK}" -- the reply above answers the request, claims nothing the facts above do not show, '
            "and can go to the user exactly as it stands. It is then sent word for word. This is the normal outcome "
            "for a good reply: never rewrite a reply that is already fine. Never OK a reply that only announces or "
            "promises the work (\"подключаю\", \"сейчас проверю\", \"I'll send it\") when the request was to do it "
            "and the facts above show no tool call that did it -- that task is NOT finished. "
            + _RESULT_READ_BACK_RULE
            + _REQUEST_VALUES_RULE
            + (
                " Never OK a reply that asks the user something -- which option to take, what to do next, a "
                "detail, a confirmation -- or hands them a choice instead of finishing the task: escalate it, so a "
                "stronger model tries to finish the task itself. The only question that goes to the user as it "
                "is: asking them to approve the exact text of a message (or a payment) about to be sent on their "
                "behalf, once everything else is done."
                if can_escalate else ""
            )
        )
    if can_escalate:
        # Per explicit instruction (2026-10-22): while a stronger tier exists, a question to
        # the user is not an answer -- the stronger tier gets the task instead. Seen the same
        # day: a tool error ("can't search in Cyrillic") came back as a list of options for
        # the user, though reading the latest messages without a search would have found the
        # letter. The one question that does go out: confirming a send on the user's behalf.
        forms.append(
            f'- "{ESCALATION_SENTINEL} <one short sentence saying what is still missing or wrong>" -- the task is '
            "genuinely NOT finished yet: real work is still needed, not just a wording issue. A stronger model will "
            "pick it up from exactly that point, with the full tool-call history so far. Only for genuinely "
            "unfinished work -- which includes a reply that only promises or announces an action (\"I'll send it "
            "now\") where the facts above show no such action was taken."
        )
    if can_retry:
        forms.append(
            f'- "{RETRY_SENTINEL} <one short sentence saying exactly what is still missing>" -- the task is '
            "genuinely NOT finished, but the gap looks like this SAME model simply stopped short -- it used tools "
            "fine for part of the work, then quit before covering the rest of what was named -- not that the task "
            "is beyond what this tier can reason through. Prefer this over escalating up when the same model, told "
            "specifically what's missing, is likely to finish it. Escalate instead when it never got going at all "
            "(no real tool work towards the task) or the gap looks genuinely beyond this tier's reasoning."
            + (
                " This request names several independent targets, so the rest may well need to be split into a "
                "declare_plan \"parallel\" stage + run_parallel_stage."
                if tracker.looks_multi_target else ""
            )
        )
    if can_deescalate:
        forms.append(
            f'- "{DEESCALATION_SENTINEL} <one short sentence saying what is left to do>" -- the task is genuinely '
            "NOT finished yet, but what remains is simple and mechanical: a clearly weaker, cheaper model could "
            "finish it from here. A less capable model will pick it up from exactly that point, with the full "
            "tool-call history so far. Only choose this when you are confident the SPECIFIC remaining steps are "
            "easy, not merely because the task as a whole seemed simple -- when in doubt, escalate instead."
        )
    forms.append(
        f'- "<{VERIFIER_FINAL_TAG}>the reply</{VERIFIER_FINAL_TAG}>" -- '
        + (
            "the reply above has to change before the user reads it: it claims something the facts above do not "
            "show, leaves out part of what was asked, or is not written to the user."
            if has_candidate
            else "there is no reply to send, so you write the message the user will read."
        )
        + " What is inside the tag is sent to the user as the whole reply. Write the reply itself there -- to the "
        "user, in the language of their request, clearly and to the point, directly answering it. Never describe "
        "the other assistant's work as a report, and never put a comment on the reply or on your own check inside "
        "the tag. Do not claim anything is done unless the reply and facts above show it."
        + (
            ""
            if (can_escalate or can_deescalate)
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
    parts.append(
        "CRITICAL: a program reads your reply. Reply in exactly one of these forms and with nothing else -- no "
        "explanation, no opinion of the reply:\n" + "\n".join(forms)
    )
    parts.append(f"A corrected reply written without the <{VERIFIER_FINAL_TAG}> tag cannot be used and is thrown away.")
    return "\n\n".join(parts)


# English like every text in the code; the answer is translated into the
# conversation's language on its way out (_finish_small_model_turn_answered).
_UNAVAILABLE_ANSWER = "I can't answer this right now -- please try again a little later."


# The verifier is told its reply IS the final message to the user. Seen live
# (2026-10-10, three turns in a row): the cheap verifier model instead wrote a
# review OF the reply -- "Ответ, который вы получили, полностью покрывает запрос...",
# "Ответ удовлетворяет всем условиям...", "Проверила, ответ подробный и
# соответствует задаче, дополнительных действий не требуется" -- and that review is
# what the user heard instead of a 1657-char real answer. Only replies that talk
# ABOUT "the reply" are caught: "Задача выполнена: письмо отправлено" is a real
# answer and must pass.
_VERDICT_PATTERNS = (
    re.compile(r"\b(ответ|reply|response)\w*\b.{0,80}\b(удовлетвор|соответству|покрыва|корректн|полност|"
               r"meets|satisf|covers|is complete|fully|correct)", re.IGNORECASE | re.DOTALL),
    # 2026-10-11, live, shown as Caroline's own greeting after a restart: "Поняла,
    # приветствие соответствует запросу." -- no "ответ" in it for the pattern above.
    # "The request", not "the requirements": an answer may well say a bag meets an
    # airline's requirements.
    re.compile(r"\b(соответству\w+|удовлетворя\w+) (запрос|задач|инструкци)\w*"
               r"|\b(matches|meets|satisfies|fulfil+s) the (request|instructions?|task)\b", re.IGNORECASE),
    re.compile(r"\bдополнительн\w* (работ|действи|шаг)\w* не требу", re.IGNORECASE),
    re.compile(r"\bno (further|additional) (work|action|steps?) (is |are )?(needed|required)", re.IGNORECASE),
)


# A review can also be long, and say none of the above (2026-10-11, live, 573 chars
# read out as the answer to "what will the weather be, and who wrote War and Peace"):
# "В ответе другая помощница написала, что в Киеве ... Проверка источников показывает,
# что ...". Whoever speaks of "the other assistant" or of what "the reply says" is
# talking ABOUT the answer, at any length -- the user is never the one being told that.
_REVIEW_MARKERS = re.compile(
    r"друг(ая|ой|ого|ую) (помощни|ассистент|модел)|предыдущ\w+ (помощни|ассистент)|"
    r"\bв ответе (выше )?(другая |другой )?(помощни\w+ |ассистент\w* )?(сказано|написано|написал|указано|указал|говорится|утверждается)|"
    r"\bпроверка (источников|фактов|вызовов|результат\w*) (показ|подтвержда)|"
    r"\b(the other|another|the previous|the first) (assistant|model)\b|\bthe (reply|response|answer) (above )?(says|states|claims|said)\b|"
    r"\b(checking|a check of) the (sources|tool calls) (shows|confirms)",
    re.IGNORECASE,
)


def _looks_like_verdict(text: str) -> bool:
    return bool(_REVIEW_MARKERS.search(text)) or (len(text) < 400 and any(p.search(text) for p in _VERDICT_PATTERNS))


# The verifier also writes the final text, and sometimes opens it with its opinion of the
# candidate (2026-10-11, live, read aloud to someone who had only asked about the weather:
# "Похоже, прогноз верный: завтра в Киеве ожидается ..."). Unlike a whole-text verdict
# (_looks_like_verdict) the answer is all there after the colon, so only the opening
# clause goes. It counts as the verifier's own addition only when the candidate did not
# open the same way: "Решение верное: ..." in reply to "check my solution" stays.
_VERDICT_PREFACE = re.compile(
    r"^[^:.!?\n]{0,60}\b(?:вер(?:ен|на|но|ны|ный|ная|ное|ные)|правил(?:ен|ьна|ьно|ьны|ьный|ьная|ьное|ьные)|"
    r"коррект(?:ен|на|но|ны|ный|ная|ное|ные)|подтвержд(?:аю|ается|ён|ен|ено|ена)|проверен[оаы]?|"
    r"correct|accurate|verified|confirmed|checks out)\b[^:.!?\n]{0,30}:\s+(?=\S)",
    re.IGNORECASE,
)
VERDICT_PREFACE_MIN_REST_CHARS = 40


def _strip_verdict_preface(verified: str, candidate: str) -> str:
    text = verified.lstrip()
    match = _VERDICT_PREFACE.match(text)
    if not match or _VERDICT_PREFACE.match(candidate.lstrip()):
        return verified
    rest = text[match.end():]
    if len(rest) < VERDICT_PREFACE_MIN_REST_CHARS:
        return verified
    return rest[0].upper() + rest[1:]


def _usable(text: str) -> bool:
    return bool(text.strip()) and ESCALATION_SENTINEL not in text and _is_silent_infra_failure(text) is None


VERIFIER_OK = "OK"
VERIFIER_FINAL_TAG = "final"
# A cheaper model asked first, with the same prompt (None: off, as shipped). It can only
# approve: its "OK" sends the answer out, anything else -- an escalation, a correction, a
# reply in no form -- goes to the verifier proper (AI_MODEL_MINI), which rules as before.
# Why it is here: with the answer no longer rewritten, the verifier's own wait is what
# is left between a tier's answer and the user -- live 2026-10-12, with the app's real
# prompt, 5.9s, 8.3s and 10.3s. On the golden set (evals/verifier_eval.py, 3 runs)
# google/gemini-2.5-flash-lite ruled all 33 as the verifier proper does, in 0.4s against
# 2.9s; claude-haiku-4.5 got 30 (it rewrote a correct answer three times).
# Off because that set is 11 cases with a short system prompt, and a wrong "OK" here is
# an unfinished answer delivered: turning it on is a decision about the quality gate.
VERIFIER_FIRST_MODEL: str | None = None
_VERIFIER_OK = re.compile(r"^\W*OK\W*$", re.IGNORECASE)
# The closing tag may be missing (a reply cut short): everything after the opening one is the text.
_VERIFIER_FINAL = re.compile(rf"<{VERIFIER_FINAL_TAG}>(.*?)(?:</{VERIFIER_FINAL_TAG}>|\Z)", re.IGNORECASE | re.DOTALL)
_ANY_TAG = re.compile(r"</?[a-zA-Z][\w-]*>")


def _verifier_form_reminder(can_escalate: bool, can_deescalate: bool, has_candidate: bool, can_retry: bool = False) -> str:
    """Sent once when the verifier's reply was in none of the forms (verify_candidate).
    On the golden set (evals/verifier_eval.py) the usual such reply was a correct
    correction written without the tag -- 2 runs of 3 for a wrong figure -- and keeping
    the candidate there sends the wrong figure out."""
    forms = [f'"{VERIFIER_OK}"'] if has_candidate else []
    if can_escalate:
        forms.append(f'"{ESCALATION_SENTINEL} <what is still missing>"')
    if can_retry:
        forms.append(f'"{RETRY_SENTINEL} <what is still missing>"')
    if can_deescalate:
        forms.append(f'"{DEESCALATION_SENTINEL} <what is left to do>"')
    forms.append(f'"<{VERIFIER_FINAL_TAG}>the reply the user should read</{VERIFIER_FINAL_TAG}>"')
    return ("That reply is in none of the forms, so the program cannot use it. Reply again with exactly one of these and "
            "nothing else: " + ", or ".join(forms) + ".")


def _read_verifier_reply(verified: str, candidate: str) -> tuple[str, str]:
    """What goes to the user after a verifier reply that is not an escalation, and how it
    was decided (for the log): see _build_verification_prompt for the forms.

    The candidate is the answer unless the verifier corrected it inside the tag. A reply
    in neither form is the verifier talking about the candidate instead of ruling on it
    -- exactly what used to reach the user -- so the candidate goes out, not that text.
    Only when there is no candidate at all (no tier produced one) is free text used: it
    is then the one message there is."""
    stripped = verified.strip()
    has_candidate = _usable(candidate)
    if _VERIFIER_OK.match(stripped):
        return (candidate, "ok") if has_candidate else ("", "ok_without_candidate")
    match = _VERIFIER_FINAL.search(stripped)
    if match and match.group(1).strip():
        text = match.group(1).strip()
        # The tag is where the answer goes, and still a model wrote it: the old guards stay.
        if has_candidate and _looks_like_verdict(text):
            return candidate, "verdict_in_tag"
        return _strip_verdict_preface(text, candidate), "corrected"
    if has_candidate:
        return candidate, "unparsed_candidate_kept"
    return _strip_verdict_preface(_ANY_TAG.sub("", stripped).strip(), candidate), "unparsed_text_used"


def _answer(text: str) -> dict[str, Any]:
    return {"status": "answered", "text": text}


# How much of a tool result the trace keeps -- the trace is the verifier's "ground truth"
# and what an escalated tier continues from. It was the first 300 characters. Live
# (2026-10-11, twice in a row, the same question): a web_search result is five sources
# with excerpts, ~4000 characters, and its first 300 are the heading and the start of the
# first source -- "... Morning temperature of 10 °C, af". The tier answered from the whole
# result (night, day, rain, wind); the verifier, shown only that stub, ruled "ответ
# содержит только данные о температуре утра" and escalated a correct answer that was ready
# at 10s, and the next tier, with the same stub to continue from, searched again and
# browsed for three minutes (190s and 223s to the answer). A whole web_search result fits now.
TRACE_RESULT_CHARS = 4500
# All trace lines shown together stay within this; beyond it older results are cut back to
# the old stub (a page snapshot from the app browser is as long as the limit above, and a
# browsing tier makes dozens).
TRACE_BUDGET_CHARS = 20000
TRACE_OLD_LINE_CHARS = 650


def _trace_line(name: Any, args_json: str, result: Any) -> str:
    return f"- {name}({args_json}) -> {str(result)[:TRACE_RESULT_CHARS]}"


def _trace_text(lines: list[str], limit: int) -> str:
    """The last `limit` trace lines: the newest whole, older ones cut once the budget is spent."""
    kept: list[str] = []
    spent = 0
    for line in reversed(lines[-limit:]):
        if spent + len(line) > TRACE_BUDGET_CHARS and len(line) > TRACE_OLD_LINE_CHARS:
            line = line[:TRACE_OLD_LINE_CHARS] + " ..."
        spent += len(line)
        kept.append(line)
    return "\n".join(reversed(kept))


# Whether a tool changes anything -- decided by a model (sw.tool.effect) from the tool's
# own description, never by a fixed list of name keywords (2026-10-22, explicit
# instruction). Kept per tool name + description hash in the workspace, so each tool is
# decided once, and again whenever its description changes.
TOOL_EFFECTS_FILE = "sw-tool-effects.json"
_tool_effects_lock = threading.Lock()


def _changes_something(tool_name: str, description: str, input_schema: Any, tab_id: str | None) -> bool:
    import hashlib
    from app.microagent_client import build_local_runtime, new_budget
    from app.microagents import sw_tool_effect
    from app.workspace_dir import WORKSPACE_DIR

    key = hashlib.sha256((tool_name + "\n" + (description or "")).encode("utf-8")).hexdigest()[:16]
    path = Path(WORKSPACE_DIR) / TOOL_EFFECTS_FILE
    with _tool_effects_lock:
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    known = cache.get(tool_name)
    if isinstance(known, dict) and known.get("key") == key and isinstance(known.get("changes"), bool):
        return known["changes"]
    params = ", ".join(sorted((input_schema or {}).keys())) if isinstance(input_schema, dict) else ""
    try:
        runtime = build_local_runtime()
        if runtime is None:
            return True
        for agent in sw_tool_effect.llm_agents():
            try:
                runtime.register(agent)
            except ValueError:
                pass
        result = runtime.run(sw_tool_effect.TOOL_EFFECT, {
            "name": tool_name, "description": (description or "")[:2000], "params": params,
        }, runtime.new_context(budget=new_budget()))
    except Exception as exc:  # noqa: BLE001 -- unknown means checked: the safe side
        log_event("engine", "sw_tool_effect_failed", tab_id=tab_id, tool=tool_name, error=str(exc)[:200])
        return True
    output = (result or {}).get("output") or {}
    if (result or {}).get("status") != "ok" or not isinstance(output.get("changes"), bool):
        log_event("engine", "sw_tool_effect_failed", tab_id=tab_id, tool=tool_name, error=(result or {}).get("error"))
        return True
    with _tool_effects_lock:
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        cache[tool_name] = {"key": key, "changes": output["changes"], "why": str(output.get("why"))[:300]}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    log_event("engine", "sw_tool_effect", tab_id=tab_id, tool=tool_name, changes=output["changes"], why=str(output.get("why"))[:200])
    return output["changes"]


def _action_in_scope(task: str, tool_name: str, args: dict[str, Any], calls: list[str], tab_id: str | None) -> tuple[bool, str]:
    """sw.action.scope, run synchronously on the tool-call worker thread. Fail-open: a
    check that cannot be made lets the call through (logged)."""
    from app.microagent_client import build_local_runtime, new_budget
    from app.microagents import sw_action_scope

    shown = {k: v for k, v in (args or {}).items() if k not in ("password", "imapHost", "imapPort", "smtpHost", "smtpPort")}
    started = time.monotonic()
    try:
        runtime = build_local_runtime()
        if runtime is None:
            return True, "no local runtime"
        for agent in sw_action_scope.llm_agents():
            try:
                runtime.register(agent)
            except ValueError:
                pass
        result = runtime.run(sw_action_scope.ACTION_SCOPE, {
            "task": task, "tool": tool_name, "args": json.dumps(shown, ensure_ascii=False, default=str)[:1500],
            "calls": calls[-15:],
        }, runtime.new_context(budget=new_budget()))
    except Exception as exc:  # noqa: BLE001 -- fail-open
        log_event("engine", "sw_action_scope_failed", tab_id=tab_id, tool=tool_name, error=str(exc)[:200])
        return True, "check failed"
    output = (result or {}).get("output") or {}
    if (result or {}).get("status") != "ok" or not isinstance(output.get("in_scope"), bool):
        log_event("engine", "sw_action_scope_failed", tab_id=tab_id, tool=tool_name, error=(result or {}).get("error"))
        return True, "check failed"
    log_event("engine", "sw_action_scope", tab_id=tab_id, tool=tool_name, in_scope=output["in_scope"],
              why=str(output.get("why"))[:300], ms=int((time.monotonic() - started) * 1000))
    return output["in_scope"], str(output.get("why") or "")


def _account_consistent(task: str, tool_name: str, args: dict[str, Any], calls: list[str], tab_id: str | None) -> tuple[bool, str, str]:
    """sw.account.consistency, run synchronously on the tool-call worker thread for a changing
    call that follows other calls of the turn: (consistent, the identity to use, why).
    Fail-open (logged)."""
    from app.microagent_client import build_local_runtime, new_budget
    from app.microagents import sw_account_consistency, sw_task_on_behalf

    shown = {k: v for k, v in (args or {}).items() if k not in ("password", "imapHost", "imapPort", "smtpHost", "smtpPort", "text", "html", "body")}
    started = time.monotonic()

    def run(module: Any, name: str, agent_input: dict[str, Any]) -> dict[str, Any] | None:
        runtime = build_local_runtime()
        if runtime is None:
            return None
        for agent in module.llm_agents():
            try:
                runtime.register(agent)
            except ValueError:
                pass
        result = runtime.run(name, agent_input, runtime.new_context(budget=new_budget()))
        if (result or {}).get("status") != "ok":
            log_event("engine", "sw_account_consistency_failed", tab_id=tab_id, tool=tool_name, agent=name, error=(result or {}).get("error"))
            return None
        return (result or {}).get("output") or {}

    try:
        output = run(sw_account_consistency, sw_account_consistency.ACCOUNT_CONSISTENCY, {
            "task": task, "tool": tool_name, "args": json.dumps(shown, ensure_ascii=False, default=str)[:1500],
            "calls": calls[-15:],
        })
        if output is None or sw_account_consistency.verify_account_consistency({}, output) is not None:
            return True, "", ""
        use = str(output["earlier_identity"]).strip()
        why = f"the earlier calls acted as {output['earlier_identity']}, this one as {output['this_identity']}"
        consistent, in_users_name = True, None
        if sw_account_consistency.switched(output, json.dumps(shown, ensure_ascii=False, default=str)):
            # A switch is right only if the task itself says to act in the user's own name.
            asked = run(sw_task_on_behalf, sw_task_on_behalf.TASK_ON_BEHALF, {"task": task})
            in_users_name = bool((asked or {}).get("in_users_name"))
            consistent = in_users_name
    except Exception as exc:  # noqa: BLE001 -- fail-open
        log_event("engine", "sw_account_consistency_failed", tab_id=tab_id, tool=tool_name, error=str(exc)[:200])
        return True, "", ""
    log_event("engine", "sw_account_consistency", tab_id=tab_id, tool=tool_name, consistent=consistent,
              earlier=str(output["earlier_identity"])[:80], this=str(output["this_identity"])[:80],
              task_in_users_name=in_users_name, ms=int((time.monotonic() - started) * 1000))
    return consistent, use, why


def _content_grounded(task: str, tool_name: str, args: dict[str, Any], calls: list[str], tab_id: str | None) -> tuple[bool, str]:
    """The content-grounding check, run synchronously on the tool-call worker thread for the
    changing call just checked by sw.action.scope: sw.content.extract takes the statements
    of fact out of what the call writes or sends, sw.content.grounding judges that list.
    Fail-open (logged)."""
    from app.microagent_client import build_local_runtime, new_budget
    from app.microagents import sw_content_extract, sw_content_grounding

    shown = {k: v for k, v in (args or {}).items() if k not in ("password", "imapHost", "imapPort", "smtpHost", "smtpPort")}
    started = time.monotonic()

    def run(module: Any, name: str, agent_input: dict[str, Any]) -> dict[str, Any] | None:
        runtime = build_local_runtime()
        if runtime is None:
            return None
        for agent in module.llm_agents():
            try:
                runtime.register(agent)
            except ValueError:
                pass
        result = runtime.run(name, agent_input, runtime.new_context(budget=new_budget()))
        if (result or {}).get("status") != "ok":
            log_event("engine", "sw_content_grounding_failed", tab_id=tab_id, tool=tool_name, agent=name, error=(result or {}).get("error"))
            return None
        return (result or {}).get("output") or {}

    try:
        extracted = run(sw_content_extract, sw_content_extract.CONTENT_EXTRACT, {
            "tool": tool_name, "args": json.dumps(shown, ensure_ascii=False, default=str)[:6000],
        })
        facts = [str(f) for f in (extracted or {}).get("facts") or []]
        requests = [str(r) for r in (extracted or {}).get("requests") or []][:8]
        if not facts and not requests:
            log_event("engine", "sw_content_grounding", tab_id=tab_id, tool=tool_name, grounded=True, facts=0,
                      ms=int((time.monotonic() - started) * 1000))
            return True, ""

        def judge_facts() -> list[str] | None:
            if not facts:
                return []
            judged = run(sw_content_grounding, sw_content_grounding.CONTENT_GROUNDING, {"task": task, "facts": facts, "calls": calls[-15:]})
            if judged is None or sw_content_grounding.verify_content_grounding({}, judged) is not None:
                return None
            return [str(x) for x in judged["invented"]]

        def judge_request(request: str) -> str | None:
            # A request is invented unless the agent quotes words that really are in the task.
            quoted = run(sw_content_grounding, sw_content_grounding.REQUEST_ASKED, {"task": task, "request": request})
            if quoted is None or sw_content_grounding.verify_request_asked({}, quoted) is not None:
                return None
            return None if sw_content_grounding.asked(task, quoted) else f"the task does not ask for this: {request}"

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1 + len(requests)) as pool:
            fact_job = pool.submit(judge_facts)
            request_jobs = [pool.submit(judge_request, r) for r in requests]
            invented = list(fact_job.result() or [])
            invented += [item for item in (job.result() for job in request_jobs) if item]
    except Exception as exc:  # noqa: BLE001 -- fail-open
        log_event("engine", "sw_content_grounding_failed", tab_id=tab_id, tool=tool_name, error=str(exc)[:200])
        return True, ""
    # The agents list what is invented; clean or not is decided here, by an empty list.
    log_event("engine", "sw_content_grounding", tab_id=tab_id, tool=tool_name, grounded=not invented, facts=len(facts),
              requests=len(requests), checked=(facts + requests)[:6], invented=invented[:4], ms=int((time.monotonic() - started) * 1000))
    return not invented, "; ".join(invented)


async def _result_evidence(task: str, calls: list[str], memo: dict[str, str], tab_id: str | None) -> str:
    """What is in the files the task was to make, read by the system for the verifier
    (2026-10-23): sw.result.evidence names the files, this code reads them with read_file
    (Word / Excel / PowerPoint as text). Only a path that occurs in the task or in the calls
    is read -- a guessed one never is. Read once per state of the calls (memo); "" when there
    are no files or nothing could be read."""
    from app.microagents.sw_result_complete import RESULT_COMPLETE
    from app.microagents.sw_result_evidence import MAX_FILES, RESULT_EVIDENCE
    from app.plugins import files_plugin

    key = json.dumps(calls[-30:], ensure_ascii=False, default=str)
    if key in memo:
        return memo[key]
    output = await _local_agent(RESULT_EVIDENCE, "app.microagents.sw_result_evidence", {"task": task, "calls": calls[-30:]}, tab_id)
    haystack = task + "\n" + "\n".join(calls)
    shown: list[str] = []
    for path in [str(p).strip() for p in (output or {}).get("files") or []][:MAX_FILES]:
        if path not in haystack:
            log_event("engine", "sw_result_evidence_path_dropped", tab_id=tab_id, path=path)
            continue
        try:
            text = (await files_plugin.read_file({"path": path}, None))["text"]
        except Exception as exc:  # noqa: BLE001 -- a file that is not there is itself the evidence
            text = f"(cannot be read: {exc})"
        if text and not text.startswith("(cannot be read"):
            complete = await _local_agent(RESULT_COMPLETE, "app.microagents.sw_result_complete",
                                          {"task": task, "content": text[:6000]}, tab_id)
            problems = [str(x) for x in (complete or {}).get("problems") or []]
            if problems:
                log_event("engine", "sw_result_incomplete", tab_id=tab_id, path=path, problems=problems[:6])
                text = text[:6000] + "\n\nCHECKED AGAINST THE TASK, THE FILE HAS THESE PROBLEMS:\n" + "\n".join("- " + x for x in problems)
        shown.append(f"--- {path} ---\n{text[:6000]}")
    memo[key] = "\n\n".join(shown)
    if shown:
        log_event("engine", "sw_result_evidence", tab_id=tab_id, files=len(shown), chars=len(memo[key]))
    return memo[key]


async def _local_agent(agent_name: str, module: str, agent_input: dict[str, Any], tab_id: str | None) -> dict[str, Any] | None:
    """One local microagent call (the ladder) -> its output, or None on any failure (logged)."""
    from app.plugins.voice_api import _run_local_microagent

    started = time.monotonic()
    try:
        result = await _run_local_microagent(agent_name, module, agent_input)
    except Exception as exc:  # noqa: BLE001 -- every caller has a plain fallback
        log_event("engine", "sw_microagent_failed", tab_id=tab_id, agent=agent_name, error=str(exc)[:200], error_type=type(exc).__name__)
        return None
    if not result or result.get("status") != "ok":
        log_event("engine", "sw_microagent_failed", tab_id=tab_id, agent=agent_name, error=(result or {}).get("error"),
                  ms=int((time.monotonic() - started) * 1000))
        return None
    log_event("engine", "sw_microagent_done", tab_id=tab_id, agent=agent_name, ms=int((time.monotonic() - started) * 1000),
              output=json.dumps(result.get("output"), ensure_ascii=False)[:600])
    return result.get("output") or {}


async def _task_tags(task: str, tab_id: str | None) -> list[str]:
    """Free tags of a task for the tier statistics (app/microagents/sw_task_tags.py)."""
    from app.microagents.sw_task_tags import TASK_TAGS

    output = await _local_agent(TASK_TAGS, "app.microagents.sw_task_tags", {"task": task}, tab_id)
    return [str(t).strip().lower() for t in (output or {}).get("tags") or [] if str(t).strip()]


async def _tier_advice(tags: list[str], tiers: list[str], workspace_dir: str, tab_id: str | None) -> int | None:
    """The tier to start from by the statistics (app/microagents/sw_tier_advice.py), or
    None when there are no tags or no statistics yet (then no call is made at all)."""
    from app.microagents.sw_tier_advice import TIER_ADVICE

    stats = sw_tier_stats.rows(workspace_dir)
    if not tags or not stats:
        return None
    output = await _local_agent(TIER_ADVICE, "app.microagents.sw_tier_advice", {"tags": tags, "tiers": tiers, "stats": stats}, tab_id)
    tier = (output or {}).get("start_tier")
    return tier if isinstance(tier, int) and 0 <= tier < len(tiers) else None


async def _tool_advice(task: str, gap: str, calls: list[str], registry: "ToolRegistry", tried_groups: list[str],
                       tab_id: str | None) -> str | None:
    """The tool advisor for a retry (2026-10-22, explicit instruction: the checker that
    hints knows the tools and says what to try -- most likely first, one group per
    attempt, independent calls in parallel). Step 1 ranks the plugin groups; step 2 turns
    the best group not suggested yet in this turn into concrete calls. None on failure --
    the retry then gets the generic _LOOK_IT_UP_FIRST hint."""
    from app.microagents.sw_tool_calls import TOOL_CALLS
    from app.microagents.sw_tool_groups import TOOL_GROUPS

    plugins = {p["server_name"].removeprefix("caroline-"): p["tools"] for p in registry.own_plugins}
    groups = [{"name": name, "tools": [t["name"] for t in tools]} for name, tools in plugins.items()]
    ranked_out = await _local_agent(TOOL_GROUPS, "app.microagents.sw_tool_groups", {
        "task": task, "gap": gap, "calls": calls[-30:], "tried": tried_groups, "groups": groups,
    }, tab_id)
    ranked = [g["name"] for g in (ranked_out or {}).get("groups") or [] if g.get("name") in plugins]
    if not ranked:
        return None
    group = next((g for g in ranked if g not in tried_groups), ranked[0])
    tried_groups.append(group)
    tools = [{"name": t["name"], "description": str(t.get("description") or "")[:500]} for t in plugins[group]]
    calls_out = await _local_agent(TOOL_CALLS, "app.microagents.sw_tool_calls", {
        "task": task, "gap": gap, "calls": calls[-30:], "group": group, "tools": tools,
    }, tab_id)
    suggested = (calls_out or {}).get("calls") or []
    if not suggested:
        return None
    lines = "; ".join(f"{c.get('tool')}({c.get('args') or ''}) -- {c.get('why') or ''}" for c in suggested)
    independent = [c for c in suggested if c.get("independent")]
    log_event("engine", "sw_tool_advice", tab_id=tab_id, group=group, calls=[c.get("tool") for c in suggested],
              independent=len(independent))
    parallel = (
        " The independent ones can run at the same time: declare_plan with a \"parallel\" stage (one step per "
        "call), then run_parallel_stage." if len(independent) > 1 else ""
    )
    return (
        f"Try these now, from the tools most likely to get what is missing ({group}): {lines}.{parallel} "
        "Ask the user only if they come back without it."
    )


async def _attempt_progressed(task: str, earlier: list[str], latest: list[str], tab_id: str | None) -> tuple[bool, str]:
    """(did the latest attempt move the task forward, why) -- the sw.attempt.progress
    microagent, on the local microagent ladder. Any failure counts as no progress:
    the task then goes up instead of paying for another attempt that may add nothing."""
    from app.microagents.sw_attempt_progress import ATTEMPT_PROGRESS
    from app.plugins.voice_api import _run_local_microagent

    started = time.monotonic()
    try:
        result = await _run_local_microagent(
            ATTEMPT_PROGRESS, "app.microagents.sw_attempt_progress",
            {"task": task, "earlier": earlier[-30:], "latest": latest[-30:]},
        )
    except Exception as exc:  # noqa: BLE001 -- escalate rather than guess
        log_event("engine", "sw_attempt_progress_failed", tab_id=tab_id, error=str(exc)[:200], error_type=type(exc).__name__)
        return False, "progress check failed"
    ms = int((time.monotonic() - started) * 1000)
    if not result or result.get("status") != "ok":
        log_event("engine", "sw_attempt_progress_failed", tab_id=tab_id, ms=ms, error=(result or {}).get("error"))
        return False, "progress check failed"
    output = result.get("output") or {}
    log_event("engine", "sw_attempt_progress", tab_id=tab_id, ms=ms, progress=bool(output.get("progress")), new=str(output.get("new"))[:300])
    return bool(output.get("progress")), str(output.get("new") or "")


def _continuation_note(tier_trace: list[str]) -> str:
    return (
        "[Internal: a previous attempt at this same request already ran these tool calls, with their real "
        "results. Continue from exactly where it stopped -- do not repeat these calls, do not restart from scratch. "
        + _CHECK_EARLIER_RESULT
        + "]\n"
        + _trace_text(tier_trace, 25)
    )


# What a retry or a next tier is told about the work already there (2026-10-22): "building
# on what is already done" was read as "add to it" -- on the test box a Word report got a
# second table (September missing) and three conclusions instead of one, each attempt
# appending to the file the one before had left wrong.
_CHECK_EARLIER_RESULT = (
    "What the earlier attempt produced may be wrong or incomplete -- duplicated pieces, partial leftovers, "
    "wrong values. First look at its real current state (open the file again, re-read what was changed), "
    "then make it exactly what was asked: fix or remove what is wrong, never add a second copy of something "
    "already there. Read the final result back before you reply."
)

# Per explicit instruction (2026-10-22): the verifier checks the reply against the data of
# the request. Seen the same day: told to answer that an invoice "will be paid by Friday",
# the reply drafted "we plan to pay on [date]" and was approved.
_REQUEST_VALUES_RULE = (
    " Check the reply against the task as the user gave it (the formalized task above, when there is one): "
    "every action done, and every value the user gave -- names, amounts, dates, wording -- used exactly as "
    "given. A reply that leaves one of them out, or puts a placeholder (\"[date]\", \"[дата]\", \"TBD\") or "
    "a question where the user already gave the value, is NOT finished. A message sent to someone else "
    "carries only what is meant for that recipient: the user's summary or report never goes into it (seen "
    "2026-10-22: a reply to a sender opened with \"Краткий пересказ письма: ...\", the summary meant for "
    "the user)."
)

# The verifier's rule for work that leaves a result behind (2026-10-22, same incident): the
# report with a duplicate table was approved -- nothing in the facts showed the file as it
# finally was, and the reply described it as asked.
_RESULT_READ_BACK_RULE = (
    "When the request was to create or change a file or document, the task is finished only if the facts "
    "above show the file's FINAL content -- the system's own reading of the files above, or a read-back by the "
    "assistant after its last change -- and that content is exactly what was asked: everything asked for, "
    "nothing duplicated or left over from an earlier attempt. When the content differs, say precisely what is "
    "wrong or missing in it. When there is no content to compare with, say that the final file still has to be "
    "read back -- never that the file was not made or saved unless the facts above show it."
)


COMPLEXITY_TIMEOUT_S = 12.0

# Voice turns (2026-10-10): the answer is read aloud, and any digit in it makes
# clean_text_for_speech spend a model call per spoken piece first -- live, 6s for a
# 195-char weather answer with "13-14 °C". Part of the system message, so the
# verifier (which writes the final text) follows it too. The trade-off: the chat
# bubble shows the numbers in words as well.
# Messages sent one after another within USER_MESSAGE_MERGE_WINDOW_S reach the model
# as one, joined by blank lines (chat_session._flush_held_user_messages) -- and they
# need not be about the same thing. Per explicit instruction (2026-10-11): the answer
# must not run them together. Tried on five joined pairs first: the topics were kept
# apart, each in its own paragraph, but nothing said which paragraph answered what.
_SEVERAL_REQUESTS_INSTRUCTION = (
    "The user's message may be several messages sent one after another, joined by blank lines. When they ask "
    "about different things, answer every one of them, in the order asked, and open each part by naming what it "
    "is about (\"Что касается погоды, ...\", \"As for the translation, ...\") so that the parts cannot be mixed "
    "up. When a later message corrects, narrows or withdraws an earlier one, answer what the user ended up "
    "asking, not both."
)

_VOICE_ANSWER_INSTRUCTION = (
    "This conversation is spoken: your final reply will be read aloud. Write numbers, dates, temperatures, "
    "units and formulas out in words, as they would be said, and use no markdown."
)

_COMPLEXITY_PROMPT = (
    "Classify a request for an assistant that has tools (shell commands, files, apps, windows, email, notes). "
    "Reply with exactly one word: COMPLEX if it needs several dependent tool steps (check, then act, then verify), "
    "otherwise SIMPLE.\n\nRequest:\n"
)

# Per explicit instruction (2026-10-15), a real incident: "check every mailbox"
# is a FAN-OUT task (the same mechanical check repeated over independent
# targets), not necessarily a "complex dependent chain" in _COMPLEXITY_PROMPT's
# own sense ("check, then act, then verify") -- the two are genuinely
# different shapes, and a fan-out task can plausibly score SIMPLE even though
# it's exactly the shape most likely to get silently abandoned after one
# shallow pass. A cheap, free, bilingual regex (no extra model call) rather
# than extending the classifier call -- deliberately recall-favoring: a false
# positive only costs one extra sentence in the verifier prompt or starting
# one tier higher, a false negative just leaves today's behavior unchanged.
_MULTI_TARGET_PATTERN = re.compile(
    r"\b(every|each|all (my|the)|one by one|across (my|the|all))\b.{0,40}\b"
    r"(mailbox(?:es)?|account(?:s)?|inbox(?:es)?|file(?:s)?|folder(?:s)?|document(?:s)?|item(?:s)?|"
    r"site(?:s)?|server(?:s)?|machine(?:s)?|address(?:es)?)\b"
    r"|\bкажд\w+\b|\bвсе (мои|почтов\w+|учётн\w+|аккаунт\w+)\b|по очеред\w+|по кажд\w+",
    re.IGNORECASE,
)


def _looks_multi_target(user_text: str) -> bool:
    """True when the request names multiple independent targets/items to go
    through one by one -- the signal _build_verification_prompt's own red
    flag and _start_tier_index use to catch the "checked one, declared the
    whole thing done" failure mode. See this pattern's own comment for why
    it's a free regex, not a model call."""
    return bool(_MULTI_TARGET_PATTERN.search(user_text))


# ------------------------------------------------------------------ fast lane --
#
# Prototype (2026-10-10), per explicit request ("нужно очень быстро... с твоей
# скоростью"): a question that needs no tools -- "what can you do", "explain
# fractions" -- went through the whole cascade anyway: tier pick, a reasoning model,
# the verifier, then a forced translation of an answer already in the right language.
# Measured live: 50-160s for exactly such questions. One plain call to a fast model
# answers them directly; anything needing live data, the user's own data or an action
# is declined with FAST_LANE_DECLINE and goes through the cascade exactly as before
# (sequentially -- never in parallel: a cascade worker can't be reliably stopped once
# it runs, and it may act). Off by FAST_LANE_ENABLED = False.

FAST_LANE_ENABLED = True
# Picked by benchmark (2026-10-10, same prompt/persona/key as the app, 8 questions:
# 3 to answer, 5 that need tools). Median latency / correct decisions:
#   claude-haiku-4.5 1.8s 8/8 | claude-sonnet-5 2.6s 8/8 (2x the price)
#   gemini-2.5-flash 1.5s 7/8 -- made up the time ("Сейчас 16:35")
#   gpt-4.1-mini 1.6s 6/8 -- made up memory ("ты мне вчера ничего не говорил")
#   gpt-5-nano/minimal 1.8s 4/8 -- asked for details instead of handing tasks on
#   gemini-2.5-flash-lite 1.5s 2/4 -- "Я проверила вашу почту", an invented email
# Cheaper than Haiku only meant confident wrong answers -- worse than a slow one.
FAST_LANE_MODEL = "anthropic/claude-haiku-4.5"
FAST_LANE_TIMEOUT_S = 15.0
# Only for a reasoning model in the fast lane (e.g. {"effort": "minimal"} for
# gpt-5-nano) -- left at its default such a model thinks for seconds first.
FAST_LANE_REASONING: dict[str, str] | None = None
FAST_LANE_DECLINE = "[[NEEDS_TOOLS]]"
# Declining, the fast lane also does _start_tier_index's job (2026-10-10): the same
# SIMPLE/COMPLEX call cost ~1.7s more on every tool turn, by a weaker model.
FAST_LANE_DECLINE_COMPLEX = "[[NEEDS_TOOLS:COMPLEX]]"
# A deterministic check after the model (2026-10-10), in the multi-stage sanitizing
# sense of the microagents paper: told plainly to decline questions about the user's
# own past or identity, the fast model still answered "Как меня зовут?" with "Мы ещё
# не встречались" and "Что я тебе вчера говорил?" with "Мы ещё не общались" -- false
# whenever memory or notes hold the answer. Such a reply is treated as a decline, so
# the cascade, which can look, answers instead.
_FAST_LANE_NO_HISTORY = re.compile(
    r"не (встречал|общал|знаком|разговаривал)|"
    r"(не знаю|не помню) (вашего|твоего|вашe|твоё|твое)? ?(имени|имя)|"
    r"(ничего|ещё|еще) (мне )?не (говорил|рассказывал|писал)|"
    # 2026-10-11, "Что я тебе вчера говорил про проект?" -> "Мы с вами только что начали
    # разговор -- у меня нет записей о том, что вы говорили вчера", and on the next run
    # "Я не помню предыдущих разговоров". "I don't remember" from a lane that has no
    # memory to look in is never the answer -- the cascade has one.
    r"только что (начали (разговор|общение|беседу|диалог|общаться|разговаривать)|познакомились)|"
    r"нет (записей|сведений|данных|информации|воспоминаний|памяти) о (том|наш|ваш|тво|вчераш|предыдущ|прошл)|"
    r"\bне (помню|запоминаю|сохраняю)\b|нет доступа к (предыдущ|прошл|истори|памят)|"
    r"haven'?t (met|talked|spoken)|don'?t know your name|you (haven'?t|didn'?t) (tell|told|mention)|"
    r"\bwe(('ve| have)? (only )?just (started|begun|met))|\bno (record|memory|recollection) of (what|our|your|you)|"
    r"(don'?t|do not|can'?t|cannot) (remember|recall)|don'?t have (access to|a record of|any record of) (previous|past|earlier|our)",
    re.IGNORECASE,
)
_FAST_LANE_DIALOGUE_LINES = 12
# The sentence a voice decline comes with (see _fast_lane_instructions) is read aloud
# as the turn's first narration remark, so it is held to the narrator's own rules by
# code, not by the prompt alone: no question, no digits, no promise, no machinery. One
# that fails is dropped -- the narrator then drafts the first remark itself, as before.
_FIRST_REMARK_FORBIDDEN = re.compile(
    r"[?\d\[\]]"
    r"|\b(сделаю|проверю|посмотрю|найду|поищу|узнаю|уточню|гляну|отправлю|сообщу|напишу|вернусь|попробую|постараюсь)\b"
    r"|\bскоро\b|\bI('ll| will| am going to)\b|\blet me\b"
    r"|инструмент|модел|уровень|уровн[яе]|\btools?\b|\bmodels?\b|\btier\b",
    re.IGNORECASE,
)
_FIRST_REMARK_MIN_CHARS = 8
_FIRST_REMARK_MAX_CHARS = 140


def _first_remark_from_decline(text: str, user_text: str = "") -> str | None:
    lines = text[text.rindex("]]") + 2:].strip().splitlines()
    remark = lines[0].strip().strip("\"«»") if lines else ""
    # A number the request itself has is a name, not a figure ("iPhone 17"), as in
    # voice_api._states_figure_as_settled. Live 2026-10-17: "Ищу цены на iPhone 17 в
    # украинских магазинах." was dropped for its "17", and the first remark was heard 11s
    # after the question instead of about 6.
    checked = remark
    for number in set(re.findall(r"\d+", user_text)):
        checked = re.sub(rf"(?<!\d){number}(?!\d)", " ", checked)
    if not _FIRST_REMARK_MIN_CHARS <= len(remark) <= _FIRST_REMARK_MAX_CHARS or _FIRST_REMARK_FORBIDDEN.search(checked):
        return None
    return remark


def _first_remark_clause(language: str) -> str:
    # Voice only (2026-10-11): the model that has just read the request can say in one
    # sentence what the work is, at no extra cost -- live, the narrator's own first remark
    # was heard 12.9s after the question. Checked by _first_remark_from_decline.
    return (
        "One exception to \"nothing else\": the user is listening, and hears nothing while the work goes on. "
        f"After the marker, on the same line, add ONE short sentence in {language}, in your own voice, saying "
        "what you are now doing for them, as work already under way -- \"Ищу прогноз погоды на завтра в "
        "Киеве.\" It names their task in plain words. It is not a promise (\"сейчас посмотрю\", \"I'll check\"), "
        "not a question, not a greeting; it says nothing of tools, models or steps, claims no result, and has no "
        "digits. "
    )


def _fast_lane_messages(
    workspace_dir: str, persona: Persona, instructions: str, recent_dialogue_lines: list[str], user_text: str,
) -> list[dict[str, Any]]:
    system = "\n\n".join([
        _persona_system_message(persona),
        owner_profile_system_prompt_clause(get_owner_profile(workspace_dir)),
        instructions,
    ])
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for line in recent_dialogue_lines[-_FAST_LANE_DIALOGUE_LINES:]:
        messages.append({"role": "assistant" if line.startswith("Caroline:") else "user", "content": line.split(":", 1)[-1].strip()})
    messages.append({"role": "user", "content": user_text})
    return messages


def _fast_lane_instructions(language: str, is_voice: bool) -> str:
    style = (
        "This is a spoken conversation: your reply will be read aloud. Plain sentences only -- no markdown, no "
        "lists, no emoji. Keep it short and natural, like a person answering out loud (2-5 sentences); if the "
        "user asked for detail, explain fully -- still in plain spoken sentences. Write numbers, formulas and "
        "symbols out in words, as they would be said aloud (\"углекислый газ\", not \"CO2\")."
        if is_voice
        else "Keep it concise and to the point."
    )
    first_remark = _first_remark_clause(language) if is_voice else ""
    return (
        f"Reply in {language}.\n{style}\n{_SEVERAL_REQUESTS_INSTRUCTION} If even one of several requests needs "
        "anything beyond this conversation and general knowledge, decline the whole message as described below -- "
        "do not answer the rest of it here.\n\n"
        "Answer the user's last message directly ONLY if you can do it fully and correctly from this conversation "
        "and general knowledge alone. If answering needs ANYTHING else -- current or live information (weather, "
        "news, prices, dates, schedules), the user's own data (mail, notes, files, contacts, calendar, anything "
        "remembered about them beyond this conversation), looking something up, or doing anything (sending, "
        "saving, opening, scheduling, changing) -- reply with exactly " + FAST_LANE_DECLINE + " and nothing else, or "
        "with exactly " + FAST_LANE_DECLINE_COMPLEX + " if it needs several dependent tool steps (check, then act, "
        "then verify). " + first_remark +
        "Writing, drafting, editing, checking or translating text is yours to do here -- only SENDING, saving "
        "or otherwise acting on it needs tools (seen live: \"help me write a polite letter to my teacher\" was "
        "declined). Anything about the user themselves that your instructions above don't state -- their name, "
        "history, preferences, what they said or did before this conversation -- may be in their notes or "
        "memory: decline; never answer that you don't know them, haven't met or don't remember (seen live: "
        "\"Как меня зовут?\" -> \"Мы ещё не встречались\"; \"Что я тебе вчера говорил?\" -> \"Я не помню "
        "предыдущих разговоров\"). "
        "When unsure, decline. Length or depth is never a reason to decline: a detailed explanation from general "
        "knowledge is still yours to give (seen live: \"Объясни подробно, как растения делают фотосинтез\" was "
        "declined, the same question without \"подробно\" answered). Never claim to have done, checked or saved "
        "anything, and never promise to."
    )


# Experiment (2026-10-10): the same check for the cascade's own final answer. The
# forced translation pass took 5-20s live on answers already in the tab's language;
# an answer that fails the check (code, paths, English terms, another language) is
# still translated exactly as before. False restores unconditional translation.
CASCADE_SKIP_TRANSLATION_WHEN_IN_LANGUAGE = True


# What in a Russian answer is not a sign of another language: links, and Latin words that
# are names -- any capital letter or digit in them (iPhone, Rozetka, BBC, iPhone17Pro).
# Live 2026-10-15: "... iPhone 17 ... в Rozetka, Comfy и Foxtrot ..." failed the 85% check,
# went through the translation pass (Russian to Russian, 7.5s), came back slightly changed,
# and so missed the speech made for it ahead (speech_ahead.py): 11.6s from the answer
# being ready to its first sound. A lower-case English word still counts against it.
_LINK = re.compile(r"(?:https?://|www\.)\S+|\b[\w.-]+\.(?:com|net|org|ua|ru|io|info|biz)(?:/\S*)?", re.IGNORECASE)
_LATIN_NAME = re.compile(r"\b(?=[A-Za-z0-9]*[A-Z0-9])[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*\b")


def _is_cyrillic(c: str) -> bool:
    return "Ѐ" <= c <= "ӿ"


def answer_is_in_language(text: str, language: str) -> bool:
    """Whether the forced translation pass can be skipped for an answer:
    only for the two languages it can be told apart cheaply and safely. Russian
    must not contain Ukrainian-only letters (a Russian/Ukrainian mix-up is exactly
    what the translation pass exists to catch)."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 10:
        return False
    if language == "Russian":
        prose_letters = [c for c in _LATIN_NAME.sub(" ", _LINK.sub(" ", text)) if c.isalpha()]
        if len(prose_letters) < 10:
            return False
        cyrillic = sum(1 for c in prose_letters if _is_cyrillic(c)) / len(prose_letters)
        return cyrillic >= 0.85 and not any(c in "іїєґІЇЄҐ" for c in text)
    if language == "English":
        return not any(_is_cyrillic(c) for c in letters) and sum(1 for c in letters if c.isascii()) / len(letters) >= 0.95
    return False


async def try_fast_answer(
    workspace_dir: str, persona: Persona, user_text: str, recent_dialogue_lines: list[str], language: str, is_voice: bool,
) -> tuple[str | None, int | None, str | None]:
    """(answer, None, None) when the fast lane answered; (None, start tier, first
    remark) when it declined -- the tier is 1 for a COMPLEX request, else 0, for
    run_small_model_turn's start_tier; the remark, voice only and possibly None, is
    what to say first while the cascade works. (None, None, None) when it couldn't
    run, and the cascade picks its tier itself. Never raises.

    One call decides all three (2026-10-22, per explicit instruction): answer it,
    or needs tools and is simple, or needs tools and is complex. There used to be a
    cheaper gate model in front deciding "needs tools?" and the starting tier, and
    a separate complexity estimator before that; the gate's weak model sent clearly
    multi-step tasks (find an email, summarize it, reply) to the weakest tier, which
    failed them. The fast model was benchmarked best at exactly this decision."""
    if not FAST_LANE_ENABLED or camerlengo_ai is None:
        return None, None, None
    started = time.monotonic()
    try:
        ai = await _model_ai()
        if ai is None:
            return None, None, None
        model = FAST_LANE_MODEL
        messages = _fast_lane_messages(workspace_dir, persona, _fast_lane_instructions(language, is_voice), recent_dialogue_lines, user_text)
        raw = await asyncio.wait_for(
            asyncio.to_thread(ai.resolve_agentic, messages, None, None, model=model, max_iterations=1, reasoning=FAST_LANE_REASONING),
            timeout=FAST_LANE_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 -- the cascade is always there to fall back on
        log_event("engine", "fast_lane_failed", error=str(exc)[:300], error_type=type(exc).__name__,
                  ms=int((time.monotonic() - started) * 1000))
        return None, None, None
    text = str(raw or "").strip()
    ms = int((time.monotonic() - started) * 1000)
    if not text or text.startswith("Agent error"):
        log_event("engine", "fast_lane_failed", error=text[:300], error_type="agent_error", ms=ms)
        return None, None, None
    if FAST_LANE_DECLINE_COMPLEX in text or FAST_LANE_DECLINE in text:
        start_tier = 1 if FAST_LANE_DECLINE_COMPLEX in text else 0
        first_remark = _first_remark_from_decline(text, user_text) if is_voice else None
        log_event("engine", "fast_lane_declined", ms=ms, start_tier=start_tier, first_remark=first_remark, raw=text[:200])
        return None, start_tier, first_remark
    if _FAST_LANE_NO_HISTORY.search(text):
        log_event("engine", "fast_lane_declined", ms=ms, start_tier=0, reason="claims_no_history", text=text[:200])
        return None, 0, None
    log_event("engine", "fast_lane_answered", ms=ms, model=model, text_len=len(text), text=text)
    return text, None, None


# Same model as the fast lane, for the same reason: on a narrator golden set it beat
# the SMALL-draft-plus-translation route (see generate_progress_comment_direct).
NARRATOR_MODEL = FAST_LANE_MODEL
# The longest remark said aloud, as drafted (numbers already in words): about 8s of speech.
# The prompt asks for one short sentence with at most one figure; both models broke it on
# lists of prices or cities. 354 remarks heard live up to 2026-10-17: median 59 chars, 90%
# within 103, 19 over 120 (6 by the first model, 13 by the second). One of them, 197 chars,
# took 13.6s to say, and the answer that was ready meanwhile waited for it.
NARRATOR_MAX_CHARS = 120


async def resolve_narration_prompt(prompt: str, model: str | None = None, reasoning: dict[str, str] | None = None) -> str:
    """Raw reply of NARRATOR_MODEL to one narrator prompt; raises if the engine or
    the provider key is unavailable (the caller then falls back)."""
    if camerlengo_ai is None:
        raise RuntimeError("camerlengo unavailable")
    ai = await _model_ai()
    if ai is None:
        raise RuntimeError("no provider key")
    return await asyncio.wait_for(
        asyncio.to_thread(ai.resolve_agentic, [{"role": "user", "content": prompt}], None, None,
                          model=model or NARRATOR_MODEL, max_iterations=1, reasoning=reasoning),
        timeout=FAST_LANE_TIMEOUT_S,
    )


async def draft_narration_remark(
    task: str, facts: str, already_said: list[str], language: str, gender: str | None = None, addressee_gender: str | None = None,
    repeat_window: int | None = None,
) -> str | None:
    """One narration remark by NARRATOR_MODEL. Same contract as
    generate_progress_comment_direct: the remark, "" for nothing new, None on failure.

    There used to be a cheaper first-draft model (gemini-2.5-flash-lite) tried before
    NARRATOR_MODEL; removed 2026-10-22 per explicit instruction -- no gemini anywhere.
    The cheaper alternatives benchmarked for this role were worse in the way that
    matters (gpt-4.1-nano reported "no letters found" about three found letters,
    gpt-5-nano announced the work finished and slipped into Ukrainian)."""
    from app.plugins.voice_api import generate_progress_comment_direct

    remark = await generate_progress_comment_direct(
        task, facts, already_said, language, resolve_narration_prompt, gender=gender, addressee_gender=addressee_gender,
        repeat_window=repeat_window, max_chars=NARRATOR_MAX_CHARS,
    )
    log_event("engine", "narration_drafted", model=NARRATOR_MODEL, said=bool(remark))
    return remark


async def _start_tier_index(ai: Any, cfg: Any, user_text: str) -> int:
    """Pre-assessment: a complex multi-step tool task starts directly at the second
    tier (mini) instead of the first (nano). Per explicit instruction (2026-10-15,
    "Предварительную оценку задачи делаем gpt-5.1-mini, не nano"): this one-word
    classification call runs on AI_MODEL_MINI rather than AI_MODEL_SMALL (nano) -- a
    sturdier judgment call for deciding whether nano is even worth trying. AI_MODEL_SMALL
    remains tier 0 of the real cascade; it's just no longer used for this pre-assessment
    itself. Goes through resolve_agentic (not a plain resolve call) for the same reason
    every other service call in this file does: one error-handling/Agent-error path,
    shared with the fast lane above."""
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(
                ai.resolve_agentic, [{"role": "user", "content": _COMPLEXITY_PROMPT + user_text[:2000]}], None, None,
                model=cfg.AI_MODEL_MINI, max_iterations=1, reasoning=COMPLEXITY_REASONING_EFFORT,
            ),
            timeout=COMPLEXITY_TIMEOUT_S,
        )
        if str(raw or "").startswith("Agent error"):
            # resolve_agentic reports a failed call as text, not an exception.
            log_event("engine", "small_model_complexity_failed", error=str(raw)[:300], error_type="agent_error")
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
    start_tier: int | None = None,
    is_voice: bool = False,
    on_candidate: Callable[[str], None] | None = None,
    history_messages: list[dict[str, Any]] | None = None,
    history_summary: str | None = None,
    recorder: Any = None,
    formalize_request: bool = True,
) -> dict[str, Any]:
    """Answers `user_text` through the OpenRouter-only path -- never hands off
    to the Claude SDK (2026-10-03). Always returns {"status": "answered", "text"}.

    history_messages/history_summary/recorder (Phase 2b, 2026-10-22, see
    app/sw_history.py): when given, the conversation so far comes from the
    sw engine's own history (earlier turns with their linked tool-work
    records, plus a compressed summary of anything older) instead of
    recent_dialogue_lines, and every tool call this turn makes -- parallel
    stage branches included -- is written to its own file by `recorder`.

    Four-tier cascade (nano -> mini -> AI_MODEL_ALTERNATE/Sonnet -> AI_MODEL_OPUS),
    carrying the previous tier's real tool-call trace forward on every hand-off in
    either direction. A tier escalates mechanically (ESCALATION_SENTINEL,
    NeedsEscalation, _TurnStalled) same as before, AND a verifier (AI_MODEL_MINI,
    tool-free -- a separate role) checks every tier's "done" candidate against the
    mechanical facts (undone plan steps, unconfirmed operations): if it finds the
    task genuinely unfinished and a later tier still exists, that is ALSO an
    escalation, not a final answer. Per explicit instruction (2026-10-15, "если
    продолжение по мнению самой модели может быть сделано более простой моделью -
    спускаемся на один уровень по лестнице"): the same verifier can ALSO judge the
    opposite -- the remaining work is simple enough for a weaker tier -- via
    DEESCALATION_SENTINEL, stepping the cascade back down one tier instead of up
    (capped at MAX_DEESCALATIONS per turn, so a tier can't bounce back and forth
    indefinitely). Only once a tier's candidate is verified as actually done (or
    the last tier has been tried and verified with no de-escalation left to try)
    does the verifier write the actual final user-facing text -- a real answer if
    it's done, or a plain, honest "could not complete this" if it still isn't, with
    no unsolicited instructions/tasks for the user either way.

    on_live_dialogue_update(lines), if given, is called every time this turn's
    own live exchange changes -- feeds the progress narrator. get_new_user_comments(),
    if given, is polled once per resolve_agentic() iteration so a long turn stays
    responsive to what the user says while it runs.
    """
    log_event("engine", "small_model_turn_started", tab_id=tab_id, text_len=len(user_text))
    if camerlengo_ai is None:
        log_event("engine", "small_model_engine_unavailable", tab_id=tab_id)
        return _answer(_UNAVAILABLE_ANSWER)
    # The formalized task (app/sw_request_spec.py), drafted while the rest is set up --
    # for a real user's request only: an internal note (a permission-guard stop, a
    # finished background operation) was "formalized" into a task of its own on the
    # test box, 2026-10-22, and the turn ended asking the user to confirm a send.
    from app.sw_request_spec import formalize

    request_spec_task = asyncio.ensure_future(formalize(user_text, recent_dialogue_lines, tab_id)) if formalize_request else None

    # Explicit adapter -- never rely on camerlengo_ai.AI()'s own default
    # (Config.OPENROUTER_KEY, server-only) from this process. Which adapter, and
    # whether a provider key is involved at all: see _model_ai().
    # Built here (moved up 2026-10-03) rather than right before the tier loop
    # so executor_fn (built below) can use `ai`/`cfg` directly for the
    # web_search tool, which is a plain blocking call, not a dispatch()'d
    # plugin operation.
    ai = await _model_ai()
    if ai is None:
        log_event("engine", "small_model_no_key_available", tab_id=tab_id)
        return _answer(_UNAVAILABLE_ANSWER)
    cfg = camerlengo_ai.Config

    registry = build_tool_registry()
    system = "\n\n".join([
        _persona_system_message(persona),
        owner_profile_system_prompt_clause(get_owner_profile(workspace_dir)),
        working_memory_system_prompt_clause(load_working_memory(workspace_dir)),
        memory_topics_system_prompt_clause(workspace_dir, tab_id),
        _engine_instructions(language),
        _ACT_DO_NOT_ASK_INSTRUCTION,
        _shared_policy_text(workspace_dir, tab_id),
    ] + ([_SEVERAL_REQUESTS_INSTRUCTION] if "\n\n" in user_text else []) + ([_VOICE_ANSWER_INSTRUCTION] if is_voice else [])
      + ([history_summary] if history_summary else []))
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    if history_messages is not None:
        messages.extend(history_messages)
    else:
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
    request_spec = await request_spec_task if request_spec_task is not None else None
    if request_spec:
        messages.append({"role": "user", "content": (
            f"{sent_line}\n[The task, formalized from the user's request -- this is the work to do. Every value "
            f"in it (names, amounts, dates, wording) is the user's own and is used exactly as given:]\n{request_spec}\n\n"
            f"[The user's own words, for clarification:]\n{user_text}"
        )})
    else:
        messages.append({"role": "user", "content": f"{sent_line}\n{user_text}"})

    main_loop = asyncio.get_running_loop()
    tracker = _TurnTracker()
    # A real user's task -- what the changing-action check compares calls against.
    tracker.task_text = (request_spec or user_text) if formalize_request else ""
    current_model = {"name": cfg.AI_MODEL_SMALL}

    async def run_stage(stage_index: int) -> str:
        return await _run_parallel_stage(
            stage_index, tracker=tracker, tab_id=tab_id, send=send, main_loop=main_loop,
            registry=registry, ai=ai, cfg=cfg, system_text=messages[0]["content"],
            user_text=user_text, model=current_model["name"], recorder=recorder,
        )

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

    # Ported from a teammate's branch (afb8e0f): a fact when a tool call
    # STARTS, not only once it returns -- live, the model's own thinking
    # plus a web search left the narrator nothing to say for ~16s.
    def on_tool_start(name: str, args: dict[str, Any]) -> None:
        # Only calls that do the user's work -- the plan and operation bookkeeping is not
        # news. A plugin's name ("memory", "email") tells the narrator in a plain word
        # where the work is now, which a tool name like request_info does not: live
        # (2026-10-11) a memory lookup started and the narrator found nothing to say.
        plugin = registry.lookup[name][0] if name in registry.lookup else None
        if plugin is None and name not in _WEB_SEARCH_TOOL_NAMES:
            return
        where = f'plugin "{plugin}"; ' if plugin else ""
        emit(f"Tool call {name} started ({where}arguments: {json.dumps(args, ensure_ascii=False, default=str)[:120]}).")

    executor_fn = _make_executor_fn(
        registry, tab_id, send, main_loop, tracker, ai, cfg, stage_runner=run_stage, on_tool_start=on_tool_start,
    )
    if recorder is not None:
        executor_fn = recorder.wrap(executor_fn, skip=_PLAN_TOOL_NAMES)

    def on_progress(evt: dict[str, Any]) -> None:
        # An abandoned attempt's thread must not touch the live attempt's progress
        # clock or trace -- it ends here instead (see _AttemptAbandoned).
        _abandon_if_cancelled(tracker)
        tracker.touch_progress()
        log_event(
            "engine", "small_model_progress", tab_id=tab_id, event_type=evt.get("type"),
            tool=evt.get("name"), iteration=evt.get("iteration"),
        )
        if evt.get("type") == "tool_call":
            log_event("engine", "small_model_tool_output", tab_id=tab_id, tool=evt.get("name"), args=evt.get("args"), result=str(evt.get("result", "")))
            args = json.dumps(evt.get("args") or {}, ensure_ascii=False, default=str)[:300]
            emit(f"Tool call {evt.get('name')} (arguments: {args[:120]}); result: {str(evt.get('result', ''))[:160]}")
            tier_trace.append(_trace_line(evt.get("name"), args, evt.get("result", "")))
            tracker.recent_calls.append(_trace_line(evt.get("name"), args, str(evt.get("result", ""))[:200]))
            del tracker.recent_calls[:-20]
        if evt.get("type") == "done" and on_live_dialogue_update:
            text = evt.get("text", "")
            if ESCALATION_SENTINEL not in text:
                live_dialogue.append(f"Caroline: {text}")
                on_live_dialogue_update(list(live_dialogue))

    def get_new_messages() -> list[dict[str, Any]] | None:
        # Polled before every model call of the attempt: an abandoned one ends here,
        # before paying for another call.
        _abandon_if_cancelled(tracker)
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

    # Final tier: AI_MODEL_OPUS used here WITH tools as a real escalation worker --
    # a distinct role from AI_MODEL_MINI's OTHER, tool-free use below as the verifier.
    tiers = [cfg.AI_MODEL_SMALL, cfg.AI_MODEL_MINI, cfg.AI_MODEL_ALTERNATE, cfg.AI_MODEL_OPUS]
    log_event("engine", "small_model_resolved", tab_id=tab_id, tiers=tiers, tool_count=len(registry.tool_defs))

    # Verification (AI_MODEL_MINI, no tools) runs after every tier that
    # thinks it's done. Per explicit instruction (2026-10-03, "если задача не
    # доделана, то нужна эскалация"): finding the task genuinely unfinished is
    # itself an escalation trigger while a later tier still exists -- verification
    # is no longer only a final polish pass tacked on after the loop.
    evidence_memo: dict[str, str] = {}

    async def run_verifier(
        candidate_text: str, can_escalate: bool, can_deescalate: bool, trace: list[str],
        not_a_form: str | None = None, first_model: str | None = None, can_retry: bool = False,
    ) -> str:
        verification_messages = list(messages)
        verification_messages.append({"role": "assistant", "content": candidate_text or "(no answer was produced)"})
        evidence = await _result_evidence(request_spec or user_text, list(tier_history) + list(trace), evidence_memo, tab_id)
        verification_messages.append({"role": "user", "content": _build_verification_prompt(tracker, candidate_text, can_escalate, trace, can_deescalate, can_retry, evidence)})
        if not_a_form is not None:
            verification_messages.append({"role": "assistant", "content": not_a_form})
            verification_messages.append({"role": "user", "content": _verifier_form_reminder(can_escalate, can_deescalate, _usable(candidate_text), can_retry)})
        return await _call_with_funds_wait(
            lambda: _resolve_agentic_with_watchdog(
                ai, tracker,
                messages=verification_messages, tool_defs=None, executor_fn=executor_fn,
                model=first_model or cfg.AI_MODEL_MINI, max_iterations=MAX_ITERATIONS, on_progress=on_progress,
                get_new_messages=get_new_messages, transforms=DISABLE_MIDDLE_OUT,
                reasoning=None if first_model else VERIFIER_REASONING_EFFORT, on_usage=on_usage,
                app="caroline",
            ),
            tab_id, emit,
        )

    async def verify_candidate(
        candidate_text: str, can_escalate: bool, can_deescalate: bool = False, trace: list[str] | None = None,
        can_retry: bool = False,
    ) -> tuple[str, str]:
        """Returns (text, action), action one of "done"/"up"/"down"/"retry". A
        technical verifier failure (empty reply/exception on both attempts) is
        never treated as an escalation/de-escalation/retry signal -- it just
        falls back to the candidate as-is, same as before this change.
        _FundsExhaustedGivingUp is NOT a technical failure -- let it propagate
        to the caller, which turns it into the same honest final answer
        regardless of whether funds ran out on a tier call or here."""
        emit("Checking the result (no tools).")
        if VERIFIER_FIRST_MODEL and _usable(candidate_text):
            started = time.monotonic()
            try:
                first = await run_verifier(candidate_text, can_escalate, can_deescalate, trace or [], first_model=VERIFIER_FIRST_MODEL, can_retry=can_retry)
            except _FundsExhaustedGivingUp:
                raise
            except Exception as exc:  # noqa: BLE001 -- the verifier proper decides
                log_event("engine", "small_model_verification_failed", tab_id=tab_id, attempt="first model", error=str(exc), error_type=type(exc).__name__)
                first = ""
            approved = bool(_VERIFIER_OK.match(first.strip()))
            log_event("engine", "small_model_verification_first", tab_id=tab_id, model=VERIFIER_FIRST_MODEL, approved=approved,
                      ms=int((time.monotonic() - started) * 1000), reply=first[:200])
            if approved:
                log_event("engine", "small_model_verification_done", tab_id=tab_id, how="ok_by_first_model", text_len=len(candidate_text),
                          text=candidate_text, verifier_reply=VERIFIER_OK)
                return candidate_text, "done"
        verified = ""
        for attempt in (1, 2):
            log_event("engine", "small_model_verification_started", tab_id=tab_id, attempt=attempt, has_candidate=bool(candidate_text), can_escalate=can_escalate, can_deescalate=can_deescalate, can_retry=can_retry)
            try:
                verified = await run_verifier(candidate_text, can_escalate, can_deescalate, trace or [], can_retry=can_retry)
            except _FundsExhaustedGivingUp:
                raise
            except Exception as exc:  # noqa: BLE001 -- verifier failure is a retry, never silence
                log_event("engine", "small_model_verification_failed", tab_id=tab_id, attempt=attempt, error=str(exc), error_type=type(exc).__name__)
                verified = ""
            if verified.strip():
                break
        if not verified.strip():
            log_event("engine", "small_model_verification_fallback_candidate", tab_id=tab_id, text_len=len(candidate_text))
            return candidate_text, "done"
        if can_escalate and ESCALATION_SENTINEL in verified:
            log_event("engine", "small_model_verification_escalating", tab_id=tab_id, note=verified.replace(ESCALATION_SENTINEL, "").strip()[:200])
            return verified, "up"
        if can_retry and RETRY_SENTINEL in verified:
            log_event("engine", "small_model_verification_retrying_same_tier", tab_id=tab_id, note=verified.replace(RETRY_SENTINEL, "").strip()[:200])
            return verified, "retry"
        if can_deescalate and DEESCALATION_SENTINEL in verified:
            log_event("engine", "small_model_verification_deescalating", tab_id=tab_id, note=verified.replace(DEESCALATION_SENTINEL, "").strip()[:200])
            return verified, "down"
        final, how = _read_verifier_reply(verified, candidate_text)
        if how.startswith("unparsed"):
            # In none of the forms: asked once more, with its own reply in view.
            log_event("engine", "small_model_verification_not_a_form", tab_id=tab_id, verifier_reply=verified[:300])
            try:
                again = await run_verifier(candidate_text, can_escalate, can_deescalate, trace or [], not_a_form=verified, can_retry=can_retry)
            except _FundsExhaustedGivingUp:
                raise
            except Exception as exc:  # noqa: BLE001 -- the first reading stands
                log_event("engine", "small_model_verification_failed", tab_id=tab_id, attempt="second ask", error=str(exc), error_type=type(exc).__name__)
                again = ""
            if can_escalate and ESCALATION_SENTINEL in again:
                log_event("engine", "small_model_verification_escalating", tab_id=tab_id, note=again.replace(ESCALATION_SENTINEL, "").strip()[:200])
                return again, "up"
            if can_retry and RETRY_SENTINEL in again:
                log_event("engine", "small_model_verification_retrying_same_tier", tab_id=tab_id, note=again.replace(RETRY_SENTINEL, "").strip()[:200])
                return again, "retry"
            if can_deescalate and DEESCALATION_SENTINEL in again:
                log_event("engine", "small_model_verification_deescalating", tab_id=tab_id, note=again.replace(DEESCALATION_SENTINEL, "").strip()[:200])
                return again, "down"
            if again.strip():
                final_again, how_again = _read_verifier_reply(again, candidate_text)
                if not how_again.startswith("unparsed"):
                    final, how, verified = final_again, how_again + "_on_second_ask", again
        log_event("engine", "small_model_verification_done", tab_id=tab_id, how=how, text_len=len(final), text=final,
                  verifier_reply=verified[:300] if how != "ok" else VERIFIER_OK)
        return final, "done"

    # Per explicit instruction (2026-10-15): computed once, up front, from the
    # user's own request text -- feeds both _start_tier_index just below (start
    # no lower than tier 1 for an apparent fan-out task) and the verifier's own
    # unconditional red flag (_build_verification_prompt). See _looks_multi_
    # target's own comment for the real incident this closes off.
    tracker.looks_multi_target = _looks_multi_target(user_text)

    # A real fact from the first second (2026-10-10): picking the starting tier is itself
    # a model call (~10s live), and until a tier starts the narrator had nothing to
    # draft from -- the first spoken remark of a voice turn came ~30s after the question.
    emit("The request has been received and work on it has started.")
    if start_tier is not None:
        start_index = start_tier  # already decided by the fast lane's decline
        log_event("engine", "small_model_complexity", start_tier=start_index, source="fast_lane")
    else:
        start_index = await _start_tier_index(ai, cfg, user_text)
        if tracker.looks_multi_target:
            # "check every mailbox" is a fan-out task, not necessarily a
            # COMPLEX dependent chain in _COMPLEXITY_PROMPT's own sense -- see
            # _MULTI_TARGET_PATTERN's own comment for why it can plausibly
            # score SIMPLE even though it's exactly the shape most likely to
            # get silently abandoned after one shallow pass.
            start_index = max(start_index, 1)
    # The tier statistics' advice (2026-10-22, explicit instruction): the start is the
    # higher of the choice above and the tier that does not fail too often on tasks with
    # these tags -- see app/sw_tier_stats.py. Real user requests only.
    task_tags: list[str] = []
    if formalize_request:
        task_tags = await _task_tags(request_spec or user_text, tab_id)
        advised = await _tier_advice(task_tags, tiers, workspace_dir, tab_id)
        if advised is not None and advised > start_index:
            log_event("engine", "small_model_start_tier_raised", tab_id=tab_id, from_tier=start_index, to_tier=advised)
            start_index = advised
    tiers_ran: list[int] = []
    closed_on: int | None = None
    tried_groups: list[str] = []
    final_text = ""
    index = start_index
    deescalations_used = 0
    # Per explicit instruction (2026-10-22): a tier is retried before the task goes up
    # ("сначала retry, потом вверх, обязательно"), and goes up only once it has run out
    # of what it can do -- "пока есть прогресс": retried while each attempt moves the
    # task forward, escalated after one that did not. Until then one retry per turn was
    # the limit, and a tier that answered with a question went straight up -- an email
    # task reached the top tier at $1.8 with the lower tiers never having tried a tool.
    # Whether an attempt moved the task forward is a separate microagent's call
    # (_attempt_progressed): counting "a call not made before" let nano search folders
    # that do not exist, one empty search after another, without ever going up.
    attempt_tier: int | None = None
    tier_attempts = 0
    tier_history: list[str] = []
    while index < len(tiers):
        model = tiers[index]
        if index != attempt_tier:
            attempt_tier, tier_attempts, tier_history = index, 0, []
        tiers_ran.append(index)
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
                    reasoning=TIER_REASONING_EFFORT[min(index, len(TIER_REASONING_EFFORT) - 1)], on_usage=on_usage,
                    app="caroline",
                ),
                tab_id, emit,
            )
        except _TurnStalled as exc:
            log_event("engine", "small_model_tier_stalled", tab_id=tab_id, tier=index, model=model, kind=exc.kind, elapsed_s=round(exc.elapsed_s))
            # A stall gives the work to the next tier (2026-10-22). It used to end the
            # turn: one shared cancellation flag stopped the stalled thread but would
            # have blocked every later tier too, so a stalled nano never got a stronger
            # model -- seen on the test box twice (an email task, a Word report). Now
            # only the stalled attempt's own flag is set; the next tier starts clean,
            # with what this one already did. The absolute ceiling still ends the turn.
            if exc.kind != "stall" or index >= len(tiers) - 1:
                break
            emit(f"Tier {index + 1} ({model}) stopped making progress; handing the work to tier {index + 2}.")
            log_event("engine", "small_model_tier_escalating", tab_id=tab_id, tier=index, model=model, trace_calls=len(tier_trace), reason="stall")
            if tier_trace:
                messages.append({"role": "user", "content": _continuation_note(tier_trace)})
                tier_trace.clear()
            index += 1
            continue
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
            # The answer as it will most likely go out (the verifier says "OK" to most): the
            # caller can start on what it does with a final text while the check runs.
            if on_candidate is not None:
                try:
                    on_candidate(candidate)
                except Exception:  # noqa: BLE001 -- a head start must never cost the turn
                    pass
            can_escalate = index < len(tiers) - 1
            can_deescalate = index > 0 and deescalations_used < MAX_DEESCALATIONS
            try:
                verified_text, action = await verify_candidate(candidate, can_escalate, can_deescalate, tier_trace, True)
            except _FundsExhaustedGivingUp as exc:
                log_event("engine", "small_model_funds_exhausted_giving_up", tab_id=tab_id, tier=index, model=model, reason=exc.reason)
                return _answer(_funds_exhausted_final_answer(exc.reason))
            if action == "done":
                final_text = verified_text
                closed_on = index
                break
            attempt_lines = list(tier_trace)
            if action in ("up", "retry"):
                # Unfinished: the tier's first attempt is always retried; after that it
                # stays only while the progress microagent sees the latest attempt get
                # something new (app/microagents/sw_attempt_progress.py).
                if tier_attempts == 0:
                    stay, why = True, "first attempt on this tier"
                else:
                    stay, why = await _attempt_progressed(request_spec or user_text, tier_history, attempt_lines, tab_id)
                log_event("engine", "small_model_retry_decision", tab_id=tab_id, tier=index, model=model,
                          attempt=tier_attempts + 1, stay=stay, why=why, verifier_said=action)
                if stay and action == "up":
                    verified_text, action = verified_text.replace(ESCALATION_SENTINEL, RETRY_SENTINEL), "retry"
                elif not stay and action == "retry":
                    if not can_escalate:
                        # The last tier, and its latest attempt got nothing new: the answer as it stands.
                        try:
                            verified_text, _ = await verify_candidate(candidate, False, False, attempt_lines, False)
                        except _FundsExhaustedGivingUp as exc:
                            log_event("engine", "small_model_funds_exhausted_giving_up", tab_id=tab_id, tier=index, model=model, reason=exc.reason)
                            return _answer(_funds_exhausted_final_answer(exc.reason))
                        final_text = verified_text
                        break
                    verified_text, action = verified_text.replace(RETRY_SENTINEL, ESCALATION_SENTINEL), "up"
            tier_history.extend(attempt_lines)
            if tier_trace:
                messages.append({"role": "user", "content": _continuation_note(tier_trace)})
                tier_trace.clear()
            if action == "retry":
                # Per explicit instruction (2026-10-15), for a request naming several
                # independent targets the nudge redirects to decomposition+parallelization
                # (declare_plan's "parallel" stage + run_parallel_stage), which makes the
                # retried attempt genuinely EASIER for the same tier. Only then, since
                # 2026-10-22 (explicit instruction): forced on a single sequential task
                # (a Word report, one email to find and answer) the parallel branches
                # redid work already done -- a second, English copy of the report's
                # table -- and stalled. There the nudge just says what is missing.
                # Same index (not +1): same model, same tier, continuing.
                tier_attempts += 1
                missing = verified_text.replace(RETRY_SENTINEL, "").strip()
                if tracker.looks_multi_target:
                    emit(f"The check found this tier's work incomplete but likely fixable by splitting it up; retrying tier {index + 1} with what's missing.")
                    nudge = (
                        f"[Verifier checked the previous attempt and found it incomplete: {missing} Split this into "
                        "declare_plan(stages=[{\"mode\": \"parallel\", \"steps\": [...]}]) with one step per target, "
                        "then call run_parallel_stage -- do not try to handle every target yourself in one sequential pass.]"
                    )
                else:
                    emit(f"The check found this tier's work incomplete; retrying tier {index + 1} with what's missing.")
                    advice = await _tool_advice(
                        request_spec or user_text, missing, tier_history, registry, tried_groups, tab_id,
                    )
                    nudge = (
                        f"[Verifier checked the previous attempt and found it incomplete: {missing} Do the missing part "
                        f"now, with your tools. {advice or _LOOK_IT_UP_FIRST} {_CHECK_EARLIER_RESULT}]"
                    )
                messages.append({"role": "user", "content": nudge})
                continue
            if action == "down":
                deescalations_used += 1
                emit(f"The check found the remaining work simple enough for a weaker tier; stepping down to tier {index}.")
                messages.append({"role": "user", "content": f"[Verifier checked the previous attempt and judged what's left simple enough for a less capable model: {verified_text.replace(DEESCALATION_SENTINEL, '').strip()}]"})
                index -= 1
                continue
            emit(f"The check did not confirm the task was done; escalating to tier {index + 2}.")
            messages.append({"role": "user", "content": f"[Verifier checked the previous attempt and found it not actually finished: {verified_text.replace(ESCALATION_SENTINEL, '').strip()}]"})
            index += 1
            continue

        emit(f"Tier {index + 1} ({model}) did not manage; moving on to the next tier.")
        log_event("engine", "small_model_tier_escalating", tab_id=tab_id, tier=index, model=model, trace_calls=len(tier_trace))
        if tier_trace:
            messages.append({"role": "user", "content": _continuation_note(tier_trace)})
            tier_trace.clear()
        index += 1

    if task_tags:
        try:
            sw_tier_stats.record(workspace_dir, task_tags, tiers_ran, closed_on)
        except Exception as exc:  # noqa: BLE001 -- statistics never cost the answer
            log_event("engine", "sw_tier_stats_record_failed", tab_id=tab_id, error=str(exc)[:200])
    if not final_text:
        # No tier ever produced a usable candidate (all stalled/errored/escalated
        # mechanically) -- one last, non-escalating, non-de-escalating verification
        # pass so the user still gets a real, honest answer instead of silence.
        try:
            verified_text, _ = await verify_candidate(final_text, can_escalate=False, can_deescalate=False, trace=tier_trace)
        except _FundsExhaustedGivingUp as exc:
            log_event("engine", "small_model_funds_exhausted_giving_up", tab_id=tab_id, tier=None, model=None, reason=exc.reason)
            return _answer(_funds_exhausted_final_answer(exc.reason))
        final_text = verified_text if _usable(verified_text) else _UNAVAILABLE_ANSWER

    log_event("engine", "small_model_turn_answered", tab_id=tab_id, text_len=len(final_text))
    # Whether a tier closed the task (the verifier's OK) -- the result writer then strips
    # offers to do more (2026-10-22, see app/sw_result_writer.py).
    return {**_answer(final_text), "completed": closed_on is not None}
