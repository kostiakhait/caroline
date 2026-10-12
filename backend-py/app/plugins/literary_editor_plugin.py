"""literary_editor -- edit_text: Caroline's own literary editor for a final
pass over text meant for human reading (an article, a post, a book) before
she shows or publishes it. Thin client over the server's literary:edit*
commands (apps/microagents/LiteraryEditor.py on reforce) -- see that
module's own docstring for what it actually does (patch-mode detect-then-fix,
chunk by chunk, with a terminology glossary carried across chunks).

Caroline decides herself when to call this -- per explicit instruction
(2026-10-12), there is no automatic trigger wired into any other tool or
policy; a piece a human will actually read is a deliberate enough moment
that she should notice it herself.

Soft-timeout, the same shape as memory_plugin.py's own request_info:
edit_text waits up to EDIT_SOFT_TIMEOUT_S for the job to finish; if it is
still running, the tool replies with that and the edit continues in the
background, its result delivered later as a service message. A short piece
usually finishes well inside the timeout; a whole book usually will not.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import httpx

from app.http_tls import ssl_context
from app.logging_setup import log_event
from app.operations import REGISTRY as OPERATIONS
from app.plugins.loader import Plugin, PluginTool
from app.plugins.notes_api import SessionManager
from app.plugins.sw_api import API_URL, CAROLINE_SW_KEY, SessionExpiredError, SwApiError
from app.session_context import get_inject_proactive, get_send, get_tab_id
from app.sw_gate import require_sw_or_prompt

_sessions = SessionManager()

EDIT_SOFT_TIMEOUT_S = 30.0
POLL_WAIT_S = 20.0
LATE_READ_LIMIT_S = 1800.0  # a whole book may run well past the soft timeout
LATE_EDIT_HEADER = "[Literary editor finished"


async def _post(body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(verify=ssl_context(), timeout=60.0) as client:
        response = await client.post(API_URL, json=body)
    if response.status_code >= 400:
        raise SwApiError(f'SquirrelWisdom API HTTP {response.status_code} for command "{body.get("command")}"')
    envelope = response.json()
    if envelope.get(".status") != "ok":
        reason = envelope.get(".reason", json.dumps(envelope))
        if isinstance(reason, str) and "session" in reason.lower():
            raise SessionExpiredError(reason)
        raise SwApiError(f'command "{body.get("command")}" failed: {reason}')
    return envelope


async def _literary(command: str, **params: Any) -> dict[str, Any]:
    return await _sessions.with_session(
        lambda session: _post({"command": command, "key": CAROLINE_SW_KEY, "session": session, **params})
    )


async def _gate() -> dict[str, Any] | None:
    gate = await require_sw_or_prompt(get_send())
    return None if gate.ok else {"text": gate.message, "is_error": True}


def _failure_text(reply: dict[str, Any]) -> str | None:
    status = reply.get("status")
    if status in ("ok", "empty"):
        return None
    if status == "no_funds_user":
        return "Editing was refused: the user's SquirrelWisdom balance is too low. Do not retry until they top up."
    if status == "no_funds_upstream":
        return "Editing could not run: the editor's own provider is out of funds right now. Retrying will not help."
    error = reply.get("error") or {}
    return f"Editing failed ({error.get('code') or status}): {error.get('message') or 'no details'}"


class _Poll:
    def __init__(self, poll_command: str, job_id: str) -> None:
        self.poll_command = poll_command
        self.job_id = job_id
        self.final: dict[str, Any] | None = None

    async def once(self, wait: float) -> None:
        reply = await _literary(self.poll_command, jobId=self.job_id, wait=round(max(0.0, wait), 1))
        if reply.get("done"):
            self.final = reply


def _continue_in_background(poll: _Poll, tool_name: str, trace_id: str) -> None:
    inject = get_inject_proactive()
    op = OPERATIONS.create(f"{tool_name} (continued)", tab_id=get_tab_id())

    async def run() -> None:
        until = time.monotonic() + LATE_READ_LIMIT_S
        try:
            while poll.final is None and time.monotonic() < until:
                await poll.once(POLL_WAIT_S)
            if poll.final is not None and inject is not None:
                failure = _failure_text(poll.final)
                if failure:
                    inject(f"[Service message: the {tool_name} you called earlier (trace {trace_id}) failed. {failure}]")
                else:
                    text = (poll.final.get("output") or {}).get("text") or ""
                    inject(
                        f"{LATE_EDIT_HEADER}, trace {trace_id}]\n{text}\n"
                        "If this is still relevant to what you were doing, use it now; otherwise ignore it."
                    )
            op.status = "done"
        except asyncio.CancelledError:
            op.status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 -- the soft-timeout reply was already given; a lost tail is only logged
            op.status = "error"
            log_event("plugin:literary-editor", "late_poll_failed", tool=tool_name, error=str(exc))
        finally:
            OPERATIONS.forget(op.id)

    op.task = asyncio.create_task(run())


async def _start_and_wait(tool_name: str, start_command: str, poll_command: str, params: dict[str, Any]) -> dict[str, Any]:
    """Shared by edit_text/write_text: starts the background job, waits up
    to EDIT_SOFT_TIMEOUT_S, and either returns the result or hands off to
    _continue_in_background and tells the model to expect a service message."""
    try:
        started = await _literary(start_command, **params)
    except Exception as exc:
        return {"text": f"{tool_name} could not start: {exc}", "is_error": True}
    job_id = started.get("jobId")
    if not job_id:
        failure = _failure_text(started) or f"{tool_name} did not start: {json.dumps(started)}"
        return {"text": failure, "is_error": True}
    poll = _Poll(poll_command, job_id)
    deadline = time.monotonic() + EDIT_SOFT_TIMEOUT_S
    try:
        while poll.final is None and (left := deadline - time.monotonic()) > 0:
            await poll.once(min(left, POLL_WAIT_S))
    except Exception as exc:  # noqa: BLE001 -- reported to the model below
        log_event("plugin:literary-editor", "poll_failed", tool=tool_name, error=str(exc))
        return {"text": f"{tool_name} failed while waiting: {exc}", "is_error": True}
    if poll.final is None:
        _continue_in_background(poll, tool_name, params["traceId"])
        return {"text": f'Still working after {int(EDIT_SOFT_TIMEOUT_S)}s (a long piece can take a while) -- the '
                         f'result will reach you as a service message starting with "{LATE_EDIT_HEADER}" once '
                         f"it's done. Do not call {tool_name} again for the same request; go on with what you "
                         "were doing."}
    failure = _failure_text(poll.final)
    if failure:
        return {"text": failure, "is_error": True}
    output = poll.final.get("output") or {}
    return {"text": output.get("text") or ""}


async def edit_text(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    text, chapters = args.get("text"), args.get("chapters")
    if bool(text) == bool(chapters):
        return {"text": 'edit_text needs exactly one of "text" or "chapters".', "is_error": True}
    params: dict[str, Any] = {"traceId": uuid.uuid4().hex}
    if text:
        params["text"] = text
    else:
        params["chapters"] = chapters
    return await _start_and_wait("edit_text", "literary:editStart", "literary:editPoll", params)


async def write_text(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    instruction = (args.get("instruction") or "").strip()
    if not instruction:
        return {"text": 'write_text needs "instruction": what to write and about what.', "is_error": True}
    params: dict[str, Any] = {"traceId": uuid.uuid4().hex, "instruction": instruction}
    return await _start_and_wait("write_text", "literary:writeStart", "literary:writePoll", params)


_USAGE_INSTRUCTIONS = (
    "Two tools, both for text meant for a human reader (an article, a social post, a chapter, a whole book) "
    "-- never for your own internal/system text, and not a grammar checker for the user's own writing unless "
    "asked.\n"
    "write_text(instruction) writes a piece FROM SCRATCH: plans it, then drafts and edits it section by "
    "section, and runs the whole thing through the same editorial pass edit_text does. Use it when nothing "
    "is written yet and you have been asked to produce the piece yourself.\n"
    "edit_text(text|chapters) is a final pass over a piece YOU ALREADY WROTE (in this conversation, or by "
    "write_text) before you show or publish it -- finds concrete issues (repetition, internal inconsistency, "
    "text that sounds like an AI wrote it, grammar, terminology that drifts across a long piece) and fixes "
    "them with surgical edits; it does not rewrite your voice or restructure what you wrote, only corrects "
    'what\'s actually flagged. Give exactly one of "text" (a flat string, split into sections automatically) '
    'or "chapters" (a list of {title, text} when you already know the natural split) -- use "chapters" '
    "whenever you have that structure, since it keeps the terminology/voice check working across the real "
    "boundaries instead of an arbitrary one.\n"
    "Call either yourself, deliberately -- there is no automatic trigger on anything else you do. A short "
    "piece usually returns the finished text directly; a long one may still be running after a short wait -- "
    "you'll be told, and the result reaches you later as a service message; do not call the same tool again "
    "for the same request while that is pending."
)


PLUGIN = Plugin(
    name="literary-editor",
    usage_instructions=_USAGE_INSTRUCTIONS,
    tools=[
        PluginTool(
            "edit_text",
            "Final literary-editing pass over a piece of text meant for human reading, before you show or "
            "publish it -- finds and surgically fixes repetition, inconsistency, AI-sounding phrasing, "
            'grammar, and cross-section terminology drift. Give exactly one of "text" or "chapters" (a list '
            "of {title, text}).",
            {"text": str | None, "chapters": list | None}, edit_text,
        ),
        PluginTool(
            "write_text",
            "Writes a piece of text meant for human reading (an article, a post, a book) from scratch, from "
            "an instruction -- plans it, drafts and edits it section by section, and returns the finished, "
            "already-edited text.",
            {"instruction": str}, write_text,
        ),
    ],
)
