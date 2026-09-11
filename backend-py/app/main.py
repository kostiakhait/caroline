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
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.chat_session import ChatSession, STARTUP_GREETING_NUDGE_TEMPLATE, current_language_name, refresh_language_in_background
from app.cli_control import auth_logout as cli_auth_logout, auth_status as cli_auth_status, mcp_add as cli_mcp_add, mcp_list as cli_mcp_list, mcp_remove as cli_mcp_remove, spawn_auth_login as cli_spawn_auth_login
from app.durability import dehydrated_dir, load_tab_session_id, peek_pending_turn
from app.history import read_archived_entries, read_recent_history, read_recent_history_for_session
from app.login_api import clear_credentials, is_logged_in, logged_in_email, open_login_request, register_and_save_login, take_login_request, verify_and_save_login
from app.logging_setup import log_event
from app.persona import get_persona, get_persona_edit_state, get_persona_gender, reset_profile, save_custom_persona, save_profile_override, set_profile_key
from app.plugins.files_plugin import open_file_with_default_app
from app.plugins.notes_api import load_credentials
from app.plugins.office_editor import finish_office_edit_session
from app.plugins.ratatosk_api import find_or_create_dm, send_message
from app.plugins.ratatosk_own_account import ensure_own_ratatosk_account, get_own_v2_session, has_own_ratatosk_account, own_ratatosk_email
from app.plugins.companion_api import resume_companion_operations, set_mine as companion_set_mine, start_companion_inbox_loop
from app.plugins.scheduler_plugin import ensure_recurring_backup, start_due_check_loop
from app.plugins.sw_api import mint_v2_session
from app.plugins.viewer_plugin import take_viewer_request
from app.plugins.voice_api import clean_text_for_speech, synthesize_speech, transcribe_audio, voice_for_gender
from app.ratatosk_channel import get_ratatosk_channel_status, start_ratatosk_owner_channel, start_ratatosk_presence_heartbeat
from app.sms_account import get_sms_account_status, remove_sms_account, set_sms_account
from app.subscription_mode import create_topup_checkout_url, get_own_anthropic_api_key, get_sw_status, resolve_mode, set_own_anthropic_api_key
from app.visual_mode import is_visual_mode_enabled, resolve_visual_model, set_visual_mode_enabled
from app.workspace_dir import WORKSPACE_DIR

PORT = int(os.environ.get("CAROLINE_PORT", "8765"))
PRIMARY_TAB_ID = "1"
# Headless Ratatosk owner-DM channel (see app/ratatosk_channel.py) -- a tab
# with no WebView2/WS connection at all, created lazily (only once there's
# an actual message to inject, not eagerly at backend startup) so a session
# that never opted into the Ratatosk integration never pays a whole extra
# MCP-heavy query() session for nothing.
RATATOSK_TAB_ID = "ratatosk"

# Ported verbatim from server.ts's own BACKUP_NUDGE -- seeded (idempotently,
# via the "vault-backup-hourly" kind tag) unconditionally at module load,
# same as the TS original, so it doesn't depend on the event loop existing
# yet (pure sync file I/O).
BACKUP_NUDGE = (
    "Time for your periodic memory backup: if Notes is available, save your current persona/reminders/anything "
    "worth keeping into the \"Caroline:Vault\" folder now (see your system instructions). If Notes isn't "
    "available, do nothing. Either way, this is routine background maintenance -- reply with exactly "
    "[[NO_UPDATE]] afterward, not a normal reply, unless something actually went wrong that the user needs to "
    "know about."
)
ensure_recurring_backup(BACKUP_NUDGE)

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
_has_sent_visual_mode_config = False
_resumed_unfinished_turn_for_tab: set[str] = set()


def _on_reminder_due(reminder: dict[str, Any]) -> bool:
    """Ported from server.ts's startDueCheckLoop callback. Always the
    PRIMARY tab -- reminders are Caroline's own proactive behavior (memory
    backup, checking things), not something that should fire once per open
    tab. Background tasks wait for a quiet moment instead of interrupting a
    live back-and-forth; priority tasks (the default) fire regardless."""
    primary = primary_session()
    if reminder.get("priority") == "background" and primary is not None and primary.has_live_dialog():
        return False
    if primary is None:
        return False
    # Bug fix (2026-09-11), per explicit instruction: used to pass
    # is_backup_nudge as inject_proactive()'s old "silent" flag -- a
    # whole-session switch that (a) couldn't actually force quiet once a
    # real conversation had already made the session "not silent" (an
    # AND-latch only ever moves toward "not silent", never back) and (b)
    # had nothing to do with THIS specific reply's own content. Whether
    # this reply is worth showing is now the model's own per-reply call,
    # via the existing [[NO_UPDATE]] sentinel -- for the recurring backup
    # reminder specifically, that guidance now lives directly in
    # BACKUP_NUDGE's own text (the note this reminder carries), so it
    # doesn't need special-casing here.
    delivered = primary.inject_proactive(
        f"⏰ Reminder due (you scheduled this for {reminder.get('dueAtIso')}): {reminder.get('note')}\n\n"
        "Nobody prompted you for this -- it's a self-scheduled follow-up. Act on it now and tell "
        "the user proactively, don't wait for them to say anything first."
    )
    if delivered:
        log_event("engine", "reminder_delivered", reminder_id=reminder.get("id"))
    return delivered


def _inject_companion_message(tab_id: str, text: str) -> bool:
    """Callback for companion_api.start_companion_inbox_loop: inject a
    phone-originated message into the named tab's live session, exactly as
    if the user had typed it locally. Returns False (leave it in the phone
    inbox, retry next tick) if that tab has no live session right now."""
    session = sessions.get(tab_id)
    if session is None:
        return False
    return session.inject_proactive(
        f"[The user sent this from their phone via the Caroline companion app]: {text}"
    )


def _active_tab_ids() -> list[str]:
    """Callback for the same loop: every tab with a live session right
    now, so history sync (and the inbox drain) covers all of them, not
    just the primary one."""
    return list(sessions.keys())


def _companion_history_snapshot(tab_id: str) -> list[dict[str, Any]]:
    """Callback for the same loop: THIS tab's own recent visible
    transcript to mirror to the phone. Bug fix (2026-09-10): confirmed
    live -- this used to ignore tab_id and call read_recent_history()
    (whichever session file was most recently modified anywhere in the
    workspace), which could mislabel one tab's conversation as another's.
    Now resolves this exact tab's own session id first."""
    session_id = load_tab_session_id(WORKSPACE_DIR, tab_id)
    if not session_id:
        return []
    return read_recent_history_for_session(WORKSPACE_DIR, session_id)


@app.on_event("startup")
async def _start_ratatosk_background_loops() -> None:
    # Needs a running event loop (asyncio.create_task inside both) -- can't
    # be started from the synchronous __main__ block below, which is why
    # this lives as a FastAPI startup hook instead (uvicorn.run() only
    # actually creates/runs the loop once it's called).
    start_ratatosk_owner_channel(WORKSPACE_DIR, _inject_from_ratatosk_owner)
    start_ratatosk_presence_heartbeat(WORKSPACE_DIR)
    # Checked every 20s (plus once immediately, catching anything that came
    # due while the app was closed); a reminder is only marked fired once
    # it's actually been injected into a live session (see _on_reminder_due).
    start_due_check_loop(_on_reminder_due)
    # Android companion app: drain phone-originated messages from
    # tabs/<tabId>/inbox into live sessions, and mirror recent history back
    # out to tabs/<tabId>/history. No-op while not logged into SW.
    start_companion_inbox_loop(WORKSPACE_DIR, _active_tab_ids, _inject_companion_message, _companion_history_snapshot)
    # Resume any companion operation (SMS send, sms/contacts lookup) that
    # was still in flight when the backend last went down -- see
    # companion_api.py's own module docstring for the never-gives-up
    # protocol this is completing the restart-survival half of.
    await resume_companion_operations(WORKSPACE_DIR, _inject_companion_message)


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
        session.inject_proactive(text)

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
    if op == "tab_list_set":
        # WPF's MainWindow.xaml.cs's own SyncTabListToBackend() -- tab id +
        # display name are otherwise known ONLY there (a WebView2 connects
        # with just a bare tabId, never a name). Mirrors the CURRENT full
        # tab list into Camerlengo (companion_api's tabs_list) so the
        # Android companion app's UI has a real, never-hardcoded directory
        # to render its own tab bar from -- see the caroline-android-
        # companion plan. Best-effort: silently no-ops while not logged
        # into SquirrelWisdom, same as every other companion-app sync;
        # WPF doesn't block its own tab strip on this either way.
        tabs = parsed.get("tabs")
        if isinstance(tabs, list) and is_logged_in():
            try:
                await companion_set_mine("tabs_list", tabs)
            except Exception as exc:
                log_event("engine", "tab_list_sync_failed", error=str(exc))
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
                session.inject_proactive(
                    "[The user closed the SquirrelWisdom login form without logging in. If this doesn't need any "
                    "reaction from you right now, reply with exactly [[NO_UPDATE]].]"
                )
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
                    "will work from now on -- no need to log in again. If this doesn't need any reaction from you "
                    "right now, reply with exactly [[NO_UPDATE]].]"
                )
        else:
            # Reopen the same form with the error shown, bypassing the model
            # entirely -- this is a credential retry, not something Caroline
            # needs to decide anything about.
            if send is not None:
                import uuid as _uuid
                await send({"type": "open_login", "requestId": _uuid.uuid4().hex, "error": result.error})
        return {"type": "control_response", "op": op, "ok": True, "requestId": request_id}
    if op == "auth_status":
        try:
            r = await cli_auth_status(WORKSPACE_DIR)
        except Exception as exc:
            return {"type": "control_response", "op": op, "ok": False, "stderr": str(exc), "requestId": request_id}
        log_event("engine", "auth_status", ok=r["code"] == 0)
        return {"type": "control_response", "op": op, "ok": r["code"] == 0, "stdout": r["stdout"], "stderr": r["stderr"], "requestId": request_id}
    if op == "auth_login":
        log_event("engine", "auth_login_spawning")

        async def on_chunk(_stream: str, text: str) -> None:
            if send is not None:
                await send({"type": "control_stream", "op": op, "chunk": text})

        try:
            ok = await cli_spawn_auth_login(WORKSPACE_DIR, on_chunk)
        except Exception as exc:
            return {"type": "control_response", "op": op, "ok": False, "stderr": str(exc), "requestId": request_id}
        log_event("engine", "auth_login_closed", ok=ok)
        return {"type": "control_response", "op": op, "ok": ok, "requestId": request_id}
    if op == "auth_logout":
        try:
            r = await cli_auth_logout(WORKSPACE_DIR)
        except Exception as exc:
            return {"type": "control_response", "op": op, "ok": False, "stderr": str(exc), "requestId": request_id}
        log_event("engine", "auth_logout", ok=r["code"] == 0)
        return {"type": "control_response", "op": op, "ok": r["code"] == 0, "stdout": r["stdout"], "stderr": r["stderr"], "requestId": request_id}
    if op == "force_restart":
        log_event("engine", "force_restart_via_control", had_session=session is not None)
        if session is not None:
            session.force_restart()
        return {"type": "control_response", "op": op, "ok": True, "requestId": request_id}
    if op == "mode_get":
        mode = await resolve_mode(WORKSPACE_DIR, (session.tab_id if session is not None else PRIMARY_TAB_ID))
        stdout = json.dumps({"chatSource": mode.chat_source, "swLoggedIn": mode.sw_logged_in})
        log_event("engine", "mode_get", stdout=stdout)
        return {"type": "control_response", "op": op, "ok": True, "stdout": stdout, "requestId": request_id}
    if op == "sw_status":
        status = await get_sw_status()
        stdout = json.dumps({"loggedIn": status.logged_in, "email": status.email, "balancePia": status.balance_pia, "balanceError": status.balance_error})
        log_event("engine", "sw_status", stdout=stdout)
        return {"type": "control_response", "op": op, "ok": True, "stdout": stdout, "requestId": request_id}
    if op == "sw_logout":
        log_event("engine", "sw_logout")
        clear_credentials()
        return {"type": "control_response", "op": op, "ok": True, "requestId": request_id}
    if op == "own_anthropic_key_get":
        key = get_own_anthropic_api_key(WORKSPACE_DIR)
        log_event("engine", "own_anthropic_key_get", is_set=bool(key))
        return {"type": "control_response", "op": op, "ok": True, "stdout": json.dumps({"isSet": bool(key)}), "requestId": request_id}
    if op == "own_anthropic_key_set":
        log_event("engine", "own_anthropic_key_set", clearing=not parsed.get("anthropicApiKey"))
        set_own_anthropic_api_key(WORKSPACE_DIR, parsed.get("anthropicApiKey") or None)
        return {"type": "control_response", "op": op, "ok": True, "requestId": request_id}
    if op == "sms_account_get":
        status = await get_sms_account_status()
        log_event("engine", "sms_account_get", has_account=status["hasAccount"], error=status.get("error"))
        return {"type": "control_response", "op": op, "ok": True, "stdout": json.dumps(status), "requestId": request_id}
    if op == "sms_account_set":
        if not parsed.get("smtp2goApiKey"):
            return {"type": "control_response", "op": op, "ok": False, "stderr": "sms_account_set requires smtp2goApiKey", "requestId": request_id}
        result = await set_sms_account(parsed["smtp2goApiKey"], parsed.get("smtp2goSender") or None)
        log_event("engine", "sms_account_set", ok=result["ok"])
        return {"type": "control_response", "op": op, "ok": result["ok"], "stderr": result.get("error"), "requestId": request_id}
    if op == "sms_account_remove":
        result = await remove_sms_account()
        log_event("engine", "sms_account_remove", ok=result["ok"])
        return {"type": "control_response", "op": op, "ok": result["ok"], "stderr": result.get("error"), "requestId": request_id}
    if op == "open_payment_from_settings":
        log_event("engine", "open_payment_from_settings")
        try:
            checkout_url = await create_topup_checkout_url()
        except Exception as exc:
            log_event("engine", "open_payment_from_settings_failed", error=str(exc))
            return {"type": "control_response", "op": op, "ok": False, "stderr": str(exc), "requestId": request_id}
        if send is not None:
            import uuid as _uuid
            await send({"type": "open_payment", "requestId": _uuid.uuid4().hex, "checkoutUrl": checkout_url})
        return {"type": "control_response", "op": op, "ok": True, "requestId": request_id}
    if op == "mcp_list":
        r = await cli_mcp_list(WORKSPACE_DIR)
        return {"type": "control_response", "op": op, "ok": r["code"] == 0, "stdout": r["stdout"], "stderr": r["stderr"], "requestId": request_id}
    if op == "mcp_add":
        if not parsed.get("name") or not parsed.get("command"):
            return {"type": "control_response", "op": op, "ok": False, "stderr": "mcp_add requires name and command", "requestId": request_id}
        r = await cli_mcp_add(WORKSPACE_DIR, parsed["name"], parsed["command"], parsed.get("args") or [], "user")
        log_event("engine", "mcp_add", name=parsed["name"], ok=r["code"] == 0)
        return {"type": "control_response", "op": op, "ok": r["code"] == 0, "stdout": r["stdout"], "stderr": r["stderr"], "requestId": request_id}
    if op == "mcp_remove":
        if not parsed.get("name"):
            return {"type": "control_response", "op": op, "ok": False, "stderr": "mcp_remove requires name", "requestId": request_id}
        r = await cli_mcp_remove(WORKSPACE_DIR, parsed["name"])
        log_event("engine", "mcp_remove", name=parsed["name"], ok=r["code"] == 0)
        return {"type": "control_response", "op": op, "ok": r["code"] == 0, "stdout": r["stdout"], "stderr": r["stderr"], "requestId": request_id}
    if op == "persona_get":
        log_event("engine", "persona_get")
        p = get_persona(WORKSPACE_DIR)
        merged = {
            "profileKey": p.profile_key, "name": p.name, "gender": p.gender, "age": p.age, "bio": p.bio,
            "biography": p.biography, "photos": [{"file": ph.file, "caption": ph.caption} for ph in p.photos],
        }
        stdout = json.dumps({"merged": merged, "edit": get_persona_edit_state(WORKSPACE_DIR)})
        return {"type": "control_response", "op": op, "ok": True, "stdout": stdout, "requestId": request_id}
    if op == "persona_set":
        p = parsed.get("persona")
        if not p or not p.get("profileKey"):
            return {"type": "control_response", "op": op, "ok": False, "stderr": "persona_set requires a persona object with profileKey", "requestId": request_id}
        log_event("engine", "persona_set", profile_key=p["profileKey"], name=p.get("name"))
        set_profile_key(WORKSPACE_DIR, p["profileKey"])
        if p["profileKey"] == "custom":
            save_custom_persona(WORKSPACE_DIR, {
                "name": p.get("name") or "Caroline", "gender": p.get("gender") or "female",
                "age": p.get("age") or "middle-aged", "bio": p.get("bio") or "",
            })
        else:
            save_profile_override(WORKSPACE_DIR, p["profileKey"], {
                "name": p.get("name"), "gender": p.get("gender"), "age": p.get("age"),
                "bio": p.get("bio"), "biography": p.get("biography"), "photosDir": p.get("photosDir"),
            })
        return {"type": "control_response", "op": op, "ok": True, "requestId": request_id}
    if op == "persona_reset":
        profile_key = parsed.get("profileKey")
        if profile_key not in ("caroline", "peter"):
            return {"type": "control_response", "op": op, "ok": False, "stderr": "persona_reset requires profileKey 'caroline' or 'peter'", "requestId": request_id}
        log_event("engine", "persona_reset", profile_key=profile_key)
        reset_profile(WORKSPACE_DIR, profile_key)
        return {"type": "control_response", "op": op, "ok": True, "requestId": request_id}
    if op == "visual_mode_get":
        model = resolve_visual_model(WORKSPACE_DIR)
        stdout = json.dumps({
            "enabled": is_visual_mode_enabled(WORKSPACE_DIR),
            "available": model is not None,
            "source": model["source"] if model else None,
        })
        return {"type": "control_response", "op": op, "ok": True, "stdout": stdout, "requestId": request_id}
    if op == "visual_mode_set":
        if not isinstance(parsed.get("enabled"), bool):
            return {"type": "control_response", "op": op, "ok": False, "stderr": "visual_mode_set requires a boolean 'enabled'", "requestId": request_id}
        log_event("engine", "visual_mode_set", enabled=parsed["enabled"])
        set_visual_mode_enabled(WORKSPACE_DIR, parsed["enabled"])
        return {"type": "control_response", "op": op, "ok": True, "requestId": request_id}
    if op == "shutdown_sync":
        # Best-effort: the WPF shell calls this before killing the backend
        # process, which doesn't wait for a real answer -- this just gets
        # the memory-backup nudge queued as fast as possible. Deliberately
        # the PRIMARY tab specifically (not the calling `session`, if any)
        # -- the whole app is closing, not just one tab, and the backup
        # itself writes one shared "Caroline:Vault" note regardless of
        # which tab does it.
        log_event("engine", "shutdown_sync")
        p = primary_session()
        if p is not None:
            p.inject_proactive("The app is closing right now. " + BACKUP_NUDGE + " Do it immediately, as briefly as possible.")
        return {"type": "control_response", "op": op, "ok": True, "requestId": request_id}
    if op == "get_history":
        entries = read_recent_history(WORKSPACE_DIR)
        log_event("engine", "get_history", entries=len(entries))
        return {"type": "control_response", "op": op, "ok": True, "stdout": json.dumps(entries), "requestId": request_id}
    if op == "expand_dehydrated_ref":
        import os as _os
        if not parsed.get("filePath"):
            return {"type": "control_response", "op": op, "ok": False, "stderr": "expand_dehydrated_ref requires filePath", "requestId": request_id}
        allowed_dir = _os.path.realpath(str(dehydrated_dir(WORKSPACE_DIR)))
        requested_path = _os.path.realpath(str(parsed["filePath"]))
        if requested_path != allowed_dir and not requested_path.startswith(allowed_dir + _os.sep):
            log_event("engine", "expand_dehydrated_ref_rejected", path=requested_path)
            return {"type": "control_response", "op": op, "ok": False, "stderr": "Path is not inside workspace/dehydrated/", "requestId": request_id}
        try:
            ext = _os.path.splitext(requested_path)[1].lower()
            if ext == ".txt":
                entries = read_archived_entries(requested_path)
                log_event("engine", "expand_dehydrated_ref_text", path=requested_path, entries=len(entries))
                return {"type": "control_response", "op": op, "ok": True, "stdout": json.dumps({"kind": "text", "entries": entries}), "requestId": request_id}
            import base64 as _b64
            data = Path(requested_path).read_bytes()
            mime_by_ext = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf"}
            mime_type = mime_by_ext.get(ext, "application/octet-stream")
            log_event("engine", "expand_dehydrated_ref_media", path=requested_path, bytes=len(data), mime=mime_type)
            return {"type": "control_response", "op": op, "ok": True, "stdout": json.dumps({"kind": "media", "mimeType": mime_type, "dataBase64": _b64.b64encode(data).decode("ascii")}), "requestId": request_id}
        except Exception as exc:
            log_event("engine", "expand_dehydrated_ref_failed", path=requested_path, error=str(exc))
            return {"type": "control_response", "op": op, "ok": False, "stderr": str(exc), "requestId": request_id}
    if op == "find_attachment":
        want_name = parsed.get("attachmentName")
        want_ts = parsed.get("attachmentTs")
        if not want_name or not isinstance(want_ts, (int, float)):
            return {"type": "control_response", "op": op, "ok": False, "stderr": "find_attachment requires attachmentName and attachmentTs", "requestId": request_id}
        try:
            candidates: list[tuple[str, float]] = []
            for directory in (Path(WORKSPACE_DIR) / "uploads", dehydrated_dir(WORKSPACE_DIR)):
                if not directory.exists():
                    continue
                for f in directory.iterdir():
                    if not f.name.endswith(want_name):
                        continue
                    candidates.append((str(f), abs(f.stat().st_mtime * 1000 - want_ts)))
            candidates.sort(key=lambda c: c[1])
            best = next((c for c in candidates if c[1] <= 30 * 60_000), None)
            if best is None:
                log_event("engine", "find_attachment_no_match", name=want_name, ts=want_ts)
                return {"type": "control_response", "op": op, "ok": False, "stderr": "No matching attachment found on disk", "requestId": request_id}
            data = Path(best[0]).read_bytes()
            ext = Path(best[0]).suffix.lower()
            mime_by_ext = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf"}
            mime_type = mime_by_ext.get(ext, "application/octet-stream")
            import base64 as _b64
            log_event("engine", "find_attachment_matched", name=want_name, path=best[0], diff_ms=best[1], bytes=len(data))
            return {"type": "control_response", "op": op, "ok": True, "stdout": json.dumps({"mimeType": mime_type, "dataBase64": _b64.b64encode(data).decode("ascii")}), "requestId": request_id}
        except Exception as exc:
            log_event("engine", "find_attachment_failed", error=str(exc))
            return {"type": "control_response", "op": op, "ok": False, "stderr": str(exc), "requestId": request_id}
    if op == "open_file":
        path = parsed.get("path")
        if not path:
            return {"type": "control_response", "op": op, "ok": False, "stderr": "open_file requires path", "requestId": request_id}
        if not Path(path).exists():
            log_event("engine", "open_file_not_found", path=path)
            return {"type": "control_response", "op": op, "ok": False, "stderr": f"No such file: {path}", "requestId": request_id}
        open_file_with_default_app(path)
        log_event("engine", "open_file", path=path)
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

    # Bug fix (2026-09-11): send the SAME status message (READY/WORKING/
    # RECOVERING/ERROR) the client will keep getting from here on, via the
    # session's own single source of truth for that shape, instead of a
    # separate hand-rolled "caroline_status: connected" the client had to
    # reconcile against everything else.
    await session._publish_status()

    # Bug fix (2026-09-10): confirmed live -- this was never ported from
    # server.ts at all (flagged as a known gap in the migration plan and
    # never circled back to). Without it, VisualModeManager.Configure()
    # (Windows/Caroline/VisualModeManager.cs) never runs, the render model
    # never warms up, and EVERY Visual Mode attempt permanently falls back
    # to plain audio (confirmed live: "HandleAudioAsync -- model not
    # warmed (yet), signaling fallback." on 3/3 real TTS calls, even
    # though visual_mode_is_enabled/resolve_model both correctly reported
    # true). Sent once per backend-process lifetime, primary tab only --
    # same scope as the startup greeting below, matches server.ts exactly
    # (hasSentVisualModeConfig).
    global _has_sent_visual_mode_config
    if tab_id == PRIMARY_TAB_ID and not _has_sent_visual_mode_config:
        _has_sent_visual_mode_config = True
        vm_enabled = is_visual_mode_enabled(WORKSPACE_DIR)
        vm_model = resolve_visual_model(WORKSPACE_DIR)
        log_event("engine", "visual_mode_config_sent", enabled=vm_enabled, model_path=(vm_model or {}).get("modelPath"))
        await websocket.send_json({"type": "visual_mode_config", "enabled": vm_enabled, "modelPath": (vm_model or {}).get("modelPath")})

    # Per explicit instruction: Caroline must never come back up silently.
    # Fires once per backend-process lifetime, tied to the primary tab.
    if tab_id == PRIMARY_TAB_ID and not _has_greeted:
        _has_greeted = True

        from app.durability import load_tab_session_id
        recent_session_id = load_tab_session_id(WORKSPACE_DIR, PRIMARY_TAB_ID)
        lang = current_language_name(PRIMARY_TAB_ID)
        log_event("engine", "startup_greeting", lang=lang)
        session.inject_proactive(STARTUP_GREETING_NUDGE_TEMPLATE.format(language=lang))
        refresh_language_in_background(recent_session_id, PRIMARY_TAB_ID)

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
            # Bug fix (2026-09-10): confirmed live -- this is the real
            # question the user originally asked, just replayed after a
            # restart rather than arriving via a live submit(); setting it
            # here (inject_proactive() itself always passes
            # is_real_user=False, so it never would) lets
            # _check_progress_narration/_gather_recent_dialogue_for_narration
            # (chat_session.py) anchor periodic progress narration on it --
            # previously this stayed None all process lifetime and a
            # long-running resumed task (e.g. regenerating a presentation)
            # got no narration at all.
            session.last_real_user_question = unfinished_turn.text
            resume_lang = current_language_name(tab_id)
            session.inject_proactive(
                "[Caroline was restarted (app closed or crashed) while still working on this, and it was never "
                f'finished or answered:\n\n"{unfinished_turn.text}"\n\nResume it now and answer the user -- they '
                "don't know this happened yet, so tell them you got interrupted and pick up where you left off. "
                "Don't just re-run everything from scratch if you're not sure what already completed -- check "
                "first where that makes sense (e.g. was an email already sent, a file already written). Reply in "
                f"{resume_lang}.]",
            )

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            log_event("ws", "message_received", tab_id=tab_id, msg_type=msg_type)
            if msg_type == "user_message":
                session.submit(data.get("text", ""), data.get("attachments") or [], True, bool(data.get("voice")))
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
