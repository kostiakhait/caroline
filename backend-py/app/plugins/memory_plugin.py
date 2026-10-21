"""memory -- save_info / request_info: Caroline's memory as the main model
sees it. Two tools and nothing about how memory is arranged behind them (see
docs/MICROAGENTS_PLAN.md).

Behind the tools is the microagent memory service on Camerlengo
(memory:save / memory:request and friends): a dispatcher microagent decides
which memories a text belongs in or where an answer may be -- long-term
facts, topic tracks, the Notes vault and profile -- and each one keeps or
finds its own part. Replies come back marked by source; putting them together
is the main model's job.

WHAT goes into memory is decided by the main model alone, and it passes it
itself in the tool call. Nothing here fetches anything on its own: no letter
is pulled by id, no thread is read, no folder is scanned. The single thing
code adds is the bytes of a document whose path the model named explicitly
-- a tool argument cannot carry a binary file.

Requires the user's SquirrelWisdom account (same gate as every other
SW-backed tool). Memory costs money (the server charges the wallet per
request): when the wallet is empty the top-up window opens by itself on the
first refusal; when the service's own provider is out of money the turn is
stopped and started again from the user's message once it is back (see
memory_turns.py and ChatSession._on_memory_upstream_no_funds).

The local short-term copy of topics (memory_topics.py, shown in the system
prompt) is kept current from here: every save_info reply updates the topic it
touched, and the whole list is refreshed from the server when it is stale.

Documents are a memory of their own (docs/MICROAGENTS_PLAN.md, section 2.9):
save_document(path, note?) hands a whole file to the server, which keeps it,
reads its text itself (scans too), indexes it and puts its facts into fact
memory. The model chooses to do that; nothing saves a document on its own.
A found document comes back from request_info like everything else, and a
part of it is read in full with request_info(document, section).

Neither tool keeps the model waiting long:
  - save_info hands the text to a background task and returns at once -- the
    model has the facts in the conversation already. The save goes on even
    if the user presses Stop (half of it may already be on the server); only
    if it fails is the model told, by a service message.
  - request_info has a SOFT timeout (SOFT_READ_TIMEOUT_S). The server sends
    the answer in parts as it finds them (memory:requestStart /
    memory:requestPoll); whatever arrived by the timeout is the tool's reply,
    and the read goes on in the background: every later part is handed to
    the model as soon as it arrives, as a service message starting with
    LATE_FACTS_HEADER. Stop ends such a read like any other tool call.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.http_tls import ssl_context
from app.context_cap import truncate
from app import memory_save_queue
from app import memory_turns
from app.logging_setup import log_event
from app.memory_topics import cached_topic_detail, is_stale, recent_topics, replace_topics, set_current_topic, topic_description, update_topic
from app.operations import REGISTRY as OPERATIONS
from app.plugins.loader import Plugin, PluginTool
from app.plugins.notes_api import SessionManager
from app.plugins.sw_api import API_URL, CAROLINE_SW_KEY, SessionExpiredError, SwApiError
from app.session_context import get_inject_proactive, get_send, get_tab_id
from app.sw_gate import require_sw_or_prompt
from app.task_supervisor import supervise
from app.window_registry import close_windows_for_topic
from app.workspace_dir import WORKSPACE_DIR

_sessions = SessionManager()

# One memory request may run many small-model calls on the server, whose own
# budget is five minutes. Exactly ONE attempt, never a retry on timeout: a
# save that was received but answered late would otherwise be saved twice.
REQUEST_TIMEOUT_S = 330.0
# request_info: what memory found within this time is the tool's reply; the
# read goes on, and what it finds later reaches the model as a service message.
SOFT_READ_TIMEOUT_S = 30.0
POLL_WAIT_S = 20.0                      # one memory:requestPoll waits at most this (the server's own cap)
LATE_READ_LIMIT_S = 400.0               # the server gives up on a request after 300 s
# A save runs on the server as a background job that is polled (memory:saveStart
# / memory:savePoll): one long request was cut off by the front server at 120 s
# while the save itself succeeded, and the model, told it had failed, saved the
# same text again -- up to four times (seen live 2026-10-10).
SAVE_WAIT_LIMIT_S = 400.0               # a save is given up on as "outcome unknown" after this
SAVE_POLL_RETRIES = 5                   # consecutive failed polls tolerated (flat pause between them)
SAVE_POLL_RETRY_PAUSE_S = 3.0
OUTCOME_UNKNOWN = "outcome_unknown"
# Per explicit instruction (2026-10-13): the durable save queue's own
# background drain tick -- cheap (a memory:savePoll against an already-
# started job costs nothing extra), so this can run often; it's the pure
# durability backstop for the eager _drain_one every save_info already fires
# (see save_info's own docstring) -- a no-op on almost every tick.
SAVE_DRAIN_INTERVAL_S = 15.0
# Per explicit instruction (2026-10-13): the background topic-prefetch
# loop's own tick. Not the main cache-warming mechanism -- every ordinary
# save_info/request_info(query=...) already warms the cache for free (see
# _cache_topics_from_parts) -- this only catches a "today"-tier topic this
# process never happened to touch yet (e.g. last updated from a different
# tab; topic memory is shared process-wide). 10 minutes: frequent enough
# that a newly-surfaced cold topic is warm well before it's likely to be
# asked about; not shorter than STALE_AFTER_S (memory_topics.py, 15 min --
# the slim "today" list itself doesn't change faster than that, so scanning
# more often is pure waste). A cost/latency knob, not a derived value --
# lower it if live use shows the model hitting a cold topic before prefetch
# gets to it (a one-time fallback-latency cost, not a correctness problem).
PREFETCH_INTERVAL_S = 600.0
LATE_FACTS_HEADER = "[Additional facts from memory"
LATE_READ_TOOL = "request_info (continued)"   # its operation is cancelled by Stop like any tool call
MAX_FILE_BYTES = 25 * 1024 * 1024       # the server refuses larger documents
MATERIAL_KINDS = ("message", "document")
MATERIAL_TEXT_FIELDS = ("source", "ref", "ts", "from", "subject", "name", "mime", "text")
SOURCE_TOPICS = "topics"

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_NO_FUNDS_USER = "no_funds_user"
STATUS_NO_FUNDS_UPSTREAM = "no_funds_upstream"


class MemoryInputError(Exception):
    """What the model passed cannot be sent; the message is for the model."""


async def _post(body: dict[str, Any]) -> dict[str, Any]:
    # Per explicit instruction (2026-10-10), after two silent save_info failures
    # (120.8s and 70.9s) whose own cause was never logged -- only the final
    # {"status": "error"} survived, the real reason only reached the model as
    # an injected chat message (its length logged, never its text). This is
    # the ONE choke point every memory: command goes through, so logging the
    # reason right here, before it is turned into an exception that a given
    # call site may or may not log on its way up, catches every cause (a
    # transient 502, an expired session, a malformed command) regardless of
    # which higher-level path loses or truncates it.
    command = body.get("command")
    try:
        async with httpx.AsyncClient(verify=ssl_context(), timeout=REQUEST_TIMEOUT_S) as client:
            response = await client.post(API_URL, json=body)
    except Exception as exc:  # noqa: BLE001 -- logged here, re-raised for the caller to handle as before
        log_event("plugin:memory", "api_call_failed", command=command, reason=f"transport error: {exc}")
        raise
    if response.status_code >= 400:
        log_event("plugin:memory", "api_call_failed", command=command, http_status=response.status_code, body=response.text[:500])
        raise SwApiError(f'SquirrelWisdom API HTTP {response.status_code} for command "{command}"')
    envelope = response.json()
    if envelope.get(".status") != "ok":
        reason = envelope.get(".reason", json.dumps(envelope))
        log_event("plugin:memory", "api_call_failed", command=command, reason=reason)
        if isinstance(reason, str) and "session" in reason.lower():
            raise SessionExpiredError(reason)
        raise SwApiError(f'command "{command}" failed: {reason}')
    return envelope


async def _memory(command: str, **params: Any) -> dict[str, Any]:
    """One memory: command for the logged-in user. Returns the service's
    reply: {status, output, error?, usage, traceId, chargedPia}. Every
    request carries the id of the turn it belongs to (memory_turns.py)."""
    turn_id = memory_turns.current_turn_id(get_tab_id())
    if turn_id:
        params.setdefault("turnId", turn_id)
    return await _sessions.with_session(
        lambda session: _post({"command": command, "key": CAROLINE_SW_KEY, "session": session, **params})
    )


async def _start_save(start_command: str, **params: Any) -> dict[str, Any]:
    """memory:saveStart only -- the raw reply (may lack "jobId": refused
    before it ever ran, e.g. no funds). Factored out of the old _save_job
    (2026-10-13) so the durable save queue (memory_save_queue.py) can
    persist job_id the moment it's known, BEFORE polling begins -- see
    _drain_one's own docstring for why that ordering is what makes a
    restart resume the SAME server job instead of starting a duplicate."""
    return await _memory(start_command, **params)


async def _poll_save(job_id: str) -> dict[str, Any]:
    """Waits for a started save's outcome with short polls. Returns the
    service's reply, as a plain memory:save would -- or an error with code
    OUTCOME_UNKNOWN when the outcome could not be learned (never to be
    taken for "not saved"). Unchanged polling logic, factored out of the
    old _save_job (2026-10-13) as the half that needs to be resumable
    across a restart (see _start_save's own docstring)."""
    deadline = time.monotonic() + SAVE_WAIT_LIMIT_S
    failures = 0
    while time.monotonic() < deadline:
        try:
            reply = await _memory("memory:savePoll", jobId=job_id, wait=POLL_WAIT_S)
        except Exception as exc:  # noqa: BLE001 -- a lost poll is not a lost save
            if "No such memory request" in str(exc):
                return {"status": "error", "error": {"code": OUTCOME_UNKNOWN, "message": "the server no longer knows this save (it may have restarted)", "job_lost": True}}
            failures += 1
            if failures > SAVE_POLL_RETRIES:
                return {"status": "error", "error": {"code": OUTCOME_UNKNOWN, "message": f"the server stopped answering about this save: {exc}"}}
            await asyncio.sleep(SAVE_POLL_RETRY_PAUSE_S)
            continue
        failures = 0
        if reply.get("done"):
            return reply
    return {"status": "error", "error": {"code": OUTCOME_UNKNOWN, "message": f"the save did not report its outcome within {int(SAVE_WAIT_LIMIT_S)} s"}}


async def _save_job(start_command: str, **params: Any) -> dict[str, Any]:
    """Starts a save and waits for its outcome -- the original one-shot
    shape, kept for save_owner_profile_text (the owner's profile isn't
    durably queued; a lost follow-up there is lower-stakes and that call
    site predates this module's queue). save_info itself no longer calls
    this -- see _drain_one, which calls _start_save/_poll_save directly so
    it can persist job_id between the two."""
    started = await _start_save(start_command, **params)
    job_id = started.get("jobId")
    if not job_id:
        return started
    return await _poll_save(job_id)


def _language() -> str | None:
    """The language of the conversation in the calling tab, as Caroline
    already tracks it. Fact memory works in English inside and translates
    what it found into this language on the way out."""
    tab_id = get_tab_id()
    if not tab_id:
        return None
    from app.chat_session import current_language_name  # late: chat_session loads the plugins
    return current_language_name(tab_id)


def _build_material(raw: Any, index: int) -> dict[str, Any]:
    """A material exactly as the model gave it. For a document with "path",
    the bytes of that one file are attached; nothing else is read."""
    where = f"materials[{index}]"
    if not isinstance(raw, dict):
        raise MemoryInputError(f"{where} must be an object")
    kind = raw.get("kind")
    if kind not in MATERIAL_KINDS:
        raise MemoryInputError(f'{where}: "kind" must be one of {", ".join(MATERIAL_KINDS)}')
    material: dict[str, Any] = {"kind": kind}
    for field in MATERIAL_TEXT_FIELDS:
        value = raw.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise MemoryInputError(f'{where}: "{field}" must be a string')
        material[field] = value
    if raw.get("to") is not None:
        to = raw["to"]
        if isinstance(to, str):
            to = [to]
        if not isinstance(to, list) or not all(isinstance(item, str) for item in to):
            raise MemoryInputError(f'{where}: "to" must be a list of strings')
        material["to"] = to
    path = raw.get("path")
    if path is not None:
        if kind != "document":
            raise MemoryInputError(f'{where}: "path" is only for a document')
        file = Path(str(path)).expanduser()
        if not file.is_file():
            raise MemoryInputError(f"{where}: there is no file at {path}")
        size = file.stat().st_size
        if size > MAX_FILE_BYTES:
            raise MemoryInputError(f"{where}: {path} is {size} bytes, larger than the {MAX_FILE_BYTES} allowed")
        material["content_b64"] = base64.b64encode(file.read_bytes()).decode("ascii")
        material.setdefault("name", file.name)
        material.setdefault("source", "file")
        material.setdefault("ref", str(file))
        if "mime" not in material:
            guessed, _encoding = mimetypes.guess_type(file.name)
            if guessed:
                material["mime"] = guessed
    if not material.get("text") and "content_b64" not in material:
        raise MemoryInputError(f'{where}: give its "text" (and, for a document file, its "path")')
    return material


# The top-up window is opened by itself on the FIRST refusal for an empty
# wallet, then not again until memory has worked once more -- same manners as
# sw_gate's login window.
_topup_window_shown = False


async def _open_topup_window() -> bool:
    """True when the window was opened by this call."""
    global _topup_window_shown
    if _topup_window_shown:
        return False
    _topup_window_shown = True
    try:
        from app.subscription_mode import create_topup_checkout_url, invalidate_sw_status
        checkout_url = await create_topup_checkout_url()
        invalidate_sw_status()  # the balance is about to change on purpose
        await get_send()({"type": "open_payment", "requestId": uuid.uuid4().hex, "checkoutUrl": checkout_url})
        log_event("plugin:memory", "topup_window_opened")
        return True
    except Exception as exc:
        log_event("plugin:memory", "topup_window_failed", error=str(exc))
        return False


async def _react_to_status(reply: dict[str, Any]) -> str | None:
    """What Caroline itself does about a money refusal, beyond telling the
    model; returns a sentence to add to the message for the model."""
    global _topup_window_shown
    status = reply.get("status")
    if status in (STATUS_OK, STATUS_EMPTY):
        _topup_window_shown = False
        return None
    if status == STATUS_NO_FUNDS_USER:
        if await _open_topup_window():
            return "I've opened the top-up window for the user -- tell them, and continue once they have topped up."
        return "The top-up window was already opened for this; point the user to Settings -> Account & Billing."
    if status == STATUS_NO_FUNDS_UPSTREAM:
        reason = (reply.get("error") or {}).get("message") or "the memory service's provider is out of funds"
        if memory_turns.notify_upstream_no_funds(get_tab_id(), reason):
            return ("This turn is being stopped now and will be started again from the user's message by itself "
                    "once the service has funds; do nothing more in this turn.")
    return None


def _failure_text(reply: dict[str, Any], doing: str) -> str | None:
    """A message for the model when the whole request failed, else None."""
    status = reply.get("status")
    if status in (STATUS_OK, STATUS_EMPTY):
        return None
    if status == STATUS_NO_FUNDS_USER:
        return f"{doing} was refused: the user's SquirrelWisdom balance is too low. Memory is unavailable until they top up; do not retry."
    if status == STATUS_NO_FUNDS_UPSTREAM:
        return f"{doing} could not run: the memory service itself is out of funds on its provider's side. Retrying will not help."
    error = reply.get("error") or {}
    if error.get("code") == OUTCOME_UNKNOWN:
        return (f"{doing}: it is NOT KNOWN whether it went through ({error.get('message') or 'no details'}). Do not "
                "simply do it again -- that would store the same thing twice. If it matters, check with request_info "
                "first, and repeat only what is really missing.")
    return f"{doing} failed ({error.get('code') or status}): {error.get('message') or 'no details'}"


def _reply_text(reply: dict[str, Any]) -> str:
    payload = {"status": reply.get("status"), **(reply.get("output") or {})}
    return json.dumps(payload, ensure_ascii=False)


async def _refresh_topics_if_stale() -> None:
    """Best effort; the prompt's topic list simply stays as it was on failure."""
    if not is_stale(WORKSPACE_DIR):
        return
    try:
        reply = await _memory("memory:topics", tiers=["today", "week"])
        if reply.get("status") in (STATUS_OK, STATUS_EMPTY):
            replace_topics(WORKSPACE_DIR, (reply.get("output") or {}).get("tiers") or {})
    except Exception as exc:
        log_event("plugin:memory", "topics_refresh_failed", error=str(exc))


async def _gate() -> dict[str, Any] | None:
    gate = await require_sw_or_prompt(get_send())
    return None if gate.ok else {"text": gate.message, "is_error": True}


async def save_owner_profile_text(text: str) -> dict[str, Any]:
    """memory:saveProfile -- used by owner_profile_plugin.py's owner_profile_remember.
    The owner's profile is never reached through save_info: it is chosen on purpose."""
    refused = await _gate()
    if refused:
        return refused
    text = (text or "").strip()
    if not text:
        return {"text": 'owner_profile_remember needs "text": what to keep about the owner.', "is_error": True}
    reply = await _save_job("memory:saveProfileStart", text=text, traceId=uuid.uuid4().hex, language=_language())
    reaction = await _react_to_status(reply)
    failure = _failure_text(reply, "Saving to the owner's profile")
    if failure:
        return {"text": " ".join(filter(None, [failure, reaction])), "is_error": True}
    return {"text": _reply_text(reply)}


async def request_owner_profile(query: str) -> dict[str, Any]:
    """memory:requestProfile -- used by owner_profile_plugin.py's owner_profile_recall."""
    refused = await _gate()
    if refused:
        return refused
    query = (query or "").strip()
    if not query:
        return {"text": 'owner_profile_recall needs "query": what you want to know about the owner.', "is_error": True}
    reply = await _memory("memory:requestProfile", query=query, traceId=uuid.uuid4().hex, language=_language())
    reaction = await _react_to_status(reply)
    failure = _failure_text(reply, "Reading the owner's profile")
    if failure:
        return {"text": " ".join(filter(None, [failure, reaction])), "is_error": True}
    if reply.get("status") == STATUS_EMPTY:
        return {"text": f'The owner\'s profile holds nothing about "{query}".'}
    return {"text": _reply_text(reply)}


async def save_info(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    text = (args.get("text") or "").strip()
    if not text:
        return {"text": 'save_info needs "text": what to remember, in your own words.', "is_error": True}
    try:
        materials = [_build_material(raw, index) for index, raw in enumerate(args.get("materials") or [])]
    except MemoryInputError as exc:
        return {"text": f"Nothing was saved: {exc}", "is_error": True}
    params: dict[str, Any] = {"text": text, "traceId": uuid.uuid4().hex, "language": _language()}
    if materials:
        params["materials"] = materials
    # Per explicit instruction (2026-10-15): carried through the durable
    # queue only to let _drain_one mark the topic this save lands in as this
    # TAB's current track (memory_topics.set_current_topic) once the save
    # finishes -- popped back out before the params are ever sent to the
    # server (see _drain_one), so this never reaches memory:saveStart itself.
    tab_id = get_tab_id()
    if tab_id:
        params["_tab_id"] = tab_id
    # Per explicit instruction (2026-10-13): durably enqueued BEFORE
    # anything else -- see memory_save_queue.py's own module docstring for
    # the restart-survival this fixes. Then an eager _drain_one fires right
    # away (not awaited: the model has what it saves in the conversation
    # already), same responsiveness as before -- the periodic drain loop
    # (start_save_drain_loop) is the pure durability backstop, a no-op on
    # almost every tick since this eager attempt already finishes first in
    # the overwhelming majority of cases. A plain task, not an operation of
    # the tab, so Stop does not cancel it.
    entry_id = memory_save_queue.enqueue(WORKSPACE_DIR, params)
    task = asyncio.create_task(_drain_one(entry_id, get_inject_proactive()))
    _background_saves.add(task)
    task.add_done_callback(_background_saves.discard)
    log_event("plugin:memory", "save_started", trace_id=params["traceId"], materials=len(materials), queue_id=entry_id)
    return {"text": "Saving to memory in the background; you do not need to wait for it. You will be told only if it fails."}


_background_saves: set[asyncio.Task[Any]] = set()   # strong references: a bare task may be collected mid-way
# Per explicit instruction (2026-10-13): entry ids currently being drained in
# THIS process -- guards against the eager _drain_one (fired straight from
# save_info) and the periodic sweep (start_save_drain_loop) both picking up
# the same still-in-flight entry and double-polling/double-starting it.
_draining: set[str] = set()


async def _drain_one(entry_id: str, inject: Any = None) -> None:
    """Processes one durably-queued save to completion (or to a retryable
    OUTCOME_UNKNOWN, left queued for the next tick). Per explicit
    instruction (2026-10-13): resumes an existing job_id if one was already
    persisted (a restart recovering mid-save) instead of starting a fresh
    memory:saveStart -- the fix for the old "saved the same text four
    times" duplicate-save risk across a restart specifically. Replaces the
    old _save_in_background; same aftermath (topic-cache update, failure
    notification), now keyed off the durable queue entry instead of a bare
    in-memory params dict."""
    if entry_id in _draining:
        return
    _draining.add(entry_id)
    try:
        entry = memory_save_queue.get(WORKSPACE_DIR, entry_id)
        if entry is None:
            return
        params = dict(entry["params"])
        tab_id = params.pop("_tab_id", None)
        started = time.monotonic()
        job_id = entry.get("job_id")
        try:
            if not job_id:
                start_reply = await _start_save("memory:saveStart", **params)
                job_id = start_reply.get("jobId")
                if job_id:
                    memory_save_queue.set_job_id(WORKSPACE_DIR, entry_id, job_id)
                    reply = await _poll_save(job_id)
                else:
                    reply = start_reply  # refused before it ever ran (e.g. no funds) -- nothing to poll
            else:
                reply = await _poll_save(job_id)
        except Exception as exc:  # noqa: BLE001 -- reported to the model below, never raised into nothing
            reply = {"status": "error", "error": {"code": "exception", "message": str(exc)}}

        try:
            # Only the top-up window: the turn that called save_info may be long
            # over, so an upstream no-funds must not stop and replay it (the
            # model is told below that the save failed).
            reaction = await _react_to_status(reply) if reply.get("status") in (STATUS_OK, STATUS_EMPTY, STATUS_NO_FUNDS_USER) else None
            for result in (reply.get("output") or {}).get("results") or []:
                if result.get("source") == SOURCE_TOPICS and result.get("status") == STATUS_OK:
                    saved_topic = (result.get("output") or {}).get("topic") or {}
                    update_topic(WORKSPACE_DIR, saved_topic)
                    if tab_id and saved_topic.get("id"):
                        set_current_topic(WORKSPACE_DIR, tab_id, saved_topic["id"])
            await _refresh_topics_if_stale()
        except Exception as exc:  # noqa: BLE001
            reaction = None
            log_event("plugin:memory", "save_aftermath_failed", error=str(exc))
        log_event("plugin:memory", "save_finished", trace_id=params.get("traceId"), status=reply.get("status"),
                  ms=round((time.monotonic() - started) * 1000), error=reply.get("error"), queue_id=entry_id)

        error = reply.get("error") or {}
        if error.get("code") == OUTCOME_UNKNOWN:
            memory_save_queue.mark_attempt(WORKSPACE_DIR, entry_id)
            if error.get("job_lost"):
                memory_save_queue.clear_job_id(WORKSPACE_DIR, entry_id)
            failure = _failure_text(reply, "Saving to memory in the background")
            # Notify once, not on every ~15s retry tick while it stays unknown.
            if failure and inject is not None and not entry.get("notified_failure_at"):
                memory_save_queue.mark_notified(WORKSPACE_DIR, entry_id, time.time())
                inject(
                    "[Service message about a save_info you called earlier. " + failure
                    + f"\nWhat was to be saved: {params['text'][:2000]}\nTell the user only if it matters to them.]"
                )
            return  # stays queued -- drained again on the next tick

        memory_save_queue.remove(WORKSPACE_DIR, entry_id)  # genuinely done: success OR a definitive failure
        failure = _failure_text(reply, "Saving to memory in the background")
        if failure and inject is not None:
            inject(
                "[Service message about a save_info you called earlier. " + " ".join(filter(None, [failure, reaction]))
                + f"\nWhat was to be saved: {params['text'][:2000]}\nTell the user only if it matters to them.]"
            )
    finally:
        _draining.discard(entry_id)


def start_save_drain_loop(interval_s: float = SAVE_DRAIN_INTERVAL_S) -> "asyncio.Task[None]":
    """Per explicit instruction (2026-10-13): the durable save queue's own
    background drain -- the pure durability backstop for save_info's own
    eager _drain_one (see that function's docstring). Runs once immediately
    (drains anything left over from a crash, right at backend startup)
    before settling into its own flat interval. get_inject_proactive()
    returns None outside a live ChatSession turn (confirmed via
    session_context.py's own documented no-context behavior) -- _drain_one
    already tolerates inject=None, same as every other call site here."""

    async def _tick() -> None:
        for entry in memory_save_queue.all_entries(WORKSPACE_DIR):
            if entry["id"] not in _draining:
                await _drain_one(entry["id"], get_inject_proactive())

    async def _loop() -> None:
        await _tick()
        while True:
            await asyncio.sleep(interval_s)
            await _tick()

    log_event("plugin:memory", "save_drain_loop_starting", interval_s=interval_s)
    return supervise("memory_save_drain", _loop)


async def close_track(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    """memory:topicClose -- see TopicMemory.py's own docstring, section
    "Status". Per explicit instruction (2026-10-15): the only way a track is
    marked done; saving a new episode into it (save_info) reopens it on its
    own, so there is no separate "reopen" tool."""
    refused = await _gate()
    if refused:
        return refused
    topic_id = (args.get("topic") or "").strip()
    if not topic_id:
        return {"text": 'close_track needs "topic": the id of the track to close, from your own topic list.', "is_error": True}
    reply = await _memory("memory:topicClose", topic=topic_id, traceId=uuid.uuid4().hex)
    reaction = await _react_to_status(reply)
    failure = _failure_text(reply, "Closing that track")
    if failure:
        return {"text": " ".join(filter(None, [failure, reaction])), "is_error": True}
    topic = (reply.get("output") or {}).get("topic") or {}
    if topic.get("id"):
        update_topic(WORKSPACE_DIR, topic)
    # Per explicit instruction (2026-10-16): closing a track closes the
    # windows that belong to it (viewer/browser windows opened while this
    # was the tab's current track) -- see window_registry.close_windows_
    # for_topic's own docstring. Best-effort: a window that fails to close
    # doesn't stop the track itself from being reported closed.
    try:
        closed_windows = await close_windows_for_topic(topic_id)
    except Exception as exc:  # noqa: BLE001
        log_event("plugin:memory", "close_windows_for_topic_failed", topic=topic_id, error=str(exc))
        closed_windows = []
    text = f"Closed: {topic.get('description') or topic_id}."
    if closed_windows:
        text += f" Also closed {len(closed_windows)} window(s) open for it: {', '.join(closed_windows)}."
    return {"text": text}


async def request_info(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    topic, material = args.get("topic"), args.get("material")
    if material:
        if not topic:
            return {"text": 'To read a material in full give both "topic" and "material" (their ids from an earlier request_info reply).', "is_error": True}
        return await _read_material(topic, material, args.get("offset"), args.get("save_file_to"))
    # Per explicit instruction (2026-10-13): topic given ALONE (no material)
    # used to fall straight through to the query branch below and fail with
    # a usage error -- there was no fast "give me this track's current
    # state" path at all, even though the model already has the topic's id
    # from the system prompt's own topic list. See _read_topic.
    if topic:
        return await _read_topic(topic)
    if args.get("document"):
        return await _read_document(args["document"], args.get("section"), args.get("offset"), args.get("save_file_to"))
    query = (args.get("query") or "").strip()
    if not query:
        return {"text": 'request_info needs "query": what you want to recall.', "is_error": True}
    return await _run_memory_query(query, args.get("history"))


async def _run_memory_query(query: str, history: int | None = None) -> dict[str, Any]:
    """memory:requestStart + soft-timeout polling + late-background
    continuation -- exactly what request_info(query=...) always did,
    factored out (2026-10-13) so it's shared with the topic(=id) cold
    fallback (_fetch_and_cache_topic) and the background prefetch loop
    (start_topic_prefetch_loop). Warms the local full-topic cache from
    every topics-source part it sees (_cache_topics_from_parts) -- ordinary
    query= usage keeps the cache warm too, not just the dedicated paths."""
    params: dict[str, Any] = {"query": query, "traceId": uuid.uuid4().hex, "language": _language()}
    if history:
        params["history"] = int(history)
    started = await _memory("memory:requestStart", **params)
    if not started.get("jobId"):  # refused before it ran: no funds
        log_event("plugin:memory", "request_refused", trace_id=params["traceId"], status=started.get("status"), error=started.get("error"))
        reaction = await _react_to_status(started)
        failure = _failure_text(started, "The memory request") or f"The memory request did not start: {json.dumps(started)}"
        return {"text": " ".join(filter(None, [failure, reaction])), "is_error": True}
    read = _Read(started["jobId"], query)
    deadline = time.monotonic() + SOFT_READ_TIMEOUT_S
    try:
        while read.final is None and (left := deadline - time.monotonic()) > 0:
            await read.poll(min(left, POLL_WAIT_S))
    except Exception as exc:  # noqa: BLE001 -- what was already found is still worth giving
        log_event("plugin:memory", "request_poll_failed", error=str(exc))
        found = _merge_parts(read.parts)
        return {"text": _capped_text(f"The memory request failed while reading: {exc}" + (f"\nFound before that: {json.dumps({'results': found}, ensure_ascii=False)}" if found else "")),
                "is_error": True}
    await _refresh_topics_if_stale()
    _cache_topics_from_parts(read.parts)
    results = _merge_parts(read.parts)
    if read.final is None:
        _continue_in_background(read)
        log_event("plugin:memory", "request_soft_timeout", parts=len(read.parts))
        later = (f'Memory is still being searched. Anything more it finds will be given to you as soon as it is found, '
                 f'in a service message starting with "{LATE_FACTS_HEADER}". Do not ask request_info the same again; go on '
                 "with what you have.")
        if not results:
            return {"text": f'Nothing found yet for "{query}" within {int(SOFT_READ_TIMEOUT_S)} s. {later}'}
        return {"text": _capped_text(json.dumps({"status": "partial", "results": results}, ensure_ascii=False)) + "\n" + later}
    final = read.final
    log_event("plugin:memory", "request_finished", trace_id=params["traceId"], status=final.get("status"), error=final.get("error"))
    reaction = await _react_to_status(final)
    failure = _failure_text(final, "The memory request")
    if failure:
        return {"text": _capped_text(" ".join(filter(None, [failure, reaction])) + ("\n" + json.dumps({"results": results}, ensure_ascii=False) if results else "")),
                "is_error": True}
    if not results:
        return {"text": f'Memory holds nothing relevant to "{query}".'}
    await _expand_notes_candidates(results)
    # Bug fix (2026-10-16, full-plugin context-overflow audit): merges every "facts"/
    # "documents" part across possibly many sources/pages with no total-char budget --
    # same shape as the email-batch incident (many full results, one call).
    return {"text": _capped_text(json.dumps({"status": final.get("status"), "results": results}, ensure_ascii=False))}


def _capped_text(text: str) -> str:
    window, note = truncate(text)
    if note is None:
        return text
    return window + "\n\n[... truncated: unusually large reply; narrow the query ...]"


def _cache_topics_from_parts(parts: list[dict[str, Any]]) -> None:
    """Feeds every topics-source part into update_topic, warming the local
    full-topic cache (memory_topics.py) from ordinary request_info(query=...)
    usage, not just the dedicated fast-path/prefetch writers. Defensive about
    shape: a save_info reply's own topics-result carries one singular
    "topic" (confirmed live); a broader search matching several distinct
    tracks may plausibly carry a "topics" list instead -- handled, but not
    confirmed against a real multi-topic search reply this session."""
    for part in parts:
        if part.get("source") != SOURCE_TOPICS:
            continue
        output = part.get("output") or {}
        candidates = output.get("topics") if isinstance(output.get("topics"), list) else [output.get("topic")]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("id"):
                update_topic(WORKSPACE_DIR, candidate)


async def _read_topic(topic_id: str) -> dict[str, Any]:
    """request_info(topic=<id>) alone (no "material"): cache-FIRST, always --
    per explicit instruction, never race or wait on the server when
    something is already cached, however old. The reply carries cached_at/
    age_s so the model itself can judge staleness and fall back to
    query=... if it suspects something changed. Falls back to a real search
    only the first time nothing at all is cached yet for this id."""
    cached = cached_topic_detail(WORKSPACE_DIR, topic_id)
    if cached is not None:
        age_s = time.time() - float(cached.get("cached_at") or 0.0)
        log_event("plugin:memory", "topic_fast_path_hit", topic_id=topic_id, age_s=round(age_s))
        return {"text": json.dumps({
            "status": STATUS_OK,
            "results": [{"source": SOURCE_TOPICS, "output": {
                "topic": cached["topic"], "cached_at": cached.get("cached_at"), "age_s": round(age_s),
            }}],
        }, ensure_ascii=False)}
    log_event("plugin:memory", "topic_fast_path_miss", topic_id=topic_id)
    return await _fetch_and_cache_topic(topic_id)


async def _fetch_and_cache_topic(topic_id: str, description: str | None = None) -> dict[str, Any]:
    """The only way to get one topic's full detail cold: there is no server
    command narrower than the general memory:requestStart search (confirmed
    against docs/MICROAGENTS_PLAN.md sections 2.6/2.6.1 -- memory:topics
    only ever returns the slim tiered listing, nothing more targeted
    exists). Uses the topic's own cached short description as the query
    text. Shared by _read_topic's cold path and the background prefetch
    loop; both warm the cache through _run_memory_query the same way
    ordinary query= usage does."""
    if description is None:
        description = topic_description(WORKSPACE_DIR, topic_id)
    query = (description or "").strip()
    if not query:
        return {"text": f'Topic {topic_id} is not in local memory (no cached description to search with) -- '
                         "use request_info(query=...) instead.", "is_error": True}
    return await _run_memory_query(query)


async def _prefetch_tick() -> None:
    """One pass of the background prefetch loop -- see start_topic_prefetch_
    loop's own docstring. Only the "today" tier (recent_topics()'s first
    element); "week"-tier topics are deliberately skipped, per explicit
    instruction (already lower priority for the prompt, prefetching them
    would just add cost for little benefit)."""
    today, _week = recent_topics(WORKSPACE_DIR)
    now = time.time()
    for topic in today:
        topic_id = topic.get("id")
        if not topic_id:
            continue
        age = topic_detail_age_s(WORKSPACE_DIR, topic_id, now)
        if age is not None and age <= DETAIL_STALE_AFTER_S:
            continue  # already warm -- ordinary save/query usage keeps cached_at fresh
        try:
            reply = await _fetch_and_cache_topic(topic_id, topic.get("description"))
            log_event("plugin:memory", "topic_prefetched", topic_id=topic_id, ok=not reply.get("is_error"))
        except Exception as exc:  # noqa: BLE001 -- one bad topic must not stop the rest of the tick
            log_event("plugin:memory", "topic_prefetch_failed", topic_id=topic_id, error=str(exc))


def start_topic_prefetch_loop(interval_s: float = PREFETCH_INTERVAL_S) -> "asyncio.Task[None]":
    """Per explicit instruction (2026-10-13): proactively warms the local
    full-topic cache for active ("today"-tier) tracks before the model ever
    asks -- see PREFETCH_INTERVAL_S/DETAIL_STALE_AFTER_S's own comments for
    the interval/staleness tradeoffs. Process-wide, not routed through any
    ChatSession/tool-call machinery -- a plain data-layer sync loop, same
    shape as _refresh_topics_if_stale's own opportunistic calls."""

    async def _loop() -> None:
        while True:
            await asyncio.sleep(interval_s)  # let the slim "today" list populate before the first tick
            await _prefetch_tick()

    log_event("plugin:memory", "topic_prefetch_loop_starting", interval_s=interval_s)
    return supervise("memory_topic_prefetch", _loop)


class _Read:
    """One background memory request (memory:requestStart), followed by polling."""

    def __init__(self, job_id: str, query: str) -> None:
        self.job_id = job_id
        self.query = query
        self.parts: list[dict[str, Any]] = []
        self.cursor = 0
        self.final: dict[str, Any] | None = None

    async def poll(self, wait: float) -> list[dict[str, Any]]:
        """The parts that arrived since the last poll; sets `final` once the request is done."""
        reply = await _memory("memory:requestPoll", jobId=self.job_id, cursor=self.cursor, wait=round(max(0.0, wait), 1))
        new = list(reply.get("parts") or [])
        self.parts.extend(new)
        self.cursor = int(reply.get("cursor") or self.cursor + len(new))
        if reply.get("done"):
            self.final = reply
        return new


NOTES_SOURCE_PREFIX = "notes."
# How many further id/title-only candidates to auto-fetch in full, beyond the
# one the main search already gives full text -- see _expand_notes_candidates.
NOTES_AUTO_EXPAND_EXTRA = 1


async def _expand_notes_candidates(results: list[dict[str, Any]]) -> None:
    """Per explicit instruction (2026-10-14): NotesAdapter's own top-1-only
    ranking (see its own 2026-10-14 fix, reforce-microagents/apps/microagents/
    NotesAdapter.py) judges by title/folder alone and can genuinely misrank a
    vague query -- confirmed live against the real incident's own vague query,
    where the actually-right note came back SECOND, as id/title only, while a
    stale, superficially-similar-titled note won first place. Rather than
    leaning on the calling model to notice the top pick is wrong and re-ask
    (the whole reason this is unreliable in the first place: a cheap model
    does not reliably do that), this automatically fetches the next id-only
    candidate's own full text too -- one extra parallel round trip, not one
    per candidate, and only when a part genuinely had more than one candidate.
    Re-querying with that candidate's own exact title as the query reliably
    makes THAT note rank first for ITS OWN query (prompt_select's own
    specificity-wins rule), so no new server-side judgment agent is needed --
    this stays entirely client-side, reusing the ordinary search path.

    Mutates `results` in place, adding "text"/"truncated" to whichever
    candidate stubs get confirmed. A follow-up that errors, times out, or
    comes back without that same note's id leaves its original id/title-only
    stub untouched -- fail-open, same philosophy as every other check here."""
    pending: list[dict[str, Any]] = []
    for part in results:
        source = part.get("source") or ""
        if not source.startswith(NOTES_SOURCE_PREFIX):
            continue
        notes = (part.get("output") or {}).get("notes")
        if not isinstance(notes, list):
            continue
        for candidate in notes[1 : 1 + NOTES_AUTO_EXPAND_EXTRA]:
            if isinstance(candidate, dict) and "text" not in candidate and candidate.get("title"):
                pending.append(candidate)
    if not pending:
        return
    replies = await asyncio.gather(*(_run_memory_query(candidate["title"]) for candidate in pending), return_exceptions=True)
    for candidate, reply in zip(pending, replies):
        if isinstance(reply, BaseException) or not isinstance(reply, dict):
            continue
        try:
            payload = json.loads(reply.get("text") or "{}")
        except (ValueError, TypeError):
            continue
        for sub_part in payload.get("results") or []:
            for note in (sub_part.get("output") or {}).get("notes") or []:
                if isinstance(note, dict) and note.get("id") == candidate.get("id") and "text" in note:
                    candidate["text"] = note["text"]
                    candidate["truncated"] = note.get("truncated", False)
                    break


def _is_keep_only(part: dict[str, Any]) -> bool:
    """Fact memory's last portion: no facts, only its whole-set verdict."""
    output = part.get("output") or {}
    return "keep" in output and not output.get("facts")


def _merge_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The parts of an answer as the reply of a whole request: one entry per
    source, the facts (or the documents) of all its portions together.

    Fact memory ends a read with a portion that holds no facts, only "keep":
    the ids its sanitizer kept after seeing the whole set at once (each
    portion before it was sanitized one leaf at a time). When that verdict is
    here, only the facts it names are kept."""
    merged: dict[str, dict[str, Any]] = {}
    keep: dict[str, set[str]] = {}
    for part in parts:
        source, output = part.get("source"), dict(part.get("output") or {})
        if _is_keep_only(part):
            keep[source] = set(output["keep"])
            continue
        known = merged.get(source)
        listed = next((name for name in ("facts", "documents") if isinstance(output.get(name), list)), None)
        if known is not None and listed and isinstance(known.get(listed), list):
            known[listed] = known[listed] + output[listed]
            for flag in ("incomplete", "untranslated"):
                known[flag] = bool(known.get(flag)) or bool(output.get(flag))
        else:
            merged[source] = output
    for source, kept in keep.items():
        output = merged.get(source)
        if output is not None and isinstance(output.get("facts"), list):
            output["facts"] = [fact for fact in output["facts"] if fact.get("id") in kept]
    return [{"source": source, "output": output} for source, output in merged.items() if output.get("facts") != [] or "facts" not in output]


def _late_text(query: str, parts: list[dict[str, Any]]) -> str:
    return (
        f'{LATE_FACTS_HEADER} for your earlier request_info("{query}") -- found after its reply was given:\n'
        + json.dumps({"results": _merge_parts(parts)}, ensure_ascii=False)
        + "\nIf this changes or completes what you told the user, or what you are doing, act on it; if it adds "
        "nothing that matters now, reply with exactly [[NO_UPDATE]].]"
    )


def _continue_in_background(read: _Read) -> None:
    """Goes on polling a request whose soft timeout passed. Every part that
    arrives is handed to the model at once. Registered as an operation of
    the tab, so that Stop cancels it."""
    inject = get_inject_proactive()
    op = OPERATIONS.create(LATE_READ_TOOL, tab_id=get_tab_id())

    async def run() -> None:
        until = time.monotonic() + LATE_READ_LIMIT_S
        try:
            while read.final is None and time.monotonic() < until:
                new = await read.poll(POLL_WAIT_S)
                if new:
                    _cache_topics_from_parts(new)  # late-arriving topic facts warm the cache too
                # The whole-set verdict alone says nothing new once the reply is
                # given: the facts it would filter already reached the model.
                new = [part for part in new if not _is_keep_only(part)]
                if new and inject is not None:
                    inject(_late_text(read.query, new))
                    log_event("plugin:memory", "late_facts_injected", parts=len(new))
            if read.final is not None and read.final.get("status") == STATUS_NO_FUNDS_USER:
                await _react_to_status(read.final)
            log_event("plugin:memory", "late_read_finished", status=(read.final or {}).get("status"), parts=len(read.parts))
            op.status = "done"
        except asyncio.CancelledError:
            op.status = "cancelled"
            log_event("plugin:memory", "late_read_cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 -- the main answer was given already; a lost tail is only logged
            op.status = "error"
            log_event("plugin:memory", "late_read_failed", error=str(exc))
        finally:
            OPERATIONS.forget(op.id)

    op.task = asyncio.create_task(run())


async def correct_info(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    """memory:correctFact -- one stored fact gets the right wording in place
    (FactMemory.py, "Correcting": same id, the old wording kept in its
    history, its counter starting again at 1)."""
    refused = await _gate()
    if refused:
        return refused
    fact_id, text = (args.get("fact_id") or "").strip(), (args.get("text") or "").strip()
    if not fact_id or not text:
        return {"text": 'correct_info needs "fact_id" (the "id" of a fact from a request_info reply) and "text" (its right wording).', "is_error": True}
    reply = await _memory("memory:correctFact", fact=fact_id, text=text, traceId=uuid.uuid4().hex, language=_language())
    reaction = await _react_to_status(reply)
    failure = _failure_text(reply, "Correcting that fact")
    if failure:
        return {"text": " ".join(filter(None, [failure, reaction])), "is_error": True}
    output = reply.get("output") or {}
    log_event("plugin:memory", "fact_corrected", fact_id=fact_id)
    return {"text": f'Corrected. It was: "{output.get("previous")}". It now reads: "{output.get("text")}".'}


async def forget_info(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    """memory:forgetFact -- one stored fact removed from memory entirely."""
    refused = await _gate()
    if refused:
        return refused
    fact_id = (args.get("fact_id") or "").strip()
    if not fact_id:
        return {"text": 'forget_info needs "fact_id": the "id" of a fact from a request_info reply.', "is_error": True}
    reply = await _memory("memory:forgetFact", fact=fact_id, traceId=uuid.uuid4().hex)
    reaction = await _react_to_status(reply)
    failure = _failure_text(reply, "Forgetting that fact")
    if failure:
        return {"text": " ".join(filter(None, [failure, reaction])), "is_error": True}
    log_event("plugin:memory", "fact_forgotten", fact_id=fact_id)
    return {"text": f'Forgotten: "{(reply.get("output") or {}).get("text")}".'}


async def save_document(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    """Hands a whole file to document memory. Returns at once: the upload and
    everything after it run in the background, like save_info."""
    refused = await _gate()
    if refused:
        return refused
    path = Path(str(args.get("path") or "")).expanduser()
    if not path.is_file():
        return {"text": f"Nothing was saved: there is no file at {args.get('path')}.", "is_error": True}
    size = path.stat().st_size
    if size == 0:
        return {"text": f"Nothing was saved: {path} is empty.", "is_error": True}
    if size > MAX_FILE_BYTES:
        return {"text": f"Nothing was saved: {path} is {size} bytes, larger than the {MAX_FILE_BYTES} allowed.", "is_error": True}
    params: dict[str, Any] = {
        "name": path.name, "content": base64.b64encode(path.read_bytes()).decode("ascii"),
        "source": "file", "ref": str(path), "traceId": uuid.uuid4().hex, "language": _language(),
    }
    guessed, _encoding = mimetypes.guess_type(path.name)
    if guessed:
        params["mime"] = guessed
    note = (args.get("note") or "").strip()
    if note:
        params["note"] = note
    task = asyncio.create_task(_save_document_in_background(params, get_inject_proactive()))
    _background_saves.add(task)
    task.add_done_callback(_background_saves.discard)
    log_event("plugin:memory", "document_save_started", trace_id=params["traceId"], name=path.name, bytes=size)
    return {"text": f"{path.name} is being put into document memory in the background: its text is read (scans too), indexed, and "
                    "its facts go into your memory. This takes from a minute to much longer for a long document; carry on. "
                    "You will be told only if it could not be stored."}


async def _save_document_in_background(params: dict[str, Any], inject: Any) -> None:
    try:
        reply = await _memory("memory:saveDocument", **params)
    except Exception as exc:  # noqa: BLE001 -- reported to the model below
        reply = {"status": "error", "error": {"code": "exception", "message": str(exc)}}
    try:
        reaction = await _react_to_status(reply) if reply.get("status") in (STATUS_OK, STATUS_EMPTY, STATUS_NO_FUNDS_USER) else None
    except Exception as exc:  # noqa: BLE001
        reaction = None
        log_event("plugin:memory", "document_save_aftermath_failed", error=str(exc))
    document = ((reply.get("output") or {}).get("document")) or {}
    log_event("plugin:memory", "document_save_finished", trace_id=params["traceId"], status=reply.get("status"),
              document=document.get("id"), duplicate=bool((reply.get("output") or {}).get("duplicate")), error=reply.get("error"))
    failure = _failure_text(reply, "Putting the document into memory")
    if failure and inject is not None:
        inject(
            f"[Service message: the save_document you called earlier for {params['ref']} did not go through. "
            + " ".join(filter(None, [failure, reaction])) + " Tell the user only if it matters to them.]"
        )


async def _read_document(document: str, section: Any, offset: Any, save_file_to: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"document": str(document)}
    if section not in (None, ""):
        params["section"] = str(section)
    if offset:
        params["offset"] = int(offset)
    if save_file_to:
        params["file"] = True
    reply = await _memory("memory:document", **params)
    reaction = await _react_to_status(reply)
    failure = _failure_text(reply, "Reading the document")
    if failure:
        return {"text": " ".join(filter(None, [failure, reaction])), "is_error": True}
    output = dict(reply.get("output") or {})
    content = output.pop("content_b64", None)
    if save_file_to:
        target = Path(str(save_file_to)).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(content or ""))
        output["file"] = f"saved to {target}"
    return {"text": _capped_output_json(output)}


def _capped_output_json(output: dict[str, Any]) -> str:
    # Defensive cap (2026-10-16, full-plugin context-overflow audit): memory:document/
    # memory:material already page server-side (section/offset, reforce's own
    # MEMORY_DOCUMENTS_SECTION_PAGE_CHARS) -- this is a client-side backstop in case that
    # server-side cap is ever misconfigured or a different server is pointed at, same
    # "never trust the server alone" reasoning as every other fix in this pass.
    text = json.dumps(output, ensure_ascii=False)
    window, note = truncate(text)
    if note is None:
        return text
    return window + "\n\n[... truncated: unusually large reply; pass a smaller `offset`/narrower `section` ...]"


async def _read_material(topic: str, material: str, offset: Any, save_file_to: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"topic": topic, "material": material}
    if offset:
        params["offset"] = int(offset)
    if save_file_to:
        params["file"] = True
    reply = await _memory("memory:material", **params)
    reaction = await _react_to_status(reply)
    failure = _failure_text(reply, "Reading the material")
    if failure:
        return {"text": " ".join(filter(None, [failure, reaction])), "is_error": True}
    output = dict(reply.get("output") or {})
    content = output.pop("content_b64", None)
    if save_file_to:
        if content is None:
            output["file"] = "this material has no file, only its text"
        else:
            target = Path(str(save_file_to)).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(content))
            output["file"] = f"saved to {target}"
    return {"text": _capped_output_json(output)}


_USAGE_INSTRUCTIONS = (
    "save_info and request_info are your memory. You decide what goes in and you pass it yourself -- nothing is "
    "fetched for you: if a letter or a message should be remembered, read it with your usual tools first and "
    "pass what matters here.\n"
    "save_info(text, materials?): \"text\" is what to remember, in your own words, complete enough to make sense "
    "on its own later (full names, not pronouns; dates, not \"tomorrow\"). One call may mix kinds of things; "
    "memory sorts them itself: lasting facts about people, organizations and things; the course of a topic you "
    "are discussing or working on (what happened, what was decided, what is open); credentials. Save right when "
    "you learn something, not at the end of the conversation.\n"
    "What concerns your OWNER themselves -- who they are, their preferences, their personal details -- does NOT "
    "go through save_info and is not found by request_info: it has tools of its own, owner_profile_remember and "
    "owner_profile_recall. Choose them deliberately when, and only when, the thing is about your owner.\n"
    "\"materials\" (optional) are the letters, messages and documents of the topic, kept WHOLE alongside its "
    "summary -- pass them when the exact wording may be needed later. Each is an object: {\"kind\": \"message\", "
    "\"source\": \"email\"|\"sms\"|\"telegram\"|..., \"ref\": the message's own id, \"ts\": when it was sent, "
    "\"from\", \"to\": [..], \"subject\", \"text\": its full text} or {\"kind\": \"document\", \"name\", "
    "\"text\": the document's text as you read it, \"path\": the local file} -- with \"path\" the file itself "
    "is stored too. Always give \"source\" and \"ref\" when the thing has them: the same letter sent again is "
    "then kept once.\n"
    "save_info returns at once and saves in the background: carry on, you are told only if it fails.\n"
    "request_info(query): ask in plain words what you want to recall. The reply lists what each memory found, "
    "marked by \"source\" (facts, topics, documents, contacts -- the address book --, notes.vault, ...); combine them yourself. "
    "For notes.memory/notes.vault specifically: the best-matching note (first in its \"notes\" list) carries "
    "full \"text\", and when there was more than one plausible candidate the next one does too (fetched "
    "automatically, in case the first guess was wrong); any further ones beyond that give only \"id\"/\"title\"/"
    "\"folder\". If neither of those two is what you need, call request_info(query=...) again with a MORE "
    "SPECIFIC query -- name the exact account, service or subject -- so it ranks first instead. A search that takes long "
    f"answers with what was found in its first {int(SOFT_READ_TIMEOUT_S)} seconds (status \"partial\"); the rest "
    f"reaches you later in a service message starting with \"{LATE_FACTS_HEADER}\" -- use it then. A fact comes "
    "with \"people\": the gender of the people it names, where memory knows it -- use it to speak of them "
    "correctly (he/she, the endings your language needs); for a person not listed there the gender is not "
    "known, do not guess it from the name. A fact may be "
    "marked doubtful or denied when something saved later contradicted it -- say so rather than stating it as "
    "certain. A topic comes with its summary, results, open questions and a list of its materials without their "
    "text; add \"history\": N to also get its last N episodes. To read one material in full call "
    "request_info(topic=<topic id>, material=<material id>); long texts come in pages (\"next_offset\" -> pass "
    "it as \"offset\"); add \"save_file_to\": <path> to also get a document's file written there.\n"
    "request_info(topic=<topic id>) ALONE (no \"material\"): the current state of one track -- its summary, "
    "results, open questions, materials -- by the id you already have from your own topic list in the system "
    "prompt. Usually answers INSTANTLY from your local cache, no server wait: the reply carries \"cached_at\"/"
    "\"age_s\" so you can judge how fresh it is -- call request_info(query=...) instead if you suspect it has "
    "moved since. The very first time a topic has no local cache yet, this runs a normal search instead, same "
    "as query=.\n"
    "Call request_info BEFORE searching Notes, mail or old conversation for something you may already know, and "
    "before asking the user to repeat it.\n"
    "DOCUMENTS. save_document(path, note?) puts a whole FILE into your document memory: a contract, a resume, a "
    "report, a manual, a scan, a photo of a page. You do NOT read it first and you do not pass its text: the "
    "file itself is kept, its text is read for you (PDF, DOCX, HTML, FB2, plain text, source code; scanned "
    "pages and pictures are recognized), it is indexed part by part, and the facts it states go into your "
    "memory on their own. Use it for any document worth coming back to -- one the user gave you, one you "
    "downloaded or received for a task -- instead of retelling it in save_info. \"note\" is one line of your "
    "own on what it is and why it is kept. It returns at once; indexing a long document takes minutes and "
    "costs in proportion to its length. The same file saved twice is kept once.\n"
    "A request_info(query) reply may carry the source \"documents\": each found document with its title, "
    "synopsis and the parts that answer -- every part with its \"id\", a synopsis and an \"excerpt\" of the "
    "document's own text. To read a part in full call request_info(document=<document id>, section=<part "
    "id>); request_info(document=<id>) alone gives the card and the outline of all parts; add \"save_file_to\" "
    "to get the file itself written to a path. A fact may carry \"documents\": [{\"document\", \"section\"}] -- "
    "the place it was read from; open it the same way when the exact wording matters.\n"
    "CORRECTING AND FORGETTING. Every fact in a request_info reply carries its \"id\". correct_info(fact_id, "
    "text) gives that one fact its right wording, in place (the old wording stays in its history); "
    "forget_info(fact_id) removes it from memory entirely. Use them ONLY when the user explicitly tells you a "
    "remembered fact is wrong or asks you to forget something -- never on your own judgment. A fact that is "
    "merely out of date is better saved anew with save_info: memory marks the old one doubtful or denied by "
    "itself and keeps the history visible.\n"
    "The older memory tools -- recall_memory, working_memory_*, topic_upsert/topics_list/topic_close -- are "
    "deprecated and read-only: what they hold can still be read, nothing new is written "
    "through them. notes_* tools work as before for notes the user asks about by name.\n"
    "TRACK STATUS. Each track in your system prompt's topic list may be marked [CURRENT TRACK of this tab] "
    "(the one save_info last wrote to, from this tab) and/or [closed] (finished -- close_track marks this). "
    "Before ever telling the user you have no record of something, or that context was lost and you need "
    "them to repeat it: check this tab's current track if one is marked, check every track NOT marked "
    "[closed] (with request_info(topic=<id>) for its real content), and check your recent dialogue history "
    "-- in that order. Only say you have nothing once all three have genuinely come up empty.\n"
    "[closed] is a HARD STOP on redoing work, not just a label. A generic cue to revisit old ground -- "
    "\"look at my previous message\", a stale item in your recent dialogue history, your own initiative -- "
    "must NEVER make you silently redo, continue or re-research a [closed] track. If something you're about "
    "to do matches one, stop, tell the user in one short sentence that it's already done, and ask whether "
    "they want anything new on top of it. Only the user's own explicit request to return to that specific "
    "topic lifts this -- then acting on it (save_info) reopens the track automatically, same as always."
)


PLUGIN = Plugin(
    name="memory",
    usage_instructions=_USAGE_INSTRUCTIONS,
    tools=[
        PluginTool(
            "save_info",
            "Call this EVERY time you learn a new fact, right then: who someone is, a name, number, date, "
            "address, account, what was agreed or decided, what the user wants, what you did and its result. "
            "Remembers something for later: a fact, where a topic you are discussing or working on stands, a "
            "credential. NOT for facts about your owner themselves -- use owner_profile_remember for those. "
            "\"text\" is what to remember, in your own words; optional "
            "\"materials\" are the whole letters/messages/documents of the topic (see this tool's usage "
            "instructions for their shape). You decide what to save and pass it yourself -- nothing is fetched "
            "for you.",
            {"text": str, "materials": list | None}, save_info,
        ),
        PluginTool(
            "request_info",
            "Call this at the START of every turn that concerns anything specific, before answering, acting "
            "or searching elsewhere, and before ever saying you do not know or remember something. "
            "Recalls from your memory: facts, topics (summary, decisions, open questions, their letters and "
            "documents), credentials. Not the owner's profile -- that is owner_profile_recall. Give \"query\" in plain words; or \"topic\" "
            "ALONE (an id from your own topic list) for that track's current state -- usually instant, from local "
            "cache; or \"topic\" and \"material\" ids from an earlier reply to read one letter/document in full; "
            "or \"document\" (and \"section\") ids to read a kept document's outline or one part of it. Call this "
            "before searching Notes or mail for something you may already know.",
            {
                "query": str | None, "history": int | None, "topic": str | None, "material": str | None,
                "document": str | None, "section": str | None, "offset": int | None, "save_file_to": str | None,
            },
            request_info,
        ),
        PluginTool(
            "save_document",
            "Puts a whole document FILE into your document memory -- a contract, resume, report, manual, a scan "
            "or a photo of a page. Give its \"path\"; do not read it or retell it first: the file is kept, its "
            "text is read for you (scans are recognized), indexed, and its facts go into your memory. Optional "
            "\"note\": one line on what it is. Use it for every document worth coming back to. Found later with "
            "request_info; a part is read in full with request_info(document, section).",
            {"path": str, "note": str | None},
            save_document,
        ),
        PluginTool(
            "correct_info",
            "Corrects ONE remembered fact in place: \"fact_id\" is the \"id\" of a fact from a request_info reply, "
            "\"text\" its right wording. Only when the user explicitly says that remembered fact is wrong.",
            {"fact_id": str, "text": str},
            correct_info,
        ),
        PluginTool(
            "forget_info",
            "Removes ONE remembered fact from memory entirely: \"fact_id\" is the \"id\" of a fact from a "
            "request_info reply. Only when the user explicitly asks you to forget it.",
            {"fact_id": str},
            forget_info,
        ),
        PluginTool(
            "close_track",
            "Marks a track (topic) closed: it is done, nothing is currently open on it. \"topic\" is its id, "
            "from your own topic list or an earlier request_info/save_info reply. Call this whenever you "
            "finish a substantial task/topic, not just when the user says to. Once closed, the track is "
            "read-only: redoing or continuing its work on a generic cue (not the user specifically asking to "
            "return to it) is not allowed -- see TRACK STATUS above. Saving a new episode into a closed track "
            "(save_info) reopens it on its own -- there is no separate reopen tool, but that reopening must "
            "only happen because the USER asked to return to it, never on your own initiative. A closed "
            "track is still found and read by request_info like any other. Also closes any viewer/browser "
            "windows that were opened while this was the tab's current track.",
            {"topic": str},
            close_track,
        ),
    ],
)
