"""whatsapp -- LLM-facing tools over app/whatsapp_channel.py's WhatsApp
connection. Fourth of the planned messenger integrations
(docs/MESSENGER_INTEGRATIONS_PLAN.md, 2026-10-06).

Acts AS the user's personal WhatsApp account via an unofficial protocol
library (Baileys, Node sidecar) -- there is no official API for a
personal account. See whatsapp_channel.py's own doc comment for the real,
stated risk (WhatsApp could restrict/ban the account at their discretion).
"""

from __future__ import annotations

from typing import Any

from app.whatsapp_channel import WhatsAppError, is_connected, list_chats, send_message, start_login
from app.workspace_dir import WORKSPACE_DIR
from app.plugins.loader import Plugin, PluginTool


async def whatsapp_login(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    try:
        message = await start_login(WORKSPACE_DIR)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": message}


async def whatsapp_list_chats(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    try:
        chats = await list_chats()
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": str(chats)}


async def whatsapp_send_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    to = str(args["to"]).strip()
    text = str(args["text"])
    try:
        await send_message(to, text)
    except WhatsAppError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Sent to {to}."}


def _usage_instructions() -> str:
    return (
        "Acts as the user's own personal WhatsApp account (their real chats), via an unofficial protocol "
        "library (Baileys) -- there is no official API for a personal account, so WhatsApp could in principle "
        "restrict or ban the account for this at their own discretion. `to` everywhere is the exact WhatsApp "
        "jid from whatsapp_list_chats or a proactive new-message notification, never a guessed name/number. "
        "whatsapp_login is one-time setup (a QR code image opens automatically to scan) -- call it only when "
        "the user has explicitly asked to connect WhatsApp, and make sure they understand the risk stated "
        "above before doing so, never on your own initiative. WhatsApp has no model-facing search here -- "
        "whatsapp_list_chats only returns chats already seen since the connection came up (Baileys keeps no "
        "history by default)."
    )


PLUGIN = Plugin(
    name="whatsapp",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "whatsapp_login",
            "Connects Caroline to the user's personal WhatsApp account: starts the connection and opens a QR "
            "code image to scan with WhatsApp on their phone (Linked Devices > Link a Device). Call only when "
            "the user has explicitly asked, after making sure they understand this uses an unofficial library.",
            {}, whatsapp_login,
        ),
        PluginTool(
            "whatsapp_list_chats",
            "Lists WhatsApp chats seen since the connection came up, each with its jid -- the id to use for "
            "whatsapp_send_message.",
            {}, whatsapp_list_chats,
        ),
        PluginTool(
            "whatsapp_send_message",
            "Sends a WhatsApp message, as the user. `to` must be the exact jid (see this plugin's own "
            "usage_instructions).",
            {"to": str, "text": str}, whatsapp_send_message,
        ),
    ],
)
