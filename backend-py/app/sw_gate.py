"""Ports backend/src/swGate.ts -- the shared gate every SquirrelWisdom-
gated in-process tool should call before doing its real work. Per explicit
instruction: auto-open the native login/sign-up window on the FIRST
refusal since the last logout, then on any further refusal just say so
without reopening it -- only ensure_squirrelwisdom_login (explicit user
request) or Settings' "Log in" button open it after that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.logging_setup import log_event
from app.login_api import has_auto_prompted_sw_login, is_logged_in, mark_sw_auto_prompt_shown, open_login_request

SendFn = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class SwGatedFeature:
    id: str
    tool_names: list[str]
    label: str


# Single source of truth for which tools/features require the user's own
# SquirrelWisdom account -- documentation only (not runtime enforcement;
# gating logic differs per call site, e.g. Ratatosk only gates its "owner"
# identity, not "caroline"'s own separate account). Add an entry here
# whenever a new SW-gated tool is added, even if its own gating still has
# to be written by hand at the call site.
SW_GATED_FEATURES: list[SwGatedFeature] = [
    SwGatedFeature("consult", ["consult_large_model"], "Consulting a GPT-5-class model for wording advice"),
    SwGatedFeature(
        "ratatosk-owner",
        ["ratatosk_list_conversations", "ratatosk_get_messages", "ratatosk_send_message", "ratatosk_start_chat_with"],
        "Ratatosk messaging as the user's own account",
    ),
    SwGatedFeature("office-editor", ["open_in_viewer"], "Editing Office documents (docx/xlsx/pptx) via OnlyOffice"),
    SwGatedFeature("notes", ["notes_*"], "Notes"),
    SwGatedFeature("email", ["email_*"], "Email (read/send via a registered mailbox)"),
]


@dataclass
class SwGateResult:
    ok: bool
    message: str | None = None


async def require_sw_or_prompt(send: SendFn, no_ai_at_all: bool = False) -> SwGateResult:
    if is_logged_in():
        return SwGateResult(True)

    if not has_auto_prompted_sw_login():
        mark_sw_auto_prompt_shown()
        log_event("engine", "sw_gate_first_refusal_opening_window", no_ai_at_all=no_ai_at_all)
        await open_login_request(send, no_ai_at_all)
        return SwGateResult(
            False,
            "Not available: the user isn't logged into SquirrelWisdom. I've opened the native login/sign-up window "
            "for them -- tell them, and continue once they log in (you'll be nudged separately). Don't reopen the "
            "window yourself if this happens again -- only call ensure_squirrelwisdom_login if the user explicitly "
            "asks to log in.",
        )

    log_event("engine", "sw_gate_repeat_refusal_window_already_open", no_ai_at_all=no_ai_at_all)
    return SwGateResult(
        False,
        "Still not available: the user isn't logged into SquirrelWisdom (the login window was already opened once "
        "for this). Don't reopen it yourself -- only call ensure_squirrelwisdom_login if the user explicitly asks "
        "to log in, or point them to Settings -> Account & Billing.",
    )
