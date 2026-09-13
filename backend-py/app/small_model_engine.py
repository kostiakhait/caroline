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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app import session_context
from app.logging_setup import log_event
from app.model_key_provisioning import get_model_provider_key
from app.operations import dispatch
from app.persona import Persona
from app.plugins.loader import PluginTool, discover_plugins, to_openai_tool_def

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


class NeedsEscalation(Exception):
    """Raised by the executor_fn to abort resolve_agentic()'s loop
    immediately -- caught by run_small_model_turn() and turned into an
    {"status": "escalate", ...} result. Never let resolve_agentic() itself
    see this as a normal tool error (which the model might just retry into
    the same loop) -- it has to actually stop the loop."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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


def _engine_instructions(language: str) -> str:
    return (
        "You are being asked to help with something that looked simple enough to answer directly -- possibly "
        "using one or more of the tools available to you. Do so.\n\n"
        f"Reply in {language} unless the user's own message is written in a different language -- then match "
        "theirs instead.\n\n"
        "If you judge that you are NOT coping with this task -- too many tool-call failures, the task turning "
        "out to be more complex than it looked once you got into it, or the conversation history below shows "
        "the user already had to correct you or repeat themselves more than once -- stop and reply with "
        f"EXACTLY {ESCALATION_SENTINEL} and nothing else, no explanation. This hands the conversation off to a "
        "more capable assistant -- it is not a failure on your part, it's the right call the moment a task "
        "turns out to be bigger than it looked. Only do this as a genuine judgment call, not a first resort -- "
        "most things you'll be asked fall well within what you can handle directly."
    )


@dataclass
class ToolRegistry:
    tool_defs: list[dict[str, Any]]
    lookup: dict[str, tuple[str, PluginTool]]


def build_tool_registry() -> ToolRegistry:
    """Every tool every plugin exposes, in OpenAI/Camerlengo format -- per
    explicit instruction, tools are fully shared between this path and the
    SDK path, not a curated subset; see to_openai_tool_def()'s own doc
    comment for why this needs no plugin-file changes at all."""
    tool_defs: list[dict[str, Any]] = []
    lookup: dict[str, tuple[str, PluginTool]] = {}
    for plugin in discover_plugins():
        for t in plugin.tools:
            tool_defs.append(to_openai_tool_def(t))
            lookup[t.name] = (plugin.name, t)
    return ToolRegistry(tool_defs=tool_defs, lookup=lookup)


def _make_executor_fn(
    registry: ToolRegistry, tab_id: str | None, send: session_context.SendFn | None,
    main_loop: asyncio.AbstractEventLoop,
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
    context, so there's deliberately no reset/cleanup dance needed here."""
    seen_calls: dict[tuple[str, str], int] = {}

    def executor_fn(name: str, args: dict[str, Any]) -> str:
        args = args or {}
        call_key = (name, json.dumps(args, sort_keys=True, default=str))
        seen_calls[call_key] = seen_calls.get(call_key, 0) + 1
        count = seen_calls[call_key]
        log_event("engine", "small_model_tool_call", tab_id=tab_id, tool=name, args=args, repeat_count=count)
        if count > REPEATED_CALL_LIMIT:
            log_event("engine", "small_model_repeated_call_detected", tab_id=tab_id, tool=name, args=args, count=count)
            raise NeedsEscalation(f"tool '{name}' called with identical arguments {count} times in a row -- likely a stuck loop")

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
            # is expected to be rare given FAST_PATH_TIMEOUT_S.
            return f"Operation {envelope.get('operation_id')} is still running."
        result = envelope.get("result")
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)

    return executor_fn


async def run_small_model_turn(
    tab_id: str,
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
    system = _persona_system_message(persona) + "\n\n" + _engine_instructions(language)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for line in recent_dialogue_lines:
        is_caroline = line.startswith("Caroline:")
        content = line.split(":", 1)[-1].strip()
        messages.append({"role": "assistant" if is_caroline else "user", "content": content})
    messages.append({"role": "user", "content": user_text})

    main_loop = asyncio.get_running_loop()
    executor_fn = _make_executor_fn(registry, tab_id, send, main_loop)

    live_dialogue = [f"User: {user_text}"]
    if on_live_dialogue_update:
        on_live_dialogue_update(list(live_dialogue))

    def on_progress(evt: dict[str, Any]) -> None:
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
    model = camerlengo_ai.resolveModelCategory("SMALL")
    log_event("engine", "small_model_resolved", tab_id=tab_id, model=model, tool_count=len(registry.tool_defs))

    try:
        final_text = await asyncio.to_thread(
            ai.resolve_agentic, messages, registry.tool_defs, executor_fn,
            model, MAX_ITERATIONS, on_progress, get_new_messages,
        )
    except NeedsEscalation as exc:
        log_event("engine", "small_model_escalation_mechanical", tab_id=tab_id, reason=exc.reason)
        return {"status": "escalate", "reason": exc.reason}
    except Exception as exc:  # noqa: BLE001 -- this path must never be a hard dependency
        log_event("engine", "small_model_turn_failed", tab_id=tab_id, error=str(exc), error_type=type(exc).__name__)
        return {"status": "escalate", "reason": f"internal error: {exc}"}

    if ESCALATION_SENTINEL in final_text:
        log_event("engine", "small_model_escalation_self_reported", tab_id=tab_id)
        return {"status": "escalate", "reason": "model reported NEED_ESCALATION"}

    log_event("engine", "small_model_turn_answered", tab_id=tab_id, text_len=len(final_text))
    return {"status": "answered", "text": final_text}
