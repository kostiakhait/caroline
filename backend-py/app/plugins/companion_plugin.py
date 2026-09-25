"""companion -- Caroline's side of the Android companion app (see the
caroline-android-companion plan). Lets Caroline send a real SMS from a
paired phone's own SIM, read that phone's SMS threads, and read its
contacts. All traffic goes through Camerlengo's session-scoped variable
store (companion_api.py) -- there is never a direct phone<->PC connection.

Multi-phone (explicit instruction, 2026-09-26): more than one phone can be
paired to the same account. Every tool that acts on/reads ONE phone takes
an optional `fromNumber` to pick which one; omitted, it auto-selects the
only paired phone, or fails with a clear message listing the paired
numbers if there's more than one (see _device_error_text). SMS threads and
contact lookups additionally support querying ALL paired phones at once
(the default for the list/search tools, each result tagged with its own
sourceNumber) since "read across every phone" is a meaningful, common
request on its own, not just a fallback. companion_list_phones lets
Caroline discover what's paired before deciding.

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
    AmbiguousDeviceError,
    NoPairedDeviceError,
    PhoneUnreachableError,
    UnknownDeviceError,
    list_devices,
    request_response,
    resolve_device,
    send_sms,
)
from app.plugins.companion_sms_store import last_synced_at, list_all_threads, list_messages, list_threads, load_store
from app.plugins.loader import Plugin, PluginTool
from app.session_context import get_send, get_tab_id
from app.sw_gate import require_sw_or_prompt
from app.workspace_dir import WORKSPACE_DIR


def _device_error_text(exc: Exception) -> str:
    """Turns one of companion_api's device-resolution errors into a clear,
    model-facing message -- always names the paired numbers it knows
    about so the model can either ask the user which phone, or retry with
    a fromNumber it can infer from context."""
    if isinstance(exc, NoPairedDeviceError):
        return "No phone is currently paired to this account. The user needs to enable the companion feature on a phone first."
    numbers = [d.get("phoneNumber") or "(number not set)" for d in getattr(exc, "devices", [])]
    if isinstance(exc, AmbiguousDeviceError):
        return (
            f"More than one phone is paired ({', '.join(numbers)}) -- pass fromNumber to say which one, or call "
            "companion_list_phones first if you're not sure."
        )
    if isinstance(exc, UnknownDeviceError):
        return f"'{exc.phone_number}' doesn't match any paired phone. Paired numbers: {', '.join(numbers) or '(none)'}."
    return str(exc)


async def _gate() -> str | None:
    """Returns an error message if the SW login gate fails, else None."""
    gate = await require_sw_or_prompt(get_send())
    return None if gate.ok else gate.message


def _current_tab_id() -> str:
    tab_id = get_tab_id()
    if tab_id is None:
        raise RuntimeError("companion tool called outside a live ChatSession turn -- no tab_id in context.")
    return tab_id


async def companion_list_phones(args: dict[str, Any], report_progress: Any) -> dict[str, Any]:
    gate_msg = await _gate()
    if gate_msg:
        return {"text": gate_msg, "is_error": True}
    devices = await list_devices()
    return {"text": _json([{"phoneNumber": d.get("phoneNumber"), "model": d.get("model"), "online": d.get("online")} for d in devices])}


async def companion_sms_send(args: dict[str, Any], report_progress: Any) -> dict[str, Any]:
    gate_msg = await _gate()
    if gate_msg:
        return {"text": gate_msg, "is_error": True}
    to = str(args["to"]).strip()
    text = str(args["text"])
    if not to or not text:
        return {"text": "Both 'to' (a phone number) and 'text' are required.", "is_error": True}
    from_number = args.get("fromNumber")
    try:
        device = await resolve_device(str(from_number).strip() if from_number else None)
    except (AmbiguousDeviceError, UnknownDeviceError, NoPairedDeviceError) as exc:
        return {"text": _device_error_text(exc), "is_error": True}
    try:
        result = await send_sms(WORKSPACE_DIR, _current_tab_id(), device["deviceId"], to, text, report_progress)
    except PhoneUnreachableError as exc:
        return {"text": str(exc), "is_error": True}
    if result.get("ok"):
        sent_from = f" from {device['phoneNumber']}" if device.get("phoneNumber") else ""
        return {"text": f"SMS sent to {to}{sent_from}."}
    return {"text": f"The phone couldn't send the SMS: {result.get('error', 'unknown error')}", "is_error": True}


async def companion_list_sms_threads(args: dict[str, Any], report_progress: Any) -> dict[str, Any]:
    gate_msg = await _gate()
    if gate_msg:
        return {"text": gate_msg, "is_error": True}
    # Reads the LOCAL copy (companion_sms_store.py) -- never talks to a
    # phone live, see this module's own header comment for why. Mirrors
    # email_list_messages' own unseenOnly: set up a schedule_reminder
    # ("every 15 minutes check my SMS") the same way you would for email,
    # calling this with unreadOnly=true each time and [[NO_UPDATE]] when
    # there's genuinely nothing new -- the periodic BACKGROUND sync that
    # keeps the local copy fresh is a separate, independent mechanism
    # (companion_api.start_sms_sync_loop), not triggered by this call.
    unread_only = bool(args.get("unreadOnly"))
    from_number = args.get("fromNumber")
    store = load_store(WORKSPACE_DIR)
    if from_number:
        try:
            device = await resolve_device(str(from_number).strip())
        except (AmbiguousDeviceError, UnknownDeviceError, NoPairedDeviceError) as exc:
            return {"text": _device_error_text(exc), "is_error": True}
        threads = list_threads(store, device["deviceId"], unread_only=unread_only)
        synced = last_synced_at(store, device["deviceId"])
    else:
        # No phone named -- across every paired phone, each thread already
        # tagged with its own phoneNumber (see list_all_threads).
        threads = list_all_threads(store, unread_only=unread_only)
        synced = last_synced_at(store)
    return {"text": _json({"lastSyncedAt": synced, "threads": threads})}


async def companion_read_sms_thread(args: dict[str, Any], report_progress: Any) -> dict[str, Any]:
    gate_msg = await _gate()
    if gate_msg:
        return {"text": gate_msg, "is_error": True}
    thread_id = str(args["thread_id"]).strip()
    if not thread_id:
        return {"text": "'thread_id' is required (get one from companion_list_sms_threads).", "is_error": True}
    # Also reads the local copy -- see companion_list_sms_threads above.
    # thread_id is the composite, device-scoped key companion_list_sms_
    # threads already returned -- it alone is enough to find the right
    # phone's bucket, no separate fromNumber needed here.
    store = load_store(WORKSPACE_DIR)
    messages = list_messages(store, thread_id)
    if messages is None:
        return {"text": f"No SMS thread found for '{thread_id}' -- get a thread_id from companion_list_sms_threads first.", "is_error": True}
    return {"text": _json({"lastSyncedAt": last_synced_at(store), "messages": messages})}


async def companion_list_contacts(args: dict[str, Any], report_progress: Any) -> dict[str, Any]:
    gate_msg = await _gate()
    if gate_msg:
        return {"text": gate_msg, "is_error": True}
    try:
        contacts = await _contacts_request(args.get("fromNumber"), {"op": "list"}, report_progress)
    except (PhoneUnreachableError, AmbiguousDeviceError, UnknownDeviceError, NoPairedDeviceError) as exc:
        return {"text": _device_error_text(exc) if not isinstance(exc, PhoneUnreachableError) else str(exc), "is_error": True}
    return {"text": _json(contacts)}


async def companion_search_contacts(args: dict[str, Any], report_progress: Any) -> dict[str, Any]:
    gate_msg = await _gate()
    if gate_msg:
        return {"text": gate_msg, "is_error": True}
    query = str(args["query"]).strip()
    if not query:
        return {"text": "'query' is required.", "is_error": True}
    try:
        contacts = await _contacts_request(args.get("fromNumber"), {"op": "search", "query": query}, report_progress)
    except (PhoneUnreachableError, AmbiguousDeviceError, UnknownDeviceError, NoPairedDeviceError) as exc:
        return {"text": _device_error_text(exc) if not isinstance(exc, PhoneUnreachableError) else str(exc), "is_error": True}
    return {"text": _json(contacts)}


async def _contacts_request(from_number: Any, payload: dict[str, Any], report_progress: Any) -> Any:
    """A named phone: that device's own contacts, as-is. No phone named:
    every paired phone's contacts, gathered one after another and tagged
    with sourceNumber -- a deliberate default (not just a single-phone
    fallback), since "look this person up across all my phones" is a
    reasonable ask on its own. Sequential, not concurrent, same reasoning
    as _sms_sync_tick: simplest correct thing, and a handful of phones is
    still fine against the (generous, no-timeout) two-phase protocol."""
    tab_id = _current_tab_id()
    if from_number:
        device = await resolve_device(str(from_number).strip())
        result = await request_response(WORKSPACE_DIR, tab_id, device["deviceId"], "contacts", payload, report_progress)
        return result
    devices = await list_devices()
    if not devices:
        raise NoPairedDeviceError()
    if len(devices) == 1:
        return await request_response(WORKSPACE_DIR, tab_id, devices[0]["deviceId"], "contacts", payload, report_progress)
    merged: list[dict[str, Any]] = []
    for device in devices:
        result = await request_response(WORKSPACE_DIR, tab_id, device["deviceId"], "contacts", payload, report_progress)
        if isinstance(result, list):
            for contact in result:
                merged.append({**contact, "sourceNumber": device.get("phoneNumber")} if isinstance(contact, dict) else contact)
    return merged


def _json(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, indent=2)


def _usage_instructions() -> str:
    return (
        "The companion_* tools reach the user's OWN paired Android phone(s) (their real SIM/number), through the "
        "Caroline companion app. More than one phone can be paired at once -- call companion_list_phones to see "
        "what's currently paired and each one's number. Every tool that acts on one specific phone takes an "
        "optional fromNumber: omit it and it auto-picks the only paired phone, or fails with a clear error "
        "listing the paired numbers if there's more than one (so ask the user which phone, or infer it from "
        "context, then retry with fromNumber set). companion_list_sms_threads / companion_list_contacts / "
        "companion_search_contacts default to querying EVERY paired phone when fromNumber is omitted (each "
        "result tagged with which phone it came from) -- that's a deliberate default, not just a fallback, "
        "since \"across all my phones\" is a normal thing to ask. companion_sms_send is the one exception: it "
        "REQUIRES an unambiguous single phone (auto-picked only when exactly one is paired) since a real message "
        "can only go out from one SIM at a time.\n\n"
        "Each two-phase call (send, contacts) is transactional: the phone first has to ACCEPT the request (no "
        "timeout on this -- it may take a while if the phone is asleep or briefly offline; you'll just keep "
        "polling) and only then does a bounded window start for it to actually FINISH (about 10 minutes after "
        "acceptance). A 'phone unreachable'-style error only means that finishing window elapsed AFTER "
        "acceptance -- it does not mean the app isn't paired at all. Cancel via stop_operation if the user no "
        "longer wants to wait; nothing else gives up on its own.\n\n"
        "companion_sms_send sends a REAL text message from the user's real phone number to a real recipient -- "
        "this is a higher-consequence action than most tools you have. Do not send one on your own initiative "
        "or on a vague instruction; only send when the user has clearly asked you to send a specific message to "
        "a specific person/number, and quote back the exact recipient (and which phone, if more than one is "
        "paired) and text you're about to send if there's any ambiguity.\n\n"
        "companion_list_contacts / companion_search_contacts are read-only phone lookups (two-phase, may take a "
        "while -- same timing note as above). Contacts and SMS content are personal data: use what you read to "
        "do what the user asked, don't volunteer or repeat more of it than the task needs.\n\n"
        "companion_list_sms_threads / companion_read_sms_thread are DIFFERENT from every other companion_* tool: "
        "they never talk to a phone live, they read Caroline's own local copy of the SMS, kept fresh "
        "automatically in the background every ~3 minutes for every paired phone -- always instant, no waiting, "
        "no 'phone unreachable' possible. Because the sync is automatic, YOU still have to actually call "
        "companion_list_sms_threads (unreadOnly=true) to notice anything new -- the sync itself never tells you "
        "or the user about new messages, it only keeps the data ready for when you check. If the user wants to "
        "be told about new texts periodically (\"let me know when I get a new SMS\", \"check my texts every 15 "
        "minutes\"), set that up with schedule_reminder the same way you would for \"check my email every "
        "morning\" -- on each firing, call companion_list_sms_threads(unreadOnly=true) and reply with exactly "
        "[[NO_UPDATE]] if there's genuinely nothing new. Each thread's own threadId already encodes which phone "
        "it's from -- pass it straight to companion_read_sms_thread, no fromNumber needed there."
    )


PLUGIN = Plugin(
    name="companion",
    usage_instructions=_usage_instructions(),
    tools=[
        PluginTool(
            "companion_list_phones",
            "List every Android phone currently paired to this account (phone number, model, and whether it's "
            "recently checked in). Call this when more than one phone might be paired and you need to know "
            "which numbers exist before picking a fromNumber for another companion_* tool.",
            {},
            companion_list_phones,
        ),
        PluginTool(
            "companion_sms_send",
            "Send a real SMS from one of the user's own paired Android phones (their real number) to a "
            "recipient. Higher real-world consequence than most tools -- only use it when the user has clearly "
            "asked you to send a specific message to a specific number. If more than one phone is paired, pass "
            "fromNumber to say which one sends it (otherwise this fails with the list of paired numbers to "
            "choose from). Never gives up until the phone accepts AND then finishes (or the request is "
            "cancelled) -- see get_tool_instructions for the exact timing.",
            {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "The recipient's phone number."},
                    "text": {"type": "string", "description": "The message text."},
                    "fromNumber": {"type": "string", "description": "Which paired phone to send from, if more than one is paired. Optional when only one phone is paired."},
                },
                "required": ["to", "text"],
            },
            companion_sms_send,
        ),
        PluginTool(
            "companion_list_sms_threads",
            "List SMS conversation threads (recent threads, each with a thread id, the other party, a snippet, "
            "an unread count, and which phone it's on), plus lastSyncedAt. Reads Caroline's own local copy -- "
            "instant, no waiting, works even if a phone is offline right now (may just be slightly stale). "
            "Omit fromNumber to list across EVERY paired phone at once (the normal case); pass it to look at "
            "just one. Pass unreadOnly=true to check for new messages only, and see get_tool_instructions for "
            "how to check periodically via schedule_reminder.",
            # Full JSON schema, not the {name: type} shorthand: that shorthand
            # makes every key required regardless of `| None`, which would
            # force these on every call.
            {
                "type": "object",
                "properties": {
                    "unreadOnly": {"type": "boolean", "description": "Only return threads with unread messages. Defaults to false (all recent threads)."},
                    "fromNumber": {"type": "string", "description": "Only list threads from this paired phone. Defaults to every paired phone."},
                },
                "required": [],
            },
            companion_list_sms_threads,
        ),
        PluginTool(
            "companion_read_sms_thread",
            "Read the messages in one SMS thread, by its thread id (from companion_list_sms_threads -- that id "
            "already identifies which phone it's on, so nothing else is needed here). Reads Caroline's own "
            "local copy -- instant, no waiting.",
            {"thread_id": str},
            companion_read_sms_thread,
        ),
        PluginTool(
            "companion_list_contacts",
            "List contacts (name + numbers). Omit fromNumber to gather contacts from EVERY paired phone at once "
            "(each tagged with sourceNumber); pass it to look at just one phone's contacts. May take a while -- "
            "see get_tool_instructions.",
            {
                "type": "object",
                "properties": {"fromNumber": {"type": "string", "description": "Only look at this paired phone's contacts. Defaults to every paired phone."}},
                "required": [],
            },
            companion_list_contacts,
        ),
        PluginTool(
            "companion_search_contacts",
            "Search contacts by name or number substring. Omit fromNumber to search EVERY paired phone's "
            "contacts at once (each match tagged with sourceNumber); pass it to search just one phone. May take "
            "a while -- see get_tool_instructions.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Name or number substring to search for."},
                    "fromNumber": {"type": "string", "description": "Only search this paired phone's contacts. Defaults to every paired phone."},
                },
                "required": ["query"],
            },
            companion_search_contacts,
        ),
    ],
)
