"""Per-turn session context, threaded via contextvars so plugin handlers
can reach the calling ChatSession's own WS push (`ChatSession.send`)
without every plugin author needing bespoke callback plumbing -- mirrors
backend/src/*.ts's `sendToFrontend` closures (each TS tool-factory function
took its own copy of that parameter), generalized here: one ChatSession
sets this once, and every plugin handler invoked during that session's
lifetime can reach it via get_send().

contextvars.ContextVar propagates correctly to any asyncio Task spawned
(directly or indirectly) from the point where it was set -- exactly the
call chain a ChatSession's own client/pump task and everything the SDK
calls back into (including in-process MCP tool handlers) descends from.
"""

from __future__ import annotations

import contextvars
from typing import Any, Awaitable, Callable

SendFn = Callable[[dict[str, Any]], Awaitable[None]]
InjectProactiveFn = Callable[[str], bool]
CliPidSinkFn = Callable[[int], None]
PermissionBlockerFn = Callable[[], "str | None"]
SendActionGuardFn = Callable[[str, "dict[str, Any]"], Awaitable["str | None"]]
ReplyWatchFn = Callable[[str, dict, Any], None]
RecentDirectivesFn = Callable[[], str]
GetSessionFn = Callable[[str], Any]

_current_send: contextvars.ContextVar[SendFn | None] = contextvars.ContextVar("current_send", default=None)
_current_tab_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_tab_id", default=None)
_current_inject_proactive: contextvars.ContextVar[InjectProactiveFn | None] = contextvars.ContextVar("current_inject_proactive", default=None)
_current_cli_pid_sink: contextvars.ContextVar[CliPidSinkFn | None] = contextvars.ContextVar("current_cli_pid_sink", default=None)
# Every live ChatSession's inject_proactive by tab id (see register_inject_proactive).
_inject_proactive_by_tab: dict[str, InjectProactiveFn] = {}
# Every live ChatSession's permission blocker by tab id (see register_permission_blocker).
_permission_blocker_by_tab: dict[str, PermissionBlockerFn] = {}
# Every live ChatSession's send() by tab id (see register_send_for_tab).
_send_by_tab: dict[str, SendFn] = {}
# Every live ChatSession's reply watch by tab id (see register_reply_watch).
_reply_watch_by_tab: dict[str, ReplyWatchFn] = {}
# Every live ChatSession's recent-real-user-directives text by tab id (see
# register_recent_real_user_directives).
_recent_directives_by_tab: dict[str, RecentDirectivesFn] = {}


def get_send() -> SendFn:
    """Raises if called outside a live ChatSession turn (e.g. a throwaway
    test script driving a plugin handler directly) -- callers that
    genuinely need to push a WS event have no sane fallback, so failing
    loudly here is more useful than silently doing nothing."""
    send = _current_send.get()
    if send is None:
        raise RuntimeError("No active session send() context -- this tool must be called from within a live ChatSession turn.")
    return send


def set_send(send: SendFn | None) -> contextvars.Token[SendFn | None]:
    return _current_send.set(send)


def get_tab_id() -> str | None:
    """Per explicit instruction (2026-09-10): lets app/operations.py's
    dispatch() tag every Operation with the tab that started it, so
    ChatSession.stop() can cancel exactly this tab's own in-flight
    background operations (OperationRegistry is process-wide, shared by
    every tab) without touching another tab's still-running work. None
    outside a live ChatSession turn -- same non-fatal shape as a plugin
    handler invoked directly by a throwaway script, unlike get_send()
    there's no reason to raise here, callers just skip tagging."""
    return _current_tab_id.get()


def set_tab_id(tab_id: str | None) -> contextvars.Token[str | None]:
    return _current_tab_id.set(tab_id)


def get_inject_proactive() -> InjectProactiveFn | None:
    """Per explicit instruction (2026-09-15): lets app/operations.py notify
    the calling ChatSession on its own, once a background Operation the
    model already walked away from (the slow-path "running" case -- see
    dispatch()'s own FAST_PATH_TIMEOUT_S) actually finishes, instead of the
    result just sitting in OperationRegistry forever unless something
    happens to poll it. Same non-fatal shape as get_tab_id() -- None
    outside a live ChatSession turn, callers just skip notifying rather
    than raising (an Operation dispatched by a throwaway test script has
    no session to notify anyway).

    When the context var is not set but the tab is known, the tab's own
    session is used (register_inject_proactive): the small-model engine
    runs tool handlers on the main loop from worker threads, re-setting
    only the tab id and send() in their context -- this is how they reach
    their session too."""
    fn = _current_inject_proactive.get()
    if fn is None:
        tab_id = _current_tab_id.get()
        fn = _inject_proactive_by_tab.get(tab_id) if tab_id else None
    return fn


def register_inject_proactive(tab_id: str, fn: InjectProactiveFn) -> None:
    """Called by a ChatSession for its tab; a later session of the same tab replaces it."""
    _inject_proactive_by_tab[tab_id] = fn


def set_inject_proactive(fn: InjectProactiveFn | None) -> contextvars.Token[InjectProactiveFn | None]:
    return _current_inject_proactive.set(fn)


def register_permission_blocker(tab_id: str, fn: PermissionBlockerFn) -> None:
    """Per explicit instruction (2026-10-11): lets app/operations.py's
    dispatch() ask the calling ChatSession, before running ANY tool's real
    handler, whether this tab currently has an un-approved action
    outstanding (an unanswered yes/no permission question, or a
    communication/financial action without a specific prior approval --
    see apps/microagents/PermissionGuard.py in reforce-microagents for the
    actual judgment). Mirrors register_inject_proactive exactly -- same
    per-tab dict, same non-fatal shape (a missing tab just means no
    blocker, never an error). A later session of the same tab replaces
    it."""
    _permission_blocker_by_tab[tab_id] = fn


def get_permission_block(tab_id: str | None) -> str | None:
    """The reason a tool call must not run right now, or None when nothing
    is blocking this tab. Calling this is also how the blocker gets
    consumed/cleared on the ChatSession side (see its own
    _on_permission_block_checked) -- this function itself never caches or
    mutates anything, it just forwards to whatever the tab registered."""
    fn = _permission_blocker_by_tab.get(tab_id) if tab_id else None
    return fn() if fn else None


# Every live ChatSession's send-action guard by tab id (see register_send_action_guard).
_send_action_guard_by_tab: dict[str, SendActionGuardFn] = {}


def register_send_action_guard(tab_id: str, fn: SendActionGuardFn) -> None:
    """Per explicit instruction (2026-10-15), after a real incident:
    app/operations.py's dispatch() calls this, awaited, for a send-type tool
    call BEFORE running its real handler -- a synchronous, blocking
    pre-flight check distinct from register_permission_blocker's cheap
    synchronous flag-read above. That existing check only catches an
    already-known pending block; it has no way to catch a NEW, un-narrated
    send decided silently inside this exact tool call's own arguments (e.g.
    "account": "owner" chosen without ever saying so in visible text) --
    which is exactly what let a real unauthorized send through. The
    registered function summarizes this specific call (ActionSummary.py)
    and feeds that into the SAME turn.permission.guard check
    _check_permission_guard already runs, via its own extra_context
    parameter, returning a block reason if one comes back. Mirrors
    register_permission_blocker's per-tab shape exactly, just async. A later
    session of the same tab replaces it."""
    _send_action_guard_by_tab[tab_id] = fn


async def check_send_action(tab_id: str | None, tool_name: str, args: "dict[str, Any]") -> str | None:
    """The reason this specific send-type call must not run right now, or
    None when it may proceed. A missing tab (no live session registered)
    means no guard to ask -- never blocks on that account alone, same
    non-fatal shape as get_permission_block."""
    fn = _send_action_guard_by_tab.get(tab_id) if tab_id else None
    return await fn(tool_name, args) if fn else None


_file_transfer_guard_by_tab: dict[str, SendActionGuardFn] = {}


def register_file_transfer_guard(tab_id: str, fn: SendActionGuardFn) -> None:
    """Same shape as register_send_action_guard, for a tool call that sends a local
    file off the machine or writes a received one (app/file_facts.py's
    file_transfers_in): dispatch() awaits it before the real handler runs, and the
    session's turn.file.transfer microagent decides (2026-10-22)."""
    _file_transfer_guard_by_tab[tab_id] = fn


async def check_file_transfer(tab_id: str | None, tool_name: str, args: "dict[str, Any]") -> str | None:
    """The reason this call's file transfer must not happen now, or None. No live
    session, no guard -- same non-fatal shape as check_send_action."""
    fn = _file_transfer_guard_by_tab.get(tab_id) if tab_id else None
    return await fn(tool_name, args) if fn else None


def register_send_for_tab(tab_id: str, fn: SendFn) -> None:
    """Per explicit instruction (2026-10-11): lets a background channel
    loop (e.g. app/whatsapp_channel.py's sidecar event handler) push a WS
    message to a specific tab -- a QR code to display, say -- without
    being inside that tab's live turn context at all, unlike get_send()
    above (which needs the contextvar set by whichever turn is currently
    running). A later session of the same tab replaces it, same as
    register_inject_proactive."""
    _send_by_tab[tab_id] = fn


def get_send_for_tab(tab_id: str | None) -> SendFn | None:
    return _send_by_tab.get(tab_id) if tab_id else None


def register_reply_watch(tab_id: str, fn: ReplyWatchFn) -> None:
    """Per explicit instruction (2026-10-12): lets app/operations.py's
    dispatch() tell the calling ChatSession, right after a send-type tool
    call (email_send or any messenger `*_send_message`) completes
    successfully, so it can judge whether a reply is expected and -- for
    email, the one channel with no background polling of its own -- arm
    its own tapering follow-up checks (see chat_session.py's
    _on_outbound_message_sent). Mirrors register_permission_blocker
    exactly: same per-tab dict, same non-fatal shape, a later session of
    the same tab replaces it. `fn` must return immediately (fire-and-
    forget its own async work) -- dispatch() calls it synchronously from
    inside the tool call's own success path."""
    _reply_watch_by_tab[tab_id] = fn


def get_reply_watch(tab_id: str | None) -> ReplyWatchFn | None:
    return _reply_watch_by_tab.get(tab_id) if tab_id else None


# Every live ChatSession's action-closer by tab id (see register_action_closer).
_action_closer_by_tab: dict[str, Callable[[], None]] = {}


def register_action_closer(tab_id: str, fn: Callable[[], None]) -> None:
    """Per explicit instruction (2026-10-18, the action-boundary tracking
    system, point 2 -- "when the operation completes, it closes"): lets
    app/operations.py's dispatch() tell the calling ChatSession, right after
    a send-type tool call it had stashed an action id for (see
    chat_session_mixins/permission_guard.py's own _check_action_before_send)
    completes successfully, so the action can be removed from the
    registry/freed for LRU reuse. Mirrors register_reply_watch exactly: same
    per-tab dict, same non-fatal shape, a later session of the same tab
    replaces it. `fn` must return immediately (no await) -- dispatch() calls
    it synchronously from inside the tool call's own success path."""
    _action_closer_by_tab[tab_id] = fn


def get_action_closer(tab_id: str | None) -> Callable[[], None] | None:
    return _action_closer_by_tab.get(tab_id) if tab_id else None


def register_recent_real_user_directives(tab_id: str, fn: RecentDirectivesFn) -> None:
    """Per explicit instruction (2026-10-13): lets a headless background
    session's own permission-guard check reach a DIFFERENT tab's (the
    primary tab's) recent real user messages -- the approval/prohibition
    "bridge" that keeps a background task honoring an approval the user
    gave in their real conversation, without background-task traffic ever
    diluting that tab's own permission-guard window (see chat_session.py's
    recent_real_user_directives_text, and ChatSession's approval_bridge_
    tab_id). Mirrors register_permission_blocker exactly: same per-tab
    dict, same non-fatal shape, a later session of the same tab replaces
    it."""
    _recent_directives_by_tab[tab_id] = fn


def get_recent_real_user_directives(tab_id: str | None) -> str:
    fn = _recent_directives_by_tab.get(tab_id) if tab_id else None
    return fn() if fn else ""


# The single, process-wide `sessions.get` accessor (main.py's own live-tab
# registry) -- registered once at startup. Lets chat_session_mixins/
# watchdog.py (a ChatSession mixin, which cannot import main.py -- see
# pseudo_tab.py's own module docstring for why) resolve a background
# activity's OWNING real tab back to its live ChatSession, the same
# `get_target_session` callback main.py's own _on_reminder_due already
# passes to pseudo_tab.get_or_create_pseudo_session_sync directly.
_sessions_getter: GetSessionFn | None = None


def register_sessions_getter(fn: "GetSessionFn") -> None:
    """Called once, at startup (main.py), with `sessions.get` itself --
    never per-tab (there is exactly one process-wide session registry)."""
    global _sessions_getter
    _sessions_getter = fn


def get_sessions_getter() -> "GetSessionFn | None":
    return _sessions_getter


def get_cli_pid_sink() -> CliPidSinkFn | None:
    """Per explicit instruction (2026-09-15, "Стоп должен срабатывать
    ВСЕГДА"): lets win_subprocess_patch.py's anyio.open_process wrapper
    report the real OS pid of the CLI subprocess it just spawned straight
    back to the ChatSession that asked for it, captured at the exact
    moment the process comes into existence -- confirmed live as the
    fix for a real Stop failure: the previous fallback introspected the
    SDK's own private `client._transport._process.pid` at stop-time,
    which came back None (force_kill_cli_process_no_pid) even while
    client.disconnect() itself was failing with the SDK's own confirmed
    'NoneType' object has no attribute 'returncode' bug -- i.e. Stop had
    no working path left at all in that incident. Capturing the pid
    directly at spawn time, independent of the SDK's own internal
    bookkeeping, removes that single point of failure. None outside a
    live ChatSession _run_loop iteration -- same non-fatal shape as
    get_tab_id()."""
    return _current_cli_pid_sink.get()


def set_cli_pid_sink(fn: CliPidSinkFn | None) -> contextvars.Token[CliPidSinkFn | None]:
    return _current_cli_pid_sink.set(fn)
