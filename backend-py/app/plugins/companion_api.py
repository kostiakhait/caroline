"""companion_api -- low-level client + operation engine for the Caroline
Android companion app's wire protocol (see the caroline-android-companion
plan). Every call to the phone goes through Camerlengo's session-scoped
variable commands (`var:getMine`/`var:setMine`/`var:getAllMine`/
`var:deleteMine`, all `auth="user_role"`, deployed 2026-09-10), which
auto-prefix every path with the caller's own `caroline/<sw_login>/`
namespace server-side -- this module never builds that prefix itself and
never sees the login.

Split out from companion_plugin.py the same way notes_api.py is split from
notes_plugin.py: the raw request/response plumbing and the operation
engine here, the model-facing tool definitions and their SW-login gating
there.

Auth: a fresh v2 session is minted per call via login_api.get_v2_session()
(same "refresh fresh, never persist the token" choice login_api.py already
made). One automatic retry with a new session on SessionExpiredError,
mirroring notes_api.SessionManager.with_session.

=== Wait/retry protocol (explicit instruction, 2026-09-10) =================

Every companion operation (an SMS send, or a request/response lookup like
listing SMS threads or contacts) is TRANSACTIONAL and two-phase, and NEVER
gives up on its own -- only an explicit user cancel (Stop / stop_operation)
ends it early:

  Phase 1 -- awaiting ACCEPT. The request is written to its own path (e.g.
  sms/outbox/<id>). We poll that SAME path every PHASE1_POLL_INTERVAL_S
  (60s), forever, for the phone to merge a `status: "accepted"` field into
  it (in place -- confirmed no read/write race, since the backend never
  writes that path again after creating it). No timeout in this phase, no
  "phone looks offline" short-circuit -- we just keep checking.

  Phase 2 -- awaiting RESULT. Starts the moment accept is observed. Polls
  a SEPARATE result path (e.g. sms/outbox_result/<id>) on a backoff: an
  immediate check, then 20s, then 40s (cumulative), then every 60s after
  that, capped at PHASE2_BUDGET_S (10 minutes) counted from the ORIGINAL
  ack time (persisted -- see below), not from whenever this process
  happens to be running. If the budget elapses with no result, the
  operation ends in error.

Every operation is journaled to a LOCAL file (companion-operations.json,
same pattern as schedule.json/tab-continuity-*.json -- explicit
instruction: this is Caroline's own bookkeeping, not something to round-
trip through Camerlengo) the moment it starts, updated when accept lands,
and removed when it finally resolves (success, error, or cancel) -- so a
backend restart mid-operation doesn't silently lose it. On startup,
resume_companion_operations() replays the journal: for each still-open
entry it first checks the CURRENT state in Camerlengo (the phone may have
already answered while the backend was down) before resuming the wait
from whatever phase it was in, using the persisted ack time unchanged.
Since the original tool call (and its operation_id) is gone after a
restart, a resumed operation's eventual outcome is delivered as a
PROACTIVE message into the tab that started it, not a tool return.

An explicit cancel (ChatSession.stop() -> operations.py's
REGISTRY.cancel_for_tab(), or a resumed operation's own cancel path) must
also reach the phone -- the corresponding Camerlengo path is deleted so
the phone doesn't act on a cancelled request or write a result into the
void, and the journal entry is removed.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from app.login_api import get_v2_session, is_logged_in
from app.logging_setup import log_event
from app.plugins.sw_api import SessionExpiredError, SwApiError, call_v2

# --- tunables (explicit instruction, 2026-09-10) ---------------------------
PHASE1_POLL_INTERVAL_S = 60.0  # accept-wait cadence -- forever, never gives up
PHASE2_BUDGET_S = 600.0  # 10 minutes, counted from the ack time, not resumed-process start


def _phase2_backoff_delays():
    """20s, 40s (cumulative), then every 60s -- both the "check for a
    result" cadence and the report_progress cadence (no separate faster
    poll underneath it)."""
    yield 20.0
    yield 20.0
    while True:
        yield 60.0


# The tabs/<tabId>/inbox drain + tabs/<tabId>/history sync loop cadence --
# unrelated to the phase1/phase2 protocol above; this is how often the
# background loop wakes up to check for phone-originated inbox messages
# and push any new outgoing history.
INBOX_LOOP_INTERVAL_S = 10.0

# Duplicated from chat_session.py / main.py (both already define it locally,
# same value) -- there's no shared constants module to import it from.
PRIMARY_TAB_ID = "1"

ReportProgress = Callable[[Any], None] | None


class PhoneUnreachableError(Exception):
    """Phase 2's budget elapsed with no result -- the phone accepted the
    request but never finished it in time."""


# --- raw var:*Mine client ----------------------------------------------------

async def _call(command: str, **extra: Any) -> dict[str, Any]:
    """One var:*Mine call with a fresh v2 session, retried once on an
    expired session."""
    session = await get_v2_session()
    try:
        return await call_v2(command, session=session, **extra)
    except SessionExpiredError:
        log_event("plugin:companion", "v2_session_expired_retrying", command=command)
        session = await get_v2_session()
        return await call_v2(command, session=session, **extra)


async def get_mine(path: str) -> Any | None:
    """Reads one leaf value from the caller's own tree. None if it doesn't
    exist yet (Camerlengo returns an ERROR_ELEMENT_NOT_EXIST / "no such
    variable found" for a missing path -- a normal, expected state here,
    not a failure)."""
    try:
        result = await _call("var:getMine", path=path)
    except SwApiError as exc:
        if "no such variable" in str(exc).lower():
            return None
        raise
    return result.get(".value")


async def get_all_mine(path: str = "") -> dict[str, Any]:
    """Reads a whole subtree as a dict ({} if the subtree doesn't exist
    yet). `path` is relative to the caller's own namespace root."""
    try:
        result = await _call("var:getAllMine", path=path)
    except SwApiError as exc:
        if "no such variable" in str(exc).lower():
            return {}
        raise
    value = result.get(".value")
    return value if isinstance(value, dict) else {}


async def set_mine(path: str, value: Any) -> None:
    await _call("var:setMine", path=path, value=value)


async def delete_mine(path: str) -> None:
    await _call("var:deleteMine", path=path)


async def _safe_delete(path: str) -> None:
    try:
        await delete_mine(path)
    except Exception as exc:  # noqa: BLE001 -- best effort, cleanup must never throw
        log_event("plugin:companion", "cleanup_delete_failed", path=path, error=str(exc))


# --- local operations journal (explicit instruction, 2026-09-10: LOCAL file,
#     not Camerlengo -- Caroline's own bookkeeping about her own in-flight
#     operations, not part of the phone-facing protocol) ---------------------

def _journal_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "companion-operations.json"


def _load_journal(workspace_dir: str) -> dict[str, dict[str, Any]]:
    path = _journal_path(workspace_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log_event("plugin:companion", "load_journal_failed", error=str(exc))
        return {}


def _save_journal(workspace_dir: str, journal: dict[str, dict[str, Any]]) -> None:
    try:
        _journal_path(workspace_dir).write_text(json.dumps(journal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception as exc:
        log_event("plugin:companion", "save_journal_failed", error=str(exc))


def _journal_upsert(workspace_dir: str, op_id: str, **fields: Any) -> None:
    journal = _load_journal(workspace_dir)
    entry = journal.get(op_id, {})
    entry.update(fields)
    journal[op_id] = entry
    _save_journal(workspace_dir, journal)


def _journal_remove(workspace_dir: str, op_id: str) -> None:
    journal = _load_journal(workspace_dir)
    if journal.pop(op_id, None) is not None:
        _save_journal(workspace_dir, journal)


# --- the two-phase operation engine ------------------------------------------

async def _await_accept(request_path: str) -> None:
    """Phase 1: checks immediately, then every PHASE1_POLL_INTERVAL_S,
    forever -- no timeout, no "phone looks offline" short-circuit. Only
    ends via the caller's own task being cancelled."""
    while True:
        current = await get_mine(request_path)
        if isinstance(current, dict) and current.get("status") == "accepted":
            return
        await asyncio.sleep(PHASE1_POLL_INTERVAL_S)


async def _await_result(result_path: str, ack_at: float, report_progress: ReportProgress) -> Any:
    """Phase 2: checks immediately, then follows the 20/40/60.. backoff,
    capped at PHASE2_BUDGET_S counted from `ack_at` (NOT from when this
    coroutine started -- resuming after a restart must not reset the
    clock)."""
    deadline = ack_at + PHASE2_BUDGET_S
    value = await get_mine(result_path)
    if value is not None:
        return value
    for delay in _phase2_backoff_delays():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(delay, remaining))
        value = await get_mine(result_path)
        if value is not None:
            return value
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            break
        if report_progress is not None:
            report_progress(f"phone accepted, waiting on the result (~{remaining}s left)")
    raise PhoneUnreachableError(
        f"The phone accepted this request but didn't finish it within {int(PHASE2_BUDGET_S)}s of accepting -- "
        "nothing further was delivered."
    )


async def run_operation(
    workspace_dir: str, tab_id: str, request_path: str, result_path: str,
    request_payload: dict[str, Any] | None, label: str,
    report_progress: ReportProgress = None, op_id: str | None = None,
) -> Any:
    """Runs one full two-phase operation. `op_id` + `request_payload=None`
    means RESUME an already-journaled, already-written operation (used by
    resume_companion_operations after a restart) -- otherwise this starts
    a brand new one (writes the request, journals it fresh).

    On cancellation (asyncio.CancelledError, from REGISTRY.cancel_for_tab):
    deletes both Camerlengo paths so the phone doesn't act on it or write a
    result into the void, clears the journal entry, and re-raises the bare
    CancelledError (not a substitute exception) so REGISTRY still records
    this operation as "cancelled", not "error"."""
    op_id = op_id or uuid.uuid4().hex
    is_resume = request_payload is None
    try:
        if not is_resume:
            await set_mine(request_path, {**request_payload, "ts": int(time.time() * 1000)})
            _journal_upsert(
                workspace_dir, op_id, tab_id=tab_id, label=label,
                request_path=request_path, result_path=result_path,
                phase="awaiting_accept", created_at=time.time(),
            )
            log_event("plugin:companion", "operation_started", op_id=op_id, label=label)

        journal = _load_journal(workspace_dir)
        entry = journal.get(op_id, {})
        ack_at = entry.get("ack_at")

        if entry.get("phase") != "awaiting_result":
            await _await_accept(request_path)
            ack_at = time.time()
            _journal_upsert(workspace_dir, op_id, phase="awaiting_result", ack_at=ack_at)
            log_event("plugin:companion", "operation_accepted", op_id=op_id)

        assert ack_at is not None
        result = await _await_result(result_path, ack_at, report_progress)
        log_event("plugin:companion", "operation_resolved", op_id=op_id)
        return result
    except asyncio.CancelledError:
        # Re-raise the bare CancelledError (not a substitute exception) so
        # app/operations.py's REGISTRY still records this operation as
        # "cancelled", not "error" -- cleanup runs here regardless of what
        # eventually observes it (a live REGISTRY.cancel_for_tab(), or
        # nothing at all for a resumed background task, which isn't
        # currently tracked in REGISTRY -- see resume_companion_operations'
        # own docstring for that known gap).
        log_event("plugin:companion", "operation_cancelled", op_id=op_id)
        await _safe_delete(request_path)
        await _safe_delete(result_path)
        _journal_remove(workspace_dir, op_id)
        raise
    except PhoneUnreachableError:
        await _safe_delete(request_path)
        await _safe_delete(result_path)
        _journal_remove(workspace_dir, op_id)
        raise


async def _finish_and_cleanup(workspace_dir: str, op_id: str, request_path: str, result_path: str) -> None:
    await _safe_delete(request_path)
    await _safe_delete(result_path)
    _journal_remove(workspace_dir, op_id)


# --- model-facing operation constructors -------------------------------------

async def send_sms(workspace_dir: str, tab_id: str, to: str, text: str, report_progress: ReportProgress) -> dict[str, Any]:
    op_id = uuid.uuid4().hex
    request_path = f"sms/outbox/{op_id}"
    result_path = f"sms/outbox_result/{op_id}"
    result = await run_operation(
        workspace_dir, tab_id, request_path, result_path,
        {"to": to, "text": text}, f"SMS to {to}", report_progress, op_id,
    )
    await _finish_and_cleanup(workspace_dir, op_id, request_path, result_path)
    return result if isinstance(result, dict) else {"ok": False, "error": "malformed result from phone"}


async def request_response(
    workspace_dir: str, tab_id: str, family: str, payload: dict[str, Any], report_progress: ReportProgress,
) -> Any:
    op_id = uuid.uuid4().hex
    request_path = f"{family}/requests/{op_id}"
    result_path = f"{family}/responses/{op_id}"
    label = f"{family} {payload.get('op', 'request')}"
    result = await run_operation(workspace_dir, tab_id, request_path, result_path, payload, label, report_progress, op_id)
    await _finish_and_cleanup(workspace_dir, op_id, request_path, result_path)
    return result


# --- restart-survival: resume whatever the journal says is still open -------

InjectToTab = Callable[[str, str], bool]


async def resume_companion_operations(workspace_dir: str, inject_to_tab: InjectToTab) -> None:
    """Called once at backend startup. For every still-open journal entry:
    resumes waiting (from whatever phase it was in, ack time unchanged),
    and delivers the eventual outcome as a proactive message into the tab
    that started it -- there's no live tool call left to return it to."""
    journal = _load_journal(workspace_dir)
    if not journal:
        return
    log_event("plugin:companion", "resuming_operations", count=len(journal))
    for op_id, entry in list(journal.items()):
        asyncio.create_task(_resume_one(workspace_dir, op_id, entry, inject_to_tab))


async def _resume_one(workspace_dir: str, op_id: str, entry: dict[str, Any], inject_to_tab: InjectToTab) -> None:
    tab_id = entry.get("tab_id")
    label = entry.get("label", "a companion request")
    request_path = entry.get("request_path")
    result_path = entry.get("result_path")
    if not (tab_id and request_path and result_path):
        log_event("plugin:companion", "resume_entry_malformed", op_id=op_id)
        _journal_remove(workspace_dir, op_id)
        return
    try:
        result = await run_operation(workspace_dir, tab_id, request_path, result_path, None, label, None, op_id)
        await _finish_and_cleanup(workspace_dir, op_id, request_path, result_path)
        inject_to_tab(
            tab_id,
            f"[The '{label}' request you started before Caroline's backend last restarted has now completed. "
            f"Result: {result}]",
        )
    except asyncio.CancelledError:
        pass  # already cleaned up inside run_operation; nothing to tell the user
    except Exception as exc:  # noqa: BLE001
        inject_to_tab(
            tab_id,
            f"[The '{label}' request you started before Caroline's backend last restarted did not complete: {exc}]",
        )


# --- engine-level tab wiring (started from main.py, next to the scheduler
#     due-check loop -- see companion_plugin.py's docstring) -------------------

HistorySnapshot = Callable[[], list[dict[str, Any]]]


def _history_cursor_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "companion-history-cursor.json"


def _load_history_cursor(workspace_dir: str) -> dict[str, int]:
    """Per-tab count of how many history entries have already been pushed
    to the phone -- lets the sync send only NEW entries each tick instead
    of re-transferring the whole transcript. Mirrors
    ratatosk_channel.py's own cursor-file pattern."""
    path = _history_cursor_path(workspace_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log_event("plugin:companion", "load_history_cursor_failed", error=str(exc))
        return {}


def _save_history_cursor(workspace_dir: str, cursors: dict[str, int]) -> None:
    try:
        _history_cursor_path(workspace_dir).write_text(json.dumps(cursors, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        log_event("plugin:companion", "save_history_cursor_failed", error=str(exc))


async def _sync_history(workspace_dir: str, history_snapshot: HistorySnapshot) -> None:
    """Pushes only the entries the phone hasn't received yet, one per key
    under tabs/<tabId>/history/<index> (not one overwritten blob). Primary
    tab only for v1 (history_snapshot only knows how to read the single
    most-recently-active session file)."""
    try:
        entries = history_snapshot()
        cursors = _load_history_cursor(workspace_dir)
        already_sent = cursors.get(PRIMARY_TAB_ID, 0)
        new_entries = entries[already_sent:]
        if not new_entries:
            return
        for i, entry in enumerate(new_entries, start=already_sent):
            await set_mine(f"tabs/{PRIMARY_TAB_ID}/history/{i}", entry)
        cursors[PRIMARY_TAB_ID] = len(entries)
        _save_history_cursor(workspace_dir, cursors)
        log_event("plugin:companion", "history_synced", tab_id=PRIMARY_TAB_ID, new_entries=len(new_entries), total=len(entries))
    except Exception as exc:  # noqa: BLE001 -- best effort, never break the loop
        log_event("plugin:companion", "history_sync_failed", error=str(exc))


async def _drain_inbox(inject_to_tab: InjectToTab) -> None:
    try:
        tabs = await get_all_mine("tabs")
    except Exception as exc:  # noqa: BLE001
        log_event("plugin:companion", "inbox_read_failed", error=str(exc))
        return
    for tab_id, tab_data in (tabs.items() if isinstance(tabs, dict) else []):
        inbox = tab_data.get("inbox") if isinstance(tab_data, dict) else None
        if not isinstance(inbox, dict):
            continue
        for msg_id, msg in list(inbox.items()):
            text = msg.get("text") if isinstance(msg, dict) else None
            if not isinstance(text, str) or not text.strip():
                await _safe_delete(f"tabs/{tab_id}/inbox/{msg_id}")
                continue
            try:
                delivered = inject_to_tab(tab_id, text)
            except Exception as exc:  # noqa: BLE001
                log_event("plugin:companion", "inbox_inject_failed", tab_id=tab_id, msg_id=msg_id, error=str(exc))
                delivered = False
            if delivered:
                await _safe_delete(f"tabs/{tab_id}/inbox/{msg_id}")
                log_event("plugin:companion", "inbox_message_injected", tab_id=tab_id, msg_id=msg_id)


async def _inbox_loop_tick(workspace_dir: str, inject_to_tab: InjectToTab, history_snapshot: HistorySnapshot) -> None:
    if not is_logged_in():
        return  # not paired to any SW account yet -- nothing to sync
    await _sync_history(workspace_dir, history_snapshot)
    await _drain_inbox(inject_to_tab)


def start_companion_inbox_loop(
    workspace_dir: str, inject_to_tab: InjectToTab, history_snapshot: HistorySnapshot,
    interval_s: float = INBOX_LOOP_INTERVAL_S,
) -> "asyncio.Task[None]":
    """Started once from main.py's startup hook (needs a running loop),
    same shape as scheduler_plugin.start_due_check_loop and
    ratatosk_channel.start_ratatosk_owner_channel."""
    log_event("plugin:companion", "inbox_loop_starting", interval_s=interval_s)

    async def _loop() -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await _inbox_loop_tick(workspace_dir, inject_to_tab, history_snapshot)
            except Exception as exc:  # noqa: BLE001 -- a bad tick must never kill the loop
                log_event("plugin:companion", "inbox_loop_tick_failed", error=str(exc))

    return asyncio.create_task(_loop())
