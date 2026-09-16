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
from typing import Any, Callable

from app import session_context
from app.durability import load_tab_continuity_archive
from app.logging_setup import log_event
from app.model_key_provisioning import get_model_provider_key
from app.operations import REGISTRY as OPERATIONS_REGISTRY
from app.operations import _operation_to_dict, dispatch
from app.persona import Persona
from app.plugins.loader import PluginTool, discover_plugins, to_openai_tool_def
from app.policies import (
    continuity_pointer_instruction,
    learn_from_mistakes_instruction,
    no_alarming_internal_recovery_instruction,
    no_full_filesystem_search_instruction,
    no_internal_mechanics_to_user_instruction,
    no_remote_filesystem_scans_instruction,
    no_unauthorized_secret_changes_instruction,
    prefer_embedded_browser_instruction,
    proactive_context_recovery_instruction,
    self_sufficiency_instruction,
    system_temp_dir_instruction,
    task_completion_memory_instruction,
    timestamp_awareness_instruction,
    vault_security_instruction,
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
    no_full_filesystem_search_instruction,
    no_remote_filesystem_scans_instruction,
    timestamp_awareness_instruction,
    no_alarming_internal_recovery_instruction,
    no_internal_mechanics_to_user_instruction,
    proactive_context_recovery_instruction,
    task_completion_memory_instruction,
    vault_security_instruction,
    no_unauthorized_secret_changes_instruction,
    prefer_embedded_browser_instruction,
    learn_from_mistakes_instruction,
    self_sufficiency_instruction,
    system_temp_dir_instruction,
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
        "describe it. Don't declare a plan and then just describe what you're about to do instead of doing it.\n\n"
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
]

_OPERATIONS_TOOL_NAMES = {t["function"]["name"] for t in _OPERATIONS_TOOL_DEFS}

# Per explicit instruction (2026-09-13), Phase 2 of the small-model fix
# plan: give the model a real, trackable way to say "here's my plan"
# instead of prose ("сделаю так: ...") that nothing can check. Handled
# specially in _make_executor_fn() below, same pattern as the operations
# trio -- these mutate a per-turn _TurnTracker rather than going through
# dispatch(). Declaring a plan is optional (most single-step asks don't
# need one), but ONCE declared, run_small_model_turn() checks declared vs.
# actually-marked-done steps before accepting the turn's text as final --
# see _TurnTracker.needs_verification().
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
                "properties": {"steps": {"type": "array", "items": {"type": "string"}}},
                "required": ["steps"],
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


@dataclass
class _TurnTracker:
    """Per-turn (not per-registry) mutable state -- one instance per
    run_small_model_turn() call, threaded through _make_executor_fn() so
    the executor can update it as tool calls actually happen. Used
    afterward to decide whether the turn's own text is trustworthy as a
    final answer or needs the verification pass (see
    needs_verification())."""

    tool_call_count: int = 0
    plan_steps: list[str] | None = None
    plan_done: dict[int, str] = field(default_factory=dict)
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

    def needs_verification(self) -> bool:
        if self.pending_operation_ids:
            return True
        if self.plan_steps is not None and len(self.plan_done) < len(self.plan_steps):
            return True
        return False


def _run_plan_tool(name: str, args: dict[str, Any], tracker: _TurnTracker) -> str:
    if name == "declare_plan":
        steps = list(args.get("steps") or [])
        tracker.plan_steps = steps
        tracker.plan_done = {}
        return f"Plan recorded with {len(steps)} step(s). Call mark_step_done after each one actually completes."
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


@dataclass
class ToolRegistry:
    tool_defs: list[dict[str, Any]]
    lookup: dict[str, tuple[str, PluginTool]]
    # tool_name -> that tool's plugin's usage_instructions, for the
    # get_tool_instructions tool -- see loader.py's Plugin.usage_instructions
    # own doc comment for why this is fetched on demand rather than injected
    # into the system prompt outright.
    tool_instructions: dict[str, str] = field(default_factory=dict)


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
    tool_defs: list[dict[str, Any]] = list(_OPERATIONS_TOOL_DEFS) + list(_PLAN_TOOL_DEFS)
    lookup: dict[str, tuple[str, PluginTool]] = {}
    tool_instructions: dict[str, str] = {}
    for plugin in discover_plugins():
        for t in plugin.tools:
            if t.name in _DISALLOWED_TOOL_NAMES:
                continue
            tool_defs.append(to_openai_tool_def(t))
            lookup[t.name] = (plugin.name, t)
            if plugin.usage_instructions:
                tool_instructions[t.name] = plugin.usage_instructions
    return ToolRegistry(tool_defs=tool_defs, lookup=lookup, tool_instructions=tool_instructions)


async def _run_operations_tool(name: str, args: dict[str, Any], registry: ToolRegistry, tracker: _TurnTracker) -> str:
    """Implements check_operation_status/stop_operation/get_tool_instructions
    directly against operations.py's own process-wide OPERATIONS_REGISTRY --
    NOT via dispatch() (these tools inspect/control an operation dispatch()
    already created, they don't start a new one of their own). Run on the
    main event loop (see _make_executor_fn's run_coroutine_threadsafe call)
    since Operation.task is a live asyncio.Task; .cancel() and reading task
    state should only ever happen on the loop that owns it. Also updates
    tracker.pending_operation_ids -- an operation reaching a terminal status
    here means it's no longer "dangling" (see _TurnTracker.needs_verification)."""
    if name == "check_operation_status":
        op = OPERATIONS_REGISTRY.get(args["operation_id"])
        if op is None:
            return "Unknown or already-completed operation_id."
        body = _operation_to_dict(op)
        if op.status in ("done", "error", "cancelled"):
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
    return f"ERROR: unknown operations tool '{name}'"


def _make_executor_fn(
    registry: ToolRegistry, tab_id: str | None, send: session_context.SendFn | None,
    main_loop: asyncio.AbstractEventLoop, tracker: _TurnTracker,
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
            return await dispatch(plugin_name, plugin_tool.name, plugin_tool.handler, args)

        envelope = asyncio.run_coroutine_threadsafe(_run(), main_loop).result()
        log_event("engine", "small_model_tool_result", tab_id=tab_id, tool=name, status=envelope.get("status"))
        if envelope.get("status") == "error":
            return f"ERROR: {envelope.get('error')}"
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
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)

    return executor_fn


def _build_verification_prompt(tracker: _TurnTracker, final_text: str) -> str:
    """Built fresh per verification call from tracker's own mechanical
    state -- never guesses at WHY verification is needed from final_text's
    wording, just states the concrete, checkable facts (which declared
    steps are unmarked, which operations were never resolved)."""
    parts = [
        "The reply above is from another assistant working on the same task. Check whether the underlying "
        "work is ACTUALLY complete -- not just whether the reply reads as complete -- and finish it for real if "
        "it isn't, using your own tools. Do not just restate or summarize the plan; verify and act."
    ]
    if tracker.plan_steps is not None:
        undone = [s for i, s in enumerate(tracker.plan_steps) if i not in tracker.plan_done]
        if undone:
            parts.append(
                f"A plan of {len(tracker.plan_steps)} step(s) was declared; these were never marked done: "
                + "; ".join(f'"{s}"' for s in undone)
            )
    if tracker.pending_operation_ids:
        parts.append(
            "These tool operations were started and never checked through to a final result -- call "
            "check_operation_status on each one first, before anything else: " + ", ".join(sorted(tracker.pending_operation_ids))
        )
    if tracker.tool_call_count == 0:
        parts.append("No tool was called at all for this task -- if one is actually needed, call it now.")
    parts.append(
        "Once everything is genuinely finished (and only then), reply with the real, final answer for the user "
        "-- the same way you'd answer them directly, not a report about the other assistant's work."
    )
    return "\n\n".join(parts)


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
) -> dict[str, Any]:
    """Tries to answer `user_text` via the small-model primary path.
    Returns {"status": "answered", "text": ...} or
    {"status": "escalate", "reason": ...} -- NEVER raises: this path is
    the primary but must never be a hard dependency, per explicit
    instruction that the existing SDK session is always the safety net,
    so any unexpected failure here is itself treated as an escalation
    rather than surfaced as an error.

    on_live_dialogue_update(lines), if given, is called every time this
    turn's own live exchange changes (the user's question, each real
    answer) -- lets the caller (ChatSession) feed the ACTUAL live
    conversation to the progress narrator instead of the stale on-disk SDK
    transcript, which this path never writes to at all.

    get_new_user_comments(), if given, is polled once per resolve_agentic()
    iteration (see that function's own get_new_messages parameter) --
    returns any new real user messages that arrived since the last check,
    so a long-running turn stays responsive to what the user says WHILE
    it's still working, without cancelling and restarting.
    """
    log_event("engine", "small_model_turn_started", tab_id=tab_id, text_len=len(user_text))
    if camerlengo_ai is None:
        log_event("engine", "small_model_engine_unavailable", tab_id=tab_id)
        return {"status": "escalate", "reason": "small-model engine (Camerlengo AI.py) not available on this install"}

    api_key = await get_model_provider_key()
    if not api_key:
        log_event("engine", "small_model_no_key_available", tab_id=tab_id)
        return {"status": "escalate", "reason": "no model-provider key available (not logged into SquirrelWisdom, or the fetch failed)"}

    registry = build_tool_registry()
    system = "\n\n".join([
        _persona_system_message(persona),
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
    executor_fn = _make_executor_fn(registry, tab_id, send, main_loop, tracker)

    live_dialogue = [f"User: {user_text}"]
    if on_live_dialogue_update:
        on_live_dialogue_update(list(live_dialogue))

    def on_progress(evt: dict[str, Any]) -> None:
        tracker.touch_progress()
        log_event(
            "engine", "small_model_progress", tab_id=tab_id, event_type=evt.get("type"),
            tool=evt.get("name"), iteration=evt.get("iteration"),
        )
        if evt.get("type") == "done" and on_live_dialogue_update:
            live_dialogue.append(f"Caroline: {evt.get('text', '')}")
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

    # Explicit adapter with the freshly-fetched key -- never rely on
    # camerlengo_ai.AI()'s own default (Config.OPENROUTER_KEY, server-only)
    # from this process; Caroline always passes its own, per-session key.
    adapter = camerlengo_ai.OpenRouterAdapter(api_key=api_key)
    ai = camerlengo_ai.AI(adapter=adapter)
    model = camerlengo_ai.resolveModelCategory("LARGE")
    log_event("engine", "small_model_resolved", tab_id=tab_id, model=model, tool_count=len(registry.tool_defs))

    try:
        final_text = await _resolve_agentic_with_watchdog(
            ai, tracker,
            messages=messages, tool_defs=registry.tool_defs, executor_fn=executor_fn,
            model=model, max_iterations=MAX_ITERATIONS, on_progress=on_progress,
            get_new_messages=get_new_messages, transforms=DISABLE_MIDDLE_OUT,
            reasoning=REASONING_EFFORT,
        )
    except _TurnStalled as exc:
        log_event("engine", "small_model_turn_stalled", tab_id=tab_id, kind=exc.kind, elapsed_s=round(exc.elapsed_s))
        return {"status": "escalate", "reason": f"no progress for {exc.elapsed_s:.0f}s ({exc.kind}) -- treating as a hang"}
    except NeedsEscalation as exc:
        log_event("engine", "small_model_escalation_mechanical", tab_id=tab_id, reason=exc.reason)
        return {"status": "escalate", "reason": exc.reason}
    except Exception as exc:  # noqa: BLE001 -- this path must never be a hard dependency
        log_event("engine", "small_model_turn_failed", tab_id=tab_id, error=str(exc), error_type=type(exc).__name__)
        return {"status": "escalate", "reason": f"internal error: {exc}"}

    if ESCALATION_SENTINEL in final_text:
        log_event("engine", "small_model_escalation_self_reported", tab_id=tab_id)
        return {"status": "escalate", "reason": "model reported NEED_ESCALATION"}

    infra_failure = _is_silent_infra_failure(final_text)
    if infra_failure is not None:
        log_event("engine", "small_model_escalation_infra_failure", tab_id=tab_id, reason=infra_failure)
        return {"status": "escalate", "reason": f"resolve_agentic gave up internally: {infra_failure}"}

    # Per explicit instruction (2026-09-13): "Работа малой модели в
    # отсутствие эскалации не должна требовать Claude SDK вообще" --
    # verification of an incomplete-looking turn stays entirely inside
    # Camerlengo/OpenRouter, using a DIFFERENT model category (ALTERNATE)
    # rather than either re-asking the same model (which just produced the
    # questionable answer) or routing to Claude (see chat_session.py's own
    # _finish_small_model_turn_answered, which no longer does that). Two
    # trigger conditions: tracker.needs_verification() (a plan was declared
    # but not fully marked done, or a started operation was never checked
    # through to a final status -- both mechanical, no text-pattern
    # guessing) OR a long reply with zero tool calls and no plan at all --
    # a pragmatic proxy (not NLP) for exactly the "wrote a paragraph plan,
    # called nothing" pattern confirmed live in tab 1's own log; a short
    # zero-tool reply (a quick factual answer) is left alone.
    long_text_no_tools = tracker.tool_call_count == 0 and tracker.plan_steps is None and len(final_text) > 200
    if tracker.needs_verification() or long_text_no_tools:
        log_event(
            "engine", "small_model_verification_triggered", tab_id=tab_id,
            plan_incomplete=tracker.plan_steps is not None and len(tracker.plan_done) < len(tracker.plan_steps),
            pending_operations=len(tracker.pending_operation_ids), long_text_no_tools=long_text_no_tools,
        )
        verification_model = camerlengo_ai.resolveModelCategory("ALTERNATE")
        verification_messages = list(messages)
        verification_messages.append({"role": "assistant", "content": final_text})
        verification_messages.append({"role": "user", "content": _build_verification_prompt(tracker, final_text)})
        try:
            verified_text = await _resolve_agentic_with_watchdog(
                ai, tracker,
                messages=verification_messages, tool_defs=registry.tool_defs, executor_fn=executor_fn,
                model=verification_model, max_iterations=MAX_ITERATIONS, on_progress=on_progress,
                get_new_messages=get_new_messages, transforms=DISABLE_MIDDLE_OUT,
                reasoning=REASONING_EFFORT,
            )
        except _TurnStalled as exc:
            log_event("engine", "small_model_verification_stalled", tab_id=tab_id, kind=exc.kind, elapsed_s=round(exc.elapsed_s))
            return {"status": "escalate", "reason": f"verification pass had no progress for {exc.elapsed_s:.0f}s ({exc.kind})"}
        except NeedsEscalation as exc:
            log_event("engine", "small_model_verification_escalation_mechanical", tab_id=tab_id, reason=exc.reason)
            return {"status": "escalate", "reason": exc.reason}
        except Exception as exc:  # noqa: BLE001 -- this path must never be a hard dependency
            log_event("engine", "small_model_verification_failed", tab_id=tab_id, error=str(exc), error_type=type(exc).__name__)
            return {"status": "escalate", "reason": f"verification pass internal error: {exc}"}

        if ESCALATION_SENTINEL in verified_text:
            log_event("engine", "small_model_verification_escalation_self_reported", tab_id=tab_id)
            return {"status": "escalate", "reason": "verification pass reported NEED_ESCALATION"}
        infra_failure = _is_silent_infra_failure(verified_text)
        if infra_failure is not None:
            log_event("engine", "small_model_verification_infra_failure", tab_id=tab_id, reason=infra_failure)
            return {"status": "escalate", "reason": f"verification pass gave up internally: {infra_failure}"}

        # Capped at one round -- accept the verification pass's own result
        # regardless of tracker's state now (it had the same tools and the
        # same chance to actually finish); not re-verifying a second time.
        final_text = verified_text
        log_event("engine", "small_model_verification_done", tab_id=tab_id, text_len=len(final_text))

    log_event("engine", "small_model_turn_answered", tab_id=tab_id, text_len=len(final_text))
    return {"status": "answered", "text": final_text}
