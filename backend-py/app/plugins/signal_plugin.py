"""signal -- LLM-facing tools over app/signal_channel.py's Signal
connection. Fifth and last of the planned messenger integrations
(docs/MESSENGER_INTEGRATIONS_PLAN.md, 2026-10-06).

Acts AS the user's own Signal account (linked device via signal-cli) --
Signal has no bot/app API at all, this is the standard way any
third-party integration talks to it.
"""

from __future__ import annotations

from typing import Any

from app.signal_channel import SignalError, is_connected, list_contacts, send_message, start_login
from app.workspace_dir import WORKSPACE_DIR
from app.plugins.loader import Plugin, PluginTool


async def signal_login(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    try:
        message = await start_login(WORKSPACE_DIR)
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": message}


async def signal_list_contacts(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    try:
        contacts = await list_contacts()
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": str(contacts)}


async def signal_send_message(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    to = str(args["to"]).strip()
    text = str(args["text"])
    try:
        await send_message(to, text)
    except SignalError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": f"Sent to {to}."}


def _usage_instructions() -> str:
    return (
        "Acts as the user's own Signal account (linked as a secondary device via signal-cli, the standard "
        "tool for this -- Signal has no bot/app API at all). `to` is the exact phone number (E.164, e.g. "
        "+15551234567) from signal_list_contacts or a proactive new-message notification. signal_login is "
        "one-time setup (a QR code image opens automatically to scan in the Signal app's Linked Devices "
        "screen) -- call it only when the user has explicitly asked to connect Signal, never on your own "
        "initiative. There's no model-facing message search here -- only contacts and sending."
    )


PLUGIN = Plugin(
    name="signal",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "signal_login",
            "Connects Caroline to the user's Signal account as a linked device: opens a QR code image to "
            "scan in the Signal app (Settings > Linked Devices > Link New Device). Call only when the user "
            "has explicitly asked to connect Signal.",
            {}, signal_login,
        ),
        PluginTool(
            "signal_list_contacts",
            "Lists the user's Signal contacts known to signal-cli.",
            {}, signal_list_contacts,
        ),
        PluginTool(
            "signal_send_message",
            "Sends a Signal message, as the user. `to` must be the exact phone number (E.164 format).",
            {"to": str, "text": str}, signal_send_message,
        ),
    ],
)
