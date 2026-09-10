"""ratatosk -- ports backend/src/ratatoskTools.ts. MCP tools for Ratatosk
(SquirrelWisdom's messenger). Every tool takes `as: "owner" | "caroline"`
so the SAME tool set drives both identities: "owner" acts as the user's
own SquirrelWisdom session (their messages, their contacts -- broad
standing authorization, no per-message confirmation needed, per the
user's own explicit sign-off), "caroline" acts as Caroline's own separate
Ratatosk account (ratatosk_own_account.py), if one has been registered.

The "owner" identity's SW-login gate goes through sw_gate.py's shared
require_sw_or_prompt -- same auto-popping native login window every other
SW-gated tool uses (opens once per logout, stays quiet on repeat refusals
until the user explicitly logs in or logs out again).

NOT yet ported: ratatoskChannel.ts's background poll loop (watches the
owner's DMs to Caroline for new incoming messages, publishes Caroline's
own presence heartbeat every 5s) -- genuine engine-level proactive
infrastructure, Phase 3 scope, same bucket as scheduler's due-check loop.
send_presence_heartbeat (ratatosk_api.py) is ready for that loop to call
once it exists.
"""

from __future__ import annotations

import json
from typing import Any

from app.plugins.loader import Plugin, PluginTool
from app.plugins.notes_api import load_credentials
from app.plugins.ratatosk_api import find_or_create_dm, get_recent_messages, list_conversations, send_message
from app.plugins.ratatosk_own_account import (
    ensure_own_ratatosk_account,
    get_own_v2_session,
    has_own_ratatosk_account,
    own_ratatosk_email,
)
from app.plugins.sw_api import mint_v2_session
from app.session_context import get_send
from app.sw_gate import require_sw_or_prompt
from app.workspace_dir import WORKSPACE_DIR


async def _resolve_session(as_: str) -> tuple[str, str]:
    if as_ == "owner":
        gate = await require_sw_or_prompt(get_send())
        if not gate.ok:
            raise RuntimeError(gate.message)
        creds = load_credentials()
        assert creds is not None  # gate.ok guarantees this
        session = await mint_v2_session(creds["email"], creds["password"])
        return session, creds["email"]
    if not has_own_ratatosk_account(WORKSPACE_DIR):
        raise RuntimeError("Caroline has no Ratatosk account yet -- use ensure_ratatosk_own_account first.")
    session = await get_own_v2_session(WORKSPACE_DIR)
    email = own_ratatosk_email(WORKSPACE_DIR)
    assert email is not None
    return session, email


async def ratatosk_identity_status(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    owner_creds = load_credentials()
    caroline_email = own_ratatosk_email(WORKSPACE_DIR) if has_own_ratatosk_account(WORKSPACE_DIR) else None
    payload = {
        "owner": {"loggedIn": True, "email": owner_creds["email"]} if owner_creds else {"loggedIn": False},
        "caroline": {"registered": True, "email": caroline_email} if caroline_email else {"registered": False},
    }
    return {"text": json.dumps(payload, ensure_ascii=False)}


async def ensure_ratatosk_own_account(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    result = await ensure_own_ratatosk_account(WORKSPACE_DIR)
    if not result["ok"]:
        return {"text": f"Failed: {result['error']}", "is_error": True}
    return {"text": f"Caroline's Ratatosk account: {result['email']}"}


async def ratatosk_list_conversations(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    session, sender_email = await _resolve_session(args["as"])
    conversations = await list_conversations(session, sender_email)
    return {"text": json.dumps(conversations, ensure_ascii=False)}


async def ratatosk_get_messages(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    session, _sender_email = await _resolve_session(args["as"])
    messages = await get_recent_messages(session, args["groupId"], args.get("days") or 2)
    return {"text": json.dumps(messages, ensure_ascii=False)}


async def ratatosk_send_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    session, sender_email = await _resolve_session(args["as"])
    await send_message(session, args["groupId"], sender_email, args["text"])
    return {"text": "Sent."}


async def ratatosk_start_chat_with(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    session, sender_email = await _resolve_session(args["as"])
    group_id = await find_or_create_dm(session, sender_email, args["email"])
    return {"text": group_id}


_AS_DESC = (
    '"owner" acts as the user\'s own account (their messages, their contacts). "caroline" acts as your own '
    "separate Ratatosk account, if you have one (see ensure_ratatosk_own_account)."
)


PLUGIN = Plugin(
    name="ratatosk",
    tools=[
        PluginTool(
            "ratatosk_identity_status",
            "Reports both Ratatosk identities: whether the user is logged into their own SquirrelWisdom "
            "account and what its email is, and whether you (Caroline) have your own separate Ratatosk "
            "account and what ITS email is. Call this before acting on Ratatosk if you're not sure which "
            "identity applies, or to avoid confusing who sent/received a given message.",
            {}, ratatosk_identity_status,
        ),
        PluginTool(
            "ensure_ratatosk_own_account",
            "Registers Caroline's own, separate Ratatosk account if one doesn't already exist (a real "
            "mailbox on navlink.net plus a SquirrelWisdom account, both generated automatically -- no user "
            'input needed). Idempotent: if you already have one, just reports it. Needed before using any '
            'ratatosk_* tool with as:"caroline".',
            {}, ensure_ratatosk_own_account,
        ),
        PluginTool(
            "ratatosk_list_conversations",
            "Lists Ratatosk conversations (each a group, including 2-person DMs) for the given identity.",
            {"as": str}, ratatosk_list_conversations,
        ),
        PluginTool(
            "ratatosk_get_messages",
            "Gets recent messages in a Ratatosk conversation (by groupId, from ratatosk_list_conversations) "
            "for the given identity, oldest first.",
            {"as": str, "groupId": str, "days": int | None}, ratatosk_get_messages,
        ),
        PluginTool(
            "ratatosk_send_message",
            "Sends a message in a Ratatosk conversation as the given identity. For as:\"owner\" this genuinely "
            "sends as the user, indistinguishable from them typing it themselves -- you have standing "
            "authorization for this specific channel (confirmed with the user), so no separate per-message "
            "confirmation is needed, but the message content still has to be something you actually know to "
            "be true (never invent facts in a message sent to a real person).",
            {"as": str, "groupId": str, "text": str}, ratatosk_send_message,
        ),
        PluginTool(
            "ratatosk_start_chat_with",
            "Finds or creates a direct-message conversation with the given email address, as the given "
            "identity, and returns its groupId (use that with ratatosk_get_messages/ratatosk_send_message).",
            {"as": str, "email": str}, ratatosk_start_chat_with,
        ),
    ],
)
