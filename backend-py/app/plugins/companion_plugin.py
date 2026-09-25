"""companion -- Caroline's side of the Android companion app (see the
caroline-android-companion plan). Lets Caroline send a real SMS from the
paired phone's own SIM, read that phone's SMS threads, and read its
contacts. All traffic goes through Camerlengo's session-scoped variable
store (companion_api.py) -- there is never a direct phone<->PC connection.

companion_sms_send and the contacts lookups are transactional and
two-phase (accept, then result -- see companion_api.py's own module
docstring for the full protocol) and NEVER give up on their own; only an
explicit cancel (Stop / stop_operation) ends one early. Because that can
take a while, these run through app/operations.py's start/status/stop
contract exactly like every other tool -- a slow one returns an
operation_id the model polls via check_operation_status, and
report_progress feeds that poll a human-readable "still waiting" line;
app/operations.py's own REGISTRY is what ChatSession.stop() reaches via
cancel_for_tab() to actually cancel one. These operations are ALSO
journaled locally (see companion_api.run_operation) so they survive a
backend restart -- that part is transparent to these tool handlers.

companion_list_sms_threads/companion_read_sms_thread are DIFFERENT (per
explicit instruction, 2026-09-25): a phone is not reliably reachable the
way an IMAP server is, so these never talk to the phone live at all --
they read Caroline's own local copy (companion_sms_store.py), kept fresh
by a separate background sync loop (companion_api.start_sms_sync_loop,
every 3 minutes, best-effort -- silently skips a cycle if the phone
doesn't answer in time). Instant, and correct as of the last successful
sync regardless of whether the phone happens to be reachable right this
moment.

SW-login-gated (needs the user's own SquirrelWisdom account -- the same
one paired to the Android app). Uses sw_gate.py's shared
require_sw_or_prompt, the plain-error-if-logged-out convention every other
SW-gated plugin in backend-py already uses (the native auto-popup login
window isn't ported here yet -- see consult_plugin.py / ratatosk_plugin.py
for the same note).

Tab message-exchange (writing bounded recent history to
`tabs/<tabId>/history` after each turn, and polling `tabs/<tabId>/inbox`
to inject a phone-originated message into a live tab) is engine-level
wiring that belongs in chat_session.py / main.py, next to the scheduler
due-check loop -- NOT a plugin tool -- so it lives there, not here.
"""

from __future__ import annotations

from typing import Any

from app.plugins.companion_api import (
    PhoneUnreachableError,
    request_response,
    send_sms,
)
from app.plugins.companion_sms_store import last_synced_at, list_messages, list_threads, load_store
from app.plugins.loader import Plugin, PluginTool
from app.session_context import get_send, get_tab_id
from app.sw_gate import require_sw_or_prompt
from app.workspace_dir import WORKSPACE_DIR


async def _gate() -> str | None:
    """Returns an error message if the SW login gate fails, else None."""
    gate = await require_sw_or_prompt(get_send())
    return None if gate.ok else gate.message


def _current_tab_id() -> str:
    tab_id = get_tab_id()
    if tab_id is None:
        raise RuntimeError("companion tool called outside a live ChatSession turn -- no tab_id in context.")
    return tab_id


async def companion_sms_send(args: dict[str, Any], report_progress: Any) -> dict[str, Any]:
    gate_msg = await _gate()
    if gate_msg:
        return {"text": gate_msg, "is_error": True}
    to = str(args["to"]).strip()
    text = str(args["text"])
    if not to or not text:
        return {"text": "Both 'to' (a phone number) and 'text' are required.", "is_error": True}
    try:
        result = await send_sms(WORKSPACE_DIR, _current_tab_id(), to, text, report_progress)
    except PhoneUnreachableError as exc:
        return {"text": str(exc), "is_error": True}
    if result.get("ok"):
        return {"text": f"SMS sent to {to} from the phone."}
    return {"text": f"The phone couldn't send the SMS: {result.get('error', 'unknown error')}", "is_error": True}


async def companion_list_sms_threads(args: dict[str, Any], report_progress: Any) -> dict[str, Any]:
    gate_msg = await _gate()
    if gate_msg:
        return {"text": gate_msg, "is_error": True}
    # Reads the LOCAL copy (companion_sms_store.py) -- never talks to the
    # phone live, see this module's own header comment for why. Mirrors
    # email_list_messages' own unseenOnly: set up a schedule_reminder
    # ("every 15 minutes check my SMS") the same way you would for email,
    # calling this with unreadOnly=true each time and [[NO_UPDATE]] when
    # there's genuinely nothing new -- the periodic BACKGROUND sync that
    # keeps the local copy fresh is a separate, independent mechanism
    # (companion_api.start_sms_sync_loop), not triggered by this call.
    store = load_store(WORKSPACE_DIR)
    threads = list_threads(store, unread_only=bool(args.get("unreadOnly")))
    return {"text": _json({"lastSyncedAt": last_synced_at(store), "threads": threads})}


async def companion_read_sms_thread(args: dict[str, Any], report_progress: Any) -> dict[str, Any]:
    gate_msg = await _gate()
    if gate_msg:
        return {"text": gate_msg, "is_error": True}
    thread_id = str(args["thread_id"]).strip()
    if not thread_id:
        return {"text": "'thread_id' is required (get one from companion_list_sms_threads).", "is_error": True}
    # Also reads the local copy -- see companion_list_sms_threads above.
    store = load_store(WORKSPACE_DIR)
    messages = list_messages(store, thread_id)
    return {"text": _json({"lastSyncedAt": last_synced_at(store), "messages": messages})}


async def companion_list_contacts(args: dict[str, Any], report_progress: Any) -> dict[str, Any]:
    gate_msg = await _gate()
    if gate_msg:
        return {"text": gate_msg, "is_error": True}
    try:
        contacts = await request_response(WORKSPACE_DIR, _current_tab_id(), "contacts", {"op": "list"}, report_progress)
    except PhoneUnreachableError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": _json(contacts)}


async def companion_search_contacts(args: dict[str, Any], report_progress: Any) -> dict[str, Any]:
    gate_msg = await _gate()
    if gate_msg:
        return {"text": gate_msg, "is_error": True}
    query = str(args["query"]).strip()
    if not query:
        return {"text": "'query' is required.", "is_error": True}
    try:
        contacts = await request_response(
            WORKSPACE_DIR, _current_tab_id(), "contacts", {"op": "search", "query": query}, report_progress,
        )
    except PhoneUnreachableError as exc:
        return {"text": str(exc), "is_error": True}
    return {"text": _json(contacts)}


def _json(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, indent=2)


def _usage_instructions() -> str:
    return (
        "The companion_* tools reach the user's OWN paired Android phone (their real SIM/number), through the "
        "Caroline companion app. Each call is transactional and two-phase: the phone first has to ACCEPT the "
        "request (no timeout on this -- it may take a while if the phone is asleep or briefly offline; you'll "
        "just keep polling) and only then does a bounded window start for it to actually FINISH (about 10 "
        "minutes after acceptance). A 'phone unreachable'-style error only means that finishing window elapsed "
        "AFTER acceptance -- it does not mean the app isn't paired at all. Cancel via stop_operation if the "
        "user no longer wants to wait; nothing else gives up on its own.\n\n"
        "companion_sms_send sends a REAL text message from the user's real phone number to a real recipient -- "
        "this is a higher-consequence action than most tools you have. Do not send one on your own initiative "
        "or on a vague instruction; only send when the user has clearly asked you to send a specific message to "
        "a specific person/number, and quote back the exact recipient and text you're about to send if there's "
        "any ambiguity.\n\n"
        "companion_list_contacts / companion_search_contacts are read-only phone lookups (two-phase, may take a "
        "while -- same timing note as above). Contacts and SMS content are personal data: use what you read to "
        "do what the user asked, don't volunteer or repeat more of it than the task needs.\n\n"
        "companion_list_sms_threads / companion_read_sms_thread are DIFFERENT from every other companion_* tool: "
        "they never talk to the phone live, they read Caroline's own local copy of the SMS, kept fresh "
        "automatically in the background every ~3 minutes -- always instant, no waiting, no 'phone unreachable' "
        "possible. Because the sync is automatic, YOU still have to actually call companion_list_sms_threads "
        "(unreadOnly=true) to notice anything new -- the sync itself never tells you or the user about new "
        "messages, it only keeps the data ready for when you check. If the user wants to be told about new "
        "texts periodically (\"let me know when I get a new SMS\", \"check my texts every 15 minutes\"), set "
        "that up with schedule_reminder the same way you would for \"check my email every morning\" -- on each "
        "firing, call companion_list_sms_threads(unreadOnly=true) and reply with exactly [[NO_UPDATE]] if "
        "there's genuinely nothing new."
    )


PLUGIN = Plugin(
    name="companion",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "companion_sms_send",
            "Send a real SMS from the user's own paired Android phone (their real number) to a recipient. "
            "Higher real-world consequence than most tools -- only use it when the user has clearly asked you "
            "to send a specific message to a specific number. Never gives up until the phone accepts AND then "
            "finishes (or the request is cancelled) -- see get_tool_instructions for the exact timing.",
            {"to": str, "text": str},
            companion_sms_send,
        ),
        PluginTool(
            "companion_list_sms_threads",
            "List the SMS conversation threads on the user's paired Android phone (recent threads, each with a "
            "thread id, the other party, a snippet, and an unread count), plus lastSyncedAt. Reads Caroline's "
            "own local copy -- instant, no waiting, works even if the phone is offline right now (may just be "
            "slightly stale). Pass unreadOnly=true to check for new messages only, and see "
            "get_tool_instructions for how to check periodically via schedule_reminder.",
            # Full JSON schema, not the {name: type} shorthand: that shorthand
            # makes every key required regardless of `| None`, which would
            # force unreadOnly on every call.
            {
                "type": "object",
                "properties": {"unreadOnly": {"type": "boolean", "description": "Only return threads with unread messages. Defaults to false (all recent threads)."}},
                "required": [],
            },
            companion_list_sms_threads,
        ),
        PluginTool(
            "companion_read_sms_thread",
            "Read the messages in one SMS thread on the paired Android phone, by its thread id (from "
            "companion_list_sms_threads). Reads Caroline's own local copy -- instant, no waiting.",
            {"thread_id": str},
            companion_read_sms_thread,
        ),
        PluginTool(
            "companion_list_contacts",
            "List the contacts on the user's paired Android phone (name + numbers). May take a while -- see "
            "get_tool_instructions.",
            {},
            companion_list_contacts,
        ),
        PluginTool(
            "companion_search_contacts",
            "Search the paired Android phone's contacts by name or number substring. May take a while -- see "
            "get_tool_instructions.",
            {"query": str},
            companion_search_contacts,
        ),
    ],
)
