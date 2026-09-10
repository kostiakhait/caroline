"""Caroline's Python backend -- FastAPI entry point. The WS + HTTP contract
from server.ts (Node), driving app/chat_session.py's ChatSession (which now
carries the full resilience/session-management layer -- hang detection,
restart budget, dehydration/compaction, the failure-classification
gauntlet, durability across app restarts). See the migration plan
(C:\\Users\\khait\\.claude\\plans\\foamy-sniffing-pixel.md) for what's still
outstanding (MCP-reconnect scheduling, Phase 4 cutover).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.chat_session import ChatSession, STARTUP_GREETING_NUDGE_TEMPLATE, current_language_name, refresh_language_in_background
from app.durability import peek_pending_turn
from app.login_api import is_logged_in, logged_in_email, open_login_request, register_and_save_login, take_login_request, verify_and_save_login
from app.logging_setup import log_event
from app.persona_gender import get_persona_gender
from app.plugins.notes_api import load_credentials
from app.plugins.office_editor import finish_office_edit_session
from app.plugins.ratatosk_api import find_or_create_dm, send_message
from app.plugins.ratatosk_own_account import ensure_own_ratatosk_account, get_own_v2_session, has_own_ratatosk_account, own_ratatosk_email
from app.plugins.sw_api import mint_v2_session
from app.plugins.viewer_plugin import take_viewer_request
from app.plugins.voice_api import clean_text_for_speech, synthesize_speech, transcribe_audio, voice_for_gender
from app.ratatosk_channel import get_ratatosk_channel_status, start_ratatosk_owner_channel, start_ratatosk_presence_heartbeat
from app.workspace_dir import WORKSPACE_DIR

PORT = int(os.environ.get("CAROLINE_PORT", "8765"))
PRIMARY_TAB_ID = "1"
# Headless Ratatosk owner-DM channel (see app/ratatosk_channel.py) -- a tab
# with no WebView2/WS connection at all, created lazily (only once there's
# an actual message to inject, not eagerly at backend startup) so a session
# that never opted into the Ratatosk integration never pays a whole extra
# MCP-heavy query() session for nothing.
RATATOSK_TAB_ID = "ratatosk"

app = FastAPI()
sessions: dict[str, ChatSession] = {}
_ratatosk_session: ChatSession | None = None
# Per explicit instruction: the owner-DM channel must never go silent. A
# turn triggered by an owner message can end (SDK "result") WITHOUT ever
# calling ratatosk_send_message (a hung tool call plus a rate limit can
# produce a synthetic reply that never reaches Ratatosk at all, leaving the
# owner with no idea anything happened). Tracked per-turn: reset right
# before injecting an owner message, set True the moment a
# ratatosk_send_message tool_use is observed, checked when that turn's
# "result" arrives -- if still False, send a fallback notice ourselves.
_ratatosk_turn_got_reply = False

# Guards the startup greeting to exactly once per backend-process lifetime
# -- a full app relaunch gets a fresh process (and so a fresh greeting),
# but reconnecting the SAME primary tab's WS (a WebView2 reload, say) must
# not re-greet every time. Also guards the crash-recovery pending-turn
# check per tab (not just the primary one) -- resolved lazily per tab
# since which tabs existed last run isn't known until each one's WS
# connection arrives and says its own tabId.
_has_greeted = False
_resumed_unfinished_turn_for_tab: set[str] = set()


@app.on_event("startup")
async def _start_ratatosk_background_loops() -> None:
    # Needs a running event loop (asyncio.create_task inside both) -- can't
    # be started from the synchronous __main__ block below, which is why
    # this lives as a FastAPI startup hook instead (uvicorn.run() only
    # actually creates/runs the loop once it's called).
    start_ratatosk_owner_channel(WORKSPACE_DIR, _inject_from_ratatosk_owner)
    start_ratatosk_presence_heartbeat(WORKSPACE_DIR)


def primary_session() -> ChatSession | None:
    return sessions.get(PRIMARY_TAB_ID)


@app.get("/api/status")
async def get_status() -> JSONResponse:
    primary = primary_session()
    body: dict[str, Any] = {
        "ok": True,
        "connected": primary is not None,
        **(primary.status() if primary else {}),
        "tabs": [s.status() for s in sessions.values()],
    }
    return JSONResponse(body)


class MessageBody(BaseModel):
    text: str
    attachments: list[dict[str, Any]] = []


@app.post("/api/message")
async def post_message(body: MessageBody, tab: str = PRIMARY_TAB_ID) -> JSONResponse:
    session = sessions.get(tab)
    if session is None:
        return JSONResponse(
            {"ok": False, "error": f'No active session for tab "{tab}" -- open that tab in the Caroline window at least once first.'},
            status_code=503,
        )
    session.submit(body.text, body.attachments)
    return JSONResponse({"ok": True}, status_code=202)


class ControlBody(BaseModel):
    op: str | None = None
    requestId: str | None = None

    class Config:
        extra = "allow"


@app.post("/api/control")
async def post_control(body: ControlBody, tab: str = PRIMARY_TAB_ID) -> JSONResponse:
    # Tab-agnostic HTTP path -- falls back to that tab's session (primary by
    # default) so ops needing injectProactive (login_submit) still work; any
    # OTHER event a handler pushes via `send` along the way is silently
    # dropped here (no live connection to deliver it to), matching
    # server.ts's own HTTP-path Promise that only observes control_response.
    async def _drop(_event: dict[str, Any]) -> None:
        return None

    result = await handle_control_request(body.model_dump(), _drop, sessions.get(tab))
    return JSONResponse(result)


async def _sw_session_or_none() -> str | None:
    """Best-effort v2 session for billing an ai:tts/ai:stt call against
    the user's SquirrelWisdom wallet -- None (not logged in, or the
    session mint itself failed) just means that particular call goes
    through unbilled/scope-gated-only."""
    creds = load_credentials()
    if not creds:
        return None
    try:
        return await mint_v2_session(creds["email"], creds["password"])
    except Exception:
        return None


async def _notify_owner_ratatosk_turn_had_no_reply() -> None:
    try:
        if not has_own_ratatosk_account(WORKSPACE_DIR) or not is_logged_in():
            return
        caroline_email = own_ratatosk_email(WORKSPACE_DIR)
        owner_email = logged_in_email()
        if not caroline_email or not owner_email:
            return
        session = await get_own_v2_session(WORKSPACE_DIR)
        group_id = await find_or_create_dm(session, caroline_email, owner_email)
        await send_message(
            session, group_id, caroline_email,
            "⚠️ Не смогла нормально ответить на предыдущее сообщение (сбой или лимит) -- напишите ещё раз, если "
            "это всё ещё актуально.",
        )
        log_event("engine", "ratatosk_no_reply_notice_sent")
    except Exception as exc:
        log_event("engine", "notify_owner_ratatosk_turn_had_no_reply_failed", error=str(exc))


async def _ratatosk_session_send(event: dict[str, Any]) -> None:
    """`send` for the headless ratatosk session -- there's no chat window to
    render this conversation in (deliberately headless, not a 6th tab), so
    this just logs, plus watches for whether ratatosk_send_message actually
    got called this turn."""
    global _ratatosk_turn_got_reply
    log_event("engine", "ratatosk_session_event", event_type=event.get("type"))
    if event.get("type") != "sdk_message":
        return
    message = event.get("message") or {}
    if message.get("type") == "assistant":
        for block in (message.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use" and block.get("name") == "mcp__caroline-ratatosk__ratatosk_send_message":
                _ratatosk_turn_got_reply = True
    elif message.get("type") == "result":
        if not _ratatosk_turn_got_reply:
            asyncio.create_task(_notify_owner_ratatosk_turn_had_no_reply())


async def get_or_create_ratatosk_session() -> ChatSession:
    global _ratatosk_session
    if _ratatosk_session is not None:
        return _ratatosk_session
    session = ChatSession(tab_id=RATATOSK_TAB_ID, workspace_dir=WORKSPACE_DIR, send=_ratatosk_session_send)
    sessions[RATATOSK_TAB_ID] = session
    await session.start()
    _ratatosk_session = session
    return session


def _inject_from_ratatosk_owner(text: str) -> None:
    global _ratatosk_turn_got_reply
    _ratatosk_turn_got_reply = False

    async def _do() -> None:
        session = await get_or_create_ratatosk_session()
        session.inject_proactive(text, False)

    asyncio.create_task(_do())


async def handle_control_request(
    parsed: dict[str, Any],
    send: Any = None,
    session: ChatSession | None = None,
) -> dict[str, Any]:
    """Mirrors server.ts's handleControlRequest -- wires client_diag,
    stt/tts, open_login_from_settings, login_submit so far, plus a default
    not-yet-implemented response for everything else; the full ~30-op
    switch is ongoing Phase 3 work, ported alongside each dependency as
    it's built.

    `send` pushes an event to the SAME connection this request arrived on
    (e.g. login_submit's error path re-opens the login form) -- None over
    the tab-agnostic HTTP /api/control path (see post_control), same as
    the original (server.ts:2585-2591): that path only ever observes the
    final control_response, any other push along the way is silently
    dropped, since there's no live connection to deliver it to anyway.

    `session` is the specific tab's ChatSession, for ops that need
    injectProactive (login_submit's success/cancel nudge) -- undefined for
    the tab-agnostic HTTP path too, which falls back to the primary tab at
    the call site (post_control), matching server.ts's own fallback.
    """
    op = parsed.get("op")
    request_id = parsed.get("requestId")
    if op == "client_diag":
        log_event("engine", "client_diag", **{k: v for k, v in parsed.items() if k not in ("op", "requestId")})
        return {"type": "control_response", "op": op, "ok": True, "requestId": request_id}
    if op == "stt":
        audio_b64 = parsed.get("audioBase64")
        fmt = parsed.get("format")
        if not audio_b64 or not fmt:
            return {"type": "control_response", "op": op, "ok": False, "stderr": "stt requires audioBase64 and format", "requestId": request_id}
        log_event("engine", "stt_requested", request_id=request_id, format=fmt, audio_bytes=len(audio_b64))
        try:
            text = await transcribe_audio(audio_b64, fmt, await _sw_session_or_none())
        except Exception as exc:
            log_event("engine", "stt_failed", request_id=request_id, error=str(exc))
            return {"type": "control_response", "op": op, "ok": False, "stderr": str(exc), "requestId": request_id}
        log_event("engine", "stt_done", request_id=request_id, text_len=len(text))
        return {"type": "control_response", "op": op, "ok": True, "stdout": text, "requestId": request_id}
    if op == "tts":
        text = parsed.get("text")
        if not text:
            return {"type": "control_response", "op": op, "ok": False, "stderr": "tts requires text", "requestId": request_id}
        log_event("engine", "tts_requested", request_id=request_id, text_len=len(text))
        try:
            sw_session = await _sw_session_or_none()
            cleaned_text = await clean_text_for_speech(text, sw_session)
            gender = get_persona_gender(WORKSPACE_DIR)
            audio_b64 = await synthesize_speech(cleaned_text, voice_for_gender(gender), sw_session)
        except Exception as exc:
            log_event("engine", "tts_failed", request_id=request_id, error=str(exc))
            return {"type": "control_response", "op": op, "ok": False, "stderr": str(exc), "requestId": request_id}
        log_event("engine", "tts_done", request_id=request_id, audio_bytes=len(audio_b64))
        return {"type": "control_response", "op": op, "ok": True, "stdout": audio_b64, "requestId": request_id}
    if op == "editor_result":
        outcome, path = parsed.get("outcome"), parsed.get("path")
        if not request_id or not outcome or not path:
            return {"type": "control_response", "op": op, "ok": False, "stderr": "editor_result requires requestId, outcome, and path", "requestId": request_id}
        log_event("engine", "editor_result", request_id=request_id, outcome=outcome, path=path)
        req = take_viewer_request(request_id)
        if req and req.get("remotePath") and outcome != "error":
            # A document opened via the OnlyOffice flow -- pull back whatever
            # got saved server-side and clean up the temp copy, before telling
            # Caroline anything. Best-effort: a sync failure here shouldn't
            # also swallow the close notification.
            try:
                await finish_office_edit_session(req["remotePath"], path)
            except Exception as exc:
                log_event("engine", "editor_result_sync_failed", path=path, error=str(exc))
        outcome_text = {
            "saved": f"saved ({path})",
            "cancelled": f"cancelled -- no changes saved ({path})",
            "closed": f"closed (view-only, {path})",
        }.get(outcome, f"failed to open -- {parsed.get('message') or 'unknown error'} ({path})")
        if session is not None:
            session.inject_proactive(
                f"The viewer window you opened for {path} just closed: {outcome_text}. Nobody prompted you for "
                "this -- react to it now if it's relevant (e.g. continue whatever the user asked you to do with "
                "this file once it was edited).",
            )
        return {"type": "control_response", "op": op, "ok": True, "requestId": request_id}
    if op == "ratatosk_status_get":
        owner_email = logged_in_email() if is_logged_in() else None
        caroline_email = own_ratatosk_email(WORKSPACE_DIR) if has_own_ratatosk_account(WORKSPACE_DIR) else None
        log_event("engine", "ratatosk_status_get", owner_email=owner_email, caroline_email=caroline_email)
        return {"type": "control_response", "op": op, "ok": True, "stdout": json.dumps({"ownerEmail": owner_email, "carolineEmail": caroline_email}), "requestId": request_id}
    if op == "ratatosk_channel_status":
        # Diagnostic-only: the headless owner-DM poll loop's own internal
        # state (tick count, cached groupId, cursor, last error) -- lets a
        # human (or a script over /api/control) see what it's actually
        # doing without needing to grep caroline.log by hand.
        status = get_ratatosk_channel_status()
        return {"type": "control_response", "op": op, "ok": True, "stdout": json.dumps(status), "requestId": request_id}
    if op == "ratatosk_own_account_register":
        log_event("engine", "ratatosk_own_account_register")
        result = await ensure_own_ratatosk_account(WORKSPACE_DIR)
        log_event("engine", "ratatosk_own_account_register_result", ok=result.get("ok"), email=result.get("email"), error=result.get("error"))
        return {
            "type": "control_response", "op": op, "ok": bool(result.get("ok")),
            "stdout": json.dumps({"email": result.get("email")}) if result.get("ok") else None,
            "stderr": None if result.get("ok") else result.get("error"),
            "requestId": request_id,
        }
    if op == "open_login_from_settings":
        # Same native login form ensure_squirrelwisdom_login opens (also
        # covers "Register"), triggered directly from Settings' button
        # instead of a chat tool call.
        log_event("engine", "open_login_from_settings")
        if send is not None:
            await open_login_request(send)
        return {"type": "control_response", "op": op, "ok": True, "requestId": request_id}
    if op == "login_submit":
        if not request_id:
            return {"type": "control_response", "op": op, "ok": False, "stderr": "login_submit requires requestId", "requestId": request_id}
        cancelled = bool(parsed.get("cancelled"))
        is_register = bool(parsed.get("isRegister"))
        log_event("engine", "login_submit", request_id=request_id, cancelled=cancelled, is_register=is_register)
        take_login_request(request_id)  # just clears the tracking entry
        if cancelled:
            if session is not None:
                session.inject_proactive("[The user closed the SquirrelWisdom login form without logging in.]", True)
            return {"type": "control_response", "op": op, "ok": True, "requestId": request_id}
        email, password = parsed.get("email"), parsed.get("password")
        if not email or not password:
            return {"type": "control_response", "op": op, "ok": False, "stderr": "login_submit requires email and password unless cancelled", "requestId": request_id}
        result = await (register_and_save_login(email, password) if is_register else verify_and_save_login(email, password))
        log_event("engine", "login_submit_result", is_register=is_register, email=email, ok=result.ok)
        if result.ok:
            if session is not None:
                kind = "registration" if is_register else "login"
                session.inject_proactive(
                    f"[SquirrelWisdom {kind} succeeded for {email}. Notes and other SquirrelWisdom-backed tools "
                    "will work from now on -- no need to log in again.]",
                    True,
                )
        else:
            # Reopen the same form with the error shown, bypassing the model
            # entirely -- this is a credential retry, not something Caroline
            # needs to decide anything about.
            if send is not None:
                import uuid as _uuid
                await send({"type": "open_login", "requestId": _uuid.uuid4().hex, "error": result.error})
        return {"type": "control_response", "op": op, "ok": True, "requestId": request_id}
    log_event("engine", "control_op_not_implemented", op=op)
    return {"type": "control_response", "op": op, "ok": False, "stderr": f"op not yet implemented in the Python backend: {op}", "requestId": request_id}


async def _handle_control_request_and_reply(websocket: WebSocket, data: dict[str, Any], session: ChatSession) -> None:
    """Runs handle_control_request in its own task (see the ws endpoint's
    control_request branch) and sends the reply once it resolves -- a
    disconnect/send failure mid-flight is logged and swallowed rather than
    propagated, since there's no request left to reply to. `send` here is
    the LIVE connection's own send (not a throwaway like post_control's),
    so a push made along the way (open_login, say) actually reaches the
    client, and `session` is this exact tab's own ChatSession."""
    try:
        response = await handle_control_request(data, websocket.send_json, session)
        await websocket.send_json(response)
    except Exception as exc:
        log_event("engine", "control_request_task_failed", op=data.get("op"), error=str(exc))


@app.websocket("/")
async def ws_endpoint(websocket: WebSocket) -> None:
    global _has_greeted
    # Each tab's WebView2 opens its own WS connection to ws://127.0.0.1:PORT/?tab=<id>
    # -- an older, tab-unaware client (or a manual/dev connection) that omits
    # ?tab= lands on the primary tab, same as every single-session client
    # before multi-tab existed.
    tab_id = websocket.query_params.get("tab", PRIMARY_TAB_ID)
    await websocket.accept()
    log_event("ws", "connected", tab_id=tab_id)

    # A FRESH ChatSession every connection (not reused across reconnects) --
    # matches server.ts's own design exactly: continuity across a
    # disconnect/reconnect (or a full app restart) comes from the
    # FILE-BASED durability layer (tab-session-<id>.json, pending-turn-
    # <id>.json), not from in-memory state surviving on some stale object.
    # The previous connection's session (if any) is disposed on its own
    # "close" below.
    session = ChatSession(tab_id=tab_id, workspace_dir=WORKSPACE_DIR, send=websocket.send_json)
    sessions[tab_id] = session
    await session.start()

    await websocket.send_json({"type": "caroline_status", "status": "connected"})

    # Per explicit instruction: Caroline must never come back up silently.
    # Fires once per backend-process lifetime, tied to the primary tab.
    if tab_id == PRIMARY_TAB_ID and not _has_greeted:
        _has_greeted = True

        from app.durability import load_tab_session_id
        recent_session_id = load_tab_session_id(WORKSPACE_DIR, PRIMARY_TAB_ID)
        lang = current_language_name()
        log_event("engine", "startup_greeting", lang=lang)
        session.inject_proactive(STARTUP_GREETING_NUDGE_TEMPLATE.format(language=lang), False)
        refresh_language_in_background(recent_session_id)

    # Survives a FULL app restart (not just this backend's own in-process
    # watchdog restart, which ChatSession's own failure handling already
    # replays from memory): if the whole process got closed/crashed while
    # a turn was still in flight, the user's message may already be
    # sitting in the conversation with no reply.
    if tab_id not in _resumed_unfinished_turn_for_tab:
        _resumed_unfinished_turn_for_tab.add(tab_id)
        unfinished_turn = peek_pending_turn(WORKSPACE_DIR, tab_id)
        if unfinished_turn:
            log_event("engine", "resuming_unfinished_turn", tab_id=tab_id, text_len=len(unfinished_turn.text))
            session.inject_proactive(
                "[Caroline was restarted (app closed or crashed) while still working on this, and it was never "
                f'finished or answered:\n\n"{unfinished_turn.text}"\n\nResume it now and answer the user -- they '
                "don't know this happened yet, so tell them you got interrupted and pick up where you left off. "
                "Don't just re-run everything from scratch if you're not sure what already completed -- check "
                "first where that makes sense (e.g. was an email already sent, a file already written).]",
            )

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            log_event("ws", "message_received", tab_id=tab_id, msg_type=msg_type)
            if msg_type == "user_message":
                session.submit(data.get("text", ""), data.get("attachments") or [], True, False, bool(data.get("voice")))
            elif msg_type == "interrupt":
                session.stop()
            elif msg_type == "control_request":
                # Un-awaited on purpose: tts/stt (and future long-running
                # ops) can take many seconds -- awaiting here would block
                # this same loop from processing any OTHER incoming WS
                # message (e.g. a user_message sent while tts is still
                # synthesizing) until it finishes. Concurrent requestIds
                # can finish out of order; the client matches replies by
                # id, not by assuming FIFO order (mirrors server.ts's own
                # `void handleControlRequest(...)`).
                asyncio.create_task(_handle_control_request_and_reply(websocket, data, session))
    except WebSocketDisconnect:
        log_event("ws", "disconnected", tab_id=tab_id)
    finally:
        session.dispose()
        if sessions.get(tab_id) is session:
            del sessions[tab_id]


if __name__ == "__main__":
    import uvicorn

    from app.local_tts_launcher import launch_local_tts_server
    from app.skills_seed import seed_skills

    log_event("engine", "starting", port=PORT, workspace_dir=WORKSPACE_DIR)
    seed_skills(WORKSPACE_DIR)
    launch_local_tts_server()
    uvicorn.run(app, host="127.0.0.1", port=PORT)
