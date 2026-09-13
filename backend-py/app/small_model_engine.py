"""Primary path for simple, tool-using tasks (2026-09-12, redesigned
2026-09-13), per explicit design discussion: when SquirrelWisdom access is
available, try answering a real user turn through a small/cheap model
BEFORE falling back to the full Claude Agent SDK session -- same persona,
same tools (the SAME PluginTool objects every plugin already declares, via
app/plugins/loader.py's to_openai_tool_def(); see that function's own doc
comment for why this is native dual-format support, not a translation
layer), just a cheaper engine for anything simple enough not to need
Claude's own reasoning.

Redesign (2026-09-13, per explicit instruction -- "нужно эту часть вообще
переделать; подписка должна браться со squirrelwisdom.com, а не ключи"):
the ORIGINAL version of this module vendored Camerlengo's own AI.py (plus
its Config.py/Cache.py/etc. dependency closure) into Caroline's install so
it could run resolve_agentic() locally. That was scrapped entirely after
discovering, while wiring up packaging, that Config.py's own hardcoded
defaults AND AI.py itself contain real Partners Solutions production
secrets (admin/email passwords, a Google Maps key, and -- worse -- two live
OpenAI/OpenRouter API keys used as fallback defaults) that would have
shipped in cleartext inside a PUBLIC installer. There is no safe way to
vendor that file as-is.

The model call now happens SERVER-SIDE instead: a new v2 command,
ai:resolveAgenticStep (reforce's API/Api2AICommands.py, backed by AI.py's
new resolve_agentic_step() method), takes one turn of messages+tool defs,
makes exactly one model call, and returns either a final answer or the
tool_calls to run. Caroline sends the user's OWN SquirrelWisdom v2 session
(login_api.get_v2_session()) with every call, so the model call is
authenticated and billed against THEIR wallet/subscription -- exactly the
"подписка со squirrelwisdom.com" the user asked for -- and Caroline's
install never holds or ships any model-provider credential of any kind.
Tool EXECUTION still happens entirely locally (those tools -- OS
automation, the user's own email/notes/etc. -- only exist on their
machine); only the "ask the model what to do next" step crosses the
network. This also drops the sync/thread-bridging small_model_engine.py
needed before (resolve_agentic() was a blocking, synchronous call that had
to run via asyncio.to_thread with its executor_fn bridging back to the main
loop via run_coroutine_threadsafe) -- httpx is already async, so the whole
loop below is now plain async/await, no thread crossing at all.

Escalation to the full SDK happens in exactly two ways, per the original
design discussion (still unchanged) -- deliberately NOT based on timing,
iteration count, or dispatch()'s own "running" status (that's just normal
tool execution, not a complexity signal):
  1. The model's OWN judgment, expressed as a content-level sentinel
     (ESCALATION_SENTINEL) in its final reply -- covers "this task is
     harder than it looked" AND "the user has already had to correct me
     more than once" (both are visible to the model via the recent-dialogue
     messages it's given, so no separate mechanism is needed for the
     second case).
  2. A mechanical safety check in the executor: the SAME (tool_name, args)
     pair called too many times in a row is a broken/looping execution,
     not a complexity judgment -- raises NeedsEscalation immediately,
     which aborts the loop below rather than letting the model "retry"
     into the exact same loop.
MAX_ITERATIONS is deliberately set to a value that should never fire first
in real use -- a pure runaway-loop backstop, not a task-complexity budget;
the two escalation mechanisms above are what actually decide when to hand
off, not a step count.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from app import session_context
from app.login_api import get_v2_session
from app.logging_setup import log_event
from app.operations import dispatch
from app.persona import Persona
from app.plugins.loader import PluginTool, discover_plugins, to_openai_tool_def
from app.plugins.sw_api import CAROLINE_SW_KEY, SwApiError, call_v2

ESCALATION_SENTINEL = "[[NEED_ESCALATION]]"

# Per explicit instruction (2026-09-12): NOT a task-complexity budget -- a
# pure runaway-loop backstop for a caller that has a better signal (the two
# escalation mechanisms above) and doesn't want to rely on a step count.
MAX_ITERATIONS = 200

# How many times the SAME (tool_name, json-args) pair may repeat before the
# executor treats this as a broken/looping run and aborts -- a mechanical
# check, unrelated to task complexity (see module docstring point 2).
REPEATED_CALL_LIMIT = 3


class NeedsEscalation(Exception):
    """Raised by the executor to abort the loop immediately -- caught by
    run_small_model_turn() and turned into an {"status": "escalate", ...}
    result. Never let a repeated-call loop just keep retrying into the same
    stuck pattern -- it has to actually stop."""

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


def _make_executor(
    registry: ToolRegistry, tab_id: str | None, send: session_context.SendFn | None,
) -> Callable[[str, dict[str, Any]], Any]:
    """Plain async tool executor -- no thread bridging needed at all now
    that the model call itself is a network round-trip (see module
    docstring): this runs directly on the same event loop as everything
    else in ChatSession, calling the REAL dispatch() (app/operations.py,
    the same uniform start/status/stop + auto-logging contract the SDK
    path's wrap_tool() already uses for every tool call)."""
    seen_calls: dict[tuple[str, str], int] = {}

    async def executor(name: str, args: dict[str, Any]) -> str:
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

        if tab_id is not None:
            session_context.set_tab_id(tab_id)
        if send is not None:
            session_context.set_send(send)
        envelope = await dispatch(plugin_name, plugin_tool.name, plugin_tool.handler, args)
        log_event("engine", "small_model_tool_result", tab_id=tab_id, tool=name, status=envelope.get("status"))
        if envelope.get("status") == "error":
            return f"ERROR: {envelope.get('error')}"
        if envelope.get("status") == "running":
            # Genuinely normal (a slow tool call, e.g. a network-bound
            # plugin) -- NOT a signal of anything wrong. There is no
            # polling concept here; the best honest answer right now is
            # "still working". Expected to be rare given FAST_PATH_TIMEOUT_S.
            return f"Operation {envelope.get('operation_id')} is still running."
        result = envelope.get("result")
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)

    return executor


async def _resolve_agentic_step(messages: list[dict[str, Any]], tool_defs: list[dict[str, Any]], model: str) -> dict[str, Any]:
    """One network round-trip to reforce's ai:resolveAgenticStep -- see that
    command's own docstring (API/Api2AICommands.py) and AI.resolve_agentic_step's
    (AI.py) for the exact shared request/response shape. Authenticated with
    the user's OWN v2 session (billed/gated against their real SquirrelWisdom
    wallet, per explicit instruction) alongside CAROLINE_SW_KEY (identifies
    this as legitimate Caroline traffic, same as every other ai:* call this
    backend already makes -- see voice_api.py)."""
    session = await get_v2_session()
    envelope = await call_v2(
        "ai:resolveAgenticStep", key=CAROLINE_SW_KEY, session=session,
        messages=messages, tools=tool_defs, model=model,
    )
    return envelope


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

    get_new_user_comments(), if given, is polled once per loop iteration
    below -- returns any new real user messages that arrived since the
    last check, so a long-running turn stays responsive to what the user
    says WHILE it's still working, without cancelling and restarting.
    """
    log_event("engine", "small_model_turn_started", tab_id=tab_id, text_len=len(user_text))
    registry = build_tool_registry()
    system = _persona_system_message(persona) + "\n\n" + _engine_instructions(language)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for line in recent_dialogue_lines:
        is_caroline = line.startswith("Caroline:")
        content = line.split(":", 1)[-1].strip()
        messages.append({"role": "assistant" if is_caroline else "user", "content": content})
    messages.append({"role": "user", "content": user_text})

    executor = _make_executor(registry, tab_id, send)

    live_dialogue = [f"User: {user_text}"]
    if on_live_dialogue_update:
        on_live_dialogue_update(list(live_dialogue))

    log_event("engine", "small_model_resolved", tab_id=tab_id, tool_count=len(registry.tool_defs))

    try:
        for iteration in range(MAX_ITERATIONS):
            if get_new_user_comments:
                comments = get_new_user_comments()
                if comments:
                    for c in comments:
                        log_event("engine", "small_model_live_comment_injected", tab_id=tab_id, text_len=len(c))
                        messages.append({"role": "user", "content": c})
                        live_dialogue.append(f"User: {c}")
                    if on_live_dialogue_update:
                        on_live_dialogue_update(list(live_dialogue))

            step = await _resolve_agentic_step(messages, registry.tool_defs, "SMALL")

            if step.get("type") != "tool_calls":
                final_text = (step.get("text") or "").strip()
                log_event("engine", "small_model_progress", tab_id=tab_id, event_type="done", iteration=iteration)
                if on_live_dialogue_update:
                    live_dialogue.append(f"Caroline: {final_text}")
                    on_live_dialogue_update(list(live_dialogue))
                if ESCALATION_SENTINEL in final_text:
                    log_event("engine", "small_model_escalation_self_reported", tab_id=tab_id)
                    return {"status": "escalate", "reason": "model reported NEED_ESCALATION"}
                log_event("engine", "small_model_turn_answered", tab_id=tab_id, text_len=len(final_text))
                return {"status": "answered", "text": final_text}

            messages.append(step["assistant_message"])
            for call in step["calls"]:
                name = call["name"]
                try:
                    args = json.loads(call["arguments"] or "{}")
                except Exception:
                    args = {}
                result = str(await executor(name, args))
                if len(result) > 8000:
                    result = result[:8000] + "\n[...truncated]"
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                log_event(
                    "engine", "small_model_progress", tab_id=tab_id, event_type="tool_call",
                    tool=name, iteration=iteration,
                )

        log_event("engine", "small_model_turn_max_iterations", tab_id=tab_id)
        return {"status": "escalate", "reason": "reached maximum iterations without completing"}
    except NeedsEscalation as exc:
        log_event("engine", "small_model_escalation_mechanical", tab_id=tab_id, reason=exc.reason)
        return {"status": "escalate", "reason": exc.reason}
    except SwApiError as exc:
        log_event("engine", "small_model_turn_failed", tab_id=tab_id, error=str(exc), error_type="SwApiError")
        return {"status": "escalate", "reason": f"SquirrelWisdom API error: {exc}"}
    except Exception as exc:  # noqa: BLE001 -- this path must never be a hard dependency
        log_event("engine", "small_model_turn_failed", tab_id=tab_id, error=str(exc), error_type=type(exc).__name__)
        return {"status": "escalate", "reason": f"internal error: {exc}"}
