"""login -- ports backend/src/login.ts's createLoginTool. Lets Caroline
check/establish the user's SquirrelWisdom login -- needed by Notes/email/
Ratatosk/consult/OnlyOffice today, reused by all of them (see login_api.py
for the shared credentials file and sw_gate.py for the auto-popup gate
every SW-gated tool call site uses).

The password NEVER flows through the model's own context: the form opens
in Caroline's native viewer window and posts credentials straight to this
backend over the app's own WebSocket "login_submit" control op (see
main.py) -- same reasoning as viewer_plugin.py's open_in_viewer returning
immediately rather than blocking the turn, just applied to "keep secrets
out of the transcript" instead of "don't freeze the conversation".
"""

from __future__ import annotations

from typing import Any

from app.login_api import is_logged_in, logged_in_email, open_login_request
from app.plugins.loader import Plugin, PluginTool
from app.session_context import get_send


async def ensure_squirrelwisdom_login(_args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    if is_logged_in():
        return {"text": f"Already logged in as {logged_in_email()}."}
    await open_login_request(get_send())
    return {"text": "Opened the SquirrelWisdom login form for the user. I'll let you know once they log in or cancel."}


PLUGIN = Plugin(
    name="login",
    tools=[
        PluginTool(
            "ensure_squirrelwisdom_login",
            "Checks whether the user is logged into their SquirrelWisdom account (needed for Notes and other "
            "SquirrelWisdom-backed features). If already logged in, returns immediately -- nothing else to do. "
            "If not, opens a native login form in Caroline's own viewer window and returns immediately; you'll "
            "be nudged separately once the user logs in or cancels. NEVER ask the user to type their email or "
            "password into the chat itself -- always use this tool instead.",
            {}, ensure_squirrelwisdom_login,
        ),
    ],
)
