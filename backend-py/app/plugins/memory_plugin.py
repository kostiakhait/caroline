"""memory -- save_info / request_info: Caroline's memory as the main model
sees it. Two tools and nothing about how memory is arranged behind them (see
docs/MICROAGENTS_PLAN.md).

Behind the tools is the microagent memory service on Camerlengo
(memory:save / memory:request and friends): a dispatcher microagent decides
which memories a text belongs in or where an answer may be -- long-term
facts, topic tracks, the Notes vault and profile -- and each one keeps or
finds its own part. Replies come back marked by source; putting them together
is the main model's job.

WHAT goes into memory is decided by the main model alone, and it passes it
itself in the tool call. Nothing here fetches anything on its own: no letter
is pulled by id, no thread is read, no folder is scanned. The single thing
code adds is the bytes of a document whose path the model named explicitly
-- a tool argument cannot carry a binary file.

Requires the user's SquirrelWisdom account (same gate as every other
SW-backed tool). Memory costs money (the server charges the wallet per
request): when the wallet is empty the top-up window opens by itself on the
first refusal; when the service's own provider is out of money the turn is
stopped and started again from the user's message once it is back (see
memory_turns.py and ChatSession._on_memory_upstream_no_funds).

The local short-term copy of topics (memory_topics.py, shown in the system
prompt) is kept current from here: every save_info reply updates the topic it
touched, and the whole list is refreshed from the server when it is stale.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import uuid
from pathlib import Path
from typing import Any

import httpx

from app import memory_turns
from app.logging_setup import log_event
from app.memory_topics import is_stale, replace_topics, update_topic
from app.plugins.loader import Plugin, PluginTool
from app.plugins.notes_api import SessionManager
from app.plugins.sw_api import API_URL, CAROLINE_SW_KEY, SessionExpiredError, SwApiError
from app.session_context import get_send, get_tab_id
from app.sw_gate import require_sw_or_prompt
from app.workspace_dir import WORKSPACE_DIR

_sessions = SessionManager()

# One memory request may run many small-model calls on the server, whose own
# budget is five minutes. Exactly ONE attempt, never a retry on timeout: a
# save that was received but answered late would otherwise be saved twice.
REQUEST_TIMEOUT_S = 330.0
MAX_FILE_BYTES = 25 * 1024 * 1024       # the server refuses larger documents
MATERIAL_KINDS = ("message", "document")
MATERIAL_TEXT_FIELDS = ("source", "ref", "ts", "from", "subject", "name", "mime", "text")
SOURCE_TOPICS = "topics"

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_NO_FUNDS_USER = "no_funds_user"
STATUS_NO_FUNDS_UPSTREAM = "no_funds_upstream"


class MemoryInputError(Exception):
    """What the model passed cannot be sent; the message is for the model."""


async def _post(body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
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


async def _memory(command: str, **params: Any) -> dict[str, Any]:
    """One memory: command for the logged-in user. Returns the service's
    reply: {status, output, error?, usage, traceId, chargedPia}. Every
    request carries the id of the turn it belongs to (memory_turns.py)."""
    turn_id = memory_turns.current_turn_id(get_tab_id())
    if turn_id:
        params.setdefault("turnId", turn_id)
    return await _sessions.with_session(
        lambda session: _post({"command": command, "key": CAROLINE_SW_KEY, "session": session, **params})
    )


def _language() -> str | None:
    """The language of the conversation in the calling tab, as Caroline
    already tracks it. Fact memory works in English inside and translates
    what it found into this language on the way out."""
    tab_id = get_tab_id()
    if not tab_id:
        return None
    from app.chat_session import current_language_name  # late: chat_session loads the plugins
    return current_language_name(tab_id)


def _build_material(raw: Any, index: int) -> dict[str, Any]:
    """A material exactly as the model gave it. For a document with "path",
    the bytes of that one file are attached; nothing else is read."""
    where = f"materials[{index}]"
    if not isinstance(raw, dict):
        raise MemoryInputError(f"{where} must be an object")
    kind = raw.get("kind")
    if kind not in MATERIAL_KINDS:
        raise MemoryInputError(f'{where}: "kind" must be one of {", ".join(MATERIAL_KINDS)}')
    material: dict[str, Any] = {"kind": kind}
    for field in MATERIAL_TEXT_FIELDS:
        value = raw.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise MemoryInputError(f'{where}: "{field}" must be a string')
        material[field] = value
    if raw.get("to") is not None:
        to = raw["to"]
        if isinstance(to, str):
            to = [to]
        if not isinstance(to, list) or not all(isinstance(item, str) for item in to):
            raise MemoryInputError(f'{where}: "to" must be a list of strings')
        material["to"] = to
    path = raw.get("path")
    if path is not None:
        if kind != "document":
            raise MemoryInputError(f'{where}: "path" is only for a document')
        file = Path(str(path)).expanduser()
        if not file.is_file():
            raise MemoryInputError(f"{where}: there is no file at {path}")
        size = file.stat().st_size
        if size > MAX_FILE_BYTES:
            raise MemoryInputError(f"{where}: {path} is {size} bytes, larger than the {MAX_FILE_BYTES} allowed")
        material["content_b64"] = base64.b64encode(file.read_bytes()).decode("ascii")
        material.setdefault("name", file.name)
        material.setdefault("source", "file")
        material.setdefault("ref", str(file))
        if "mime" not in material:
            guessed, _encoding = mimetypes.guess_type(file.name)
            if guessed:
                material["mime"] = guessed
    if not material.get("text") and "content_b64" not in material:
        raise MemoryInputError(f'{where}: give its "text" (and, for a document file, its "path")')
    return material


# The top-up window is opened by itself on the FIRST refusal for an empty
# wallet, then not again until memory has worked once more -- same manners as
# sw_gate's login window.
_topup_window_shown = False


async def _open_topup_window() -> bool:
    """True when the window was opened by this call."""
    global _topup_window_shown
    if _topup_window_shown:
        return False
    _topup_window_shown = True
    try:
        from app.subscription_mode import create_topup_checkout_url, invalidate_sw_status
        checkout_url = await create_topup_checkout_url()
        invalidate_sw_status()  # the balance is about to change on purpose
        await get_send()({"type": "open_payment", "requestId": uuid.uuid4().hex, "checkoutUrl": checkout_url})
        log_event("plugin:memory", "topup_window_opened")
        return True
    except Exception as exc:
        log_event("plugin:memory", "topup_window_failed", error=str(exc))
        return False


async def _react_to_status(reply: dict[str, Any]) -> str | None:
    """What Caroline itself does about a money refusal, beyond telling the
    model; returns a sentence to add to the message for the model."""
    global _topup_window_shown
    status = reply.get("status")
    if status in (STATUS_OK, STATUS_EMPTY):
        _topup_window_shown = False
        return None
    if status == STATUS_NO_FUNDS_USER:
        if await _open_topup_window():
            return "I've opened the top-up window for the user -- tell them, and continue once they have topped up."
        return "The top-up window was already opened for this; point the user to Settings -> Account & Billing."
    if status == STATUS_NO_FUNDS_UPSTREAM:
        reason = (reply.get("error") or {}).get("message") or "the memory service's provider is out of funds"
        if memory_turns.notify_upstream_no_funds(get_tab_id(), reason):
            return ("This turn is being stopped now and will be started again from the user's message by itself "
                    "once the service has funds; do nothing more in this turn.")
    return None


def _failure_text(reply: dict[str, Any], doing: str) -> str | None:
    """A message for the model when the whole request failed, else None."""
    status = reply.get("status")
    if status in (STATUS_OK, STATUS_EMPTY):
        return None
    if status == STATUS_NO_FUNDS_USER:
        return f"{doing} was refused: the user's SquirrelWisdom balance is too low. Memory is unavailable until they top up; do not retry."
    if status == STATUS_NO_FUNDS_UPSTREAM:
        return f"{doing} could not run: the memory service itself is out of funds on its provider's side. Retrying will not help."
    error = reply.get("error") or {}
    return f"{doing} failed ({error.get('code') or status}): {error.get('message') or 'no details'}"


def _reply_text(reply: dict[str, Any]) -> str:
    payload = {"status": reply.get("status"), **(reply.get("output") or {})}
    return json.dumps(payload, ensure_ascii=False)


async def _refresh_topics_if_stale() -> None:
    """Best effort; the prompt's topic list simply stays as it was on failure."""
    if not is_stale(WORKSPACE_DIR):
        return
    try:
        reply = await _memory("memory:topics", tiers=["today", "week"])
        if reply.get("status") in (STATUS_OK, STATUS_EMPTY):
            replace_topics(WORKSPACE_DIR, (reply.get("output") or {}).get("tiers") or {})
    except Exception as exc:
        log_event("plugin:memory", "topics_refresh_failed", error=str(exc))


async def _gate() -> dict[str, Any] | None:
    gate = await require_sw_or_prompt(get_send())
    return None if gate.ok else {"text": gate.message, "is_error": True}


async def save_owner_profile_text(text: str) -> dict[str, Any]:
    """memory:saveProfile -- used by owner_profile_plugin.py's owner_profile_remember.
    The owner's profile is never reached through save_info: it is chosen on purpose."""
    refused = await _gate()
    if refused:
        return refused
    text = (text or "").strip()
    if not text:
        return {"text": 'owner_profile_remember needs "text": what to keep about the owner.', "is_error": True}
    reply = await _memory("memory:saveProfile", text=text, traceId=uuid.uuid4().hex, language=_language())
    reaction = await _react_to_status(reply)
    failure = _failure_text(reply, "Saving to the owner's profile")
    if failure:
        return {"text": " ".join(filter(None, [failure, reaction])), "is_error": True}
    return {"text": _reply_text(reply)}


async def request_owner_profile(query: str) -> dict[str, Any]:
    """memory:requestProfile -- used by owner_profile_plugin.py's owner_profile_recall."""
    refused = await _gate()
    if refused:
        return refused
    query = (query or "").strip()
    if not query:
        return {"text": 'owner_profile_recall needs "query": what you want to know about the owner.', "is_error": True}
    reply = await _memory("memory:requestProfile", query=query, traceId=uuid.uuid4().hex, language=_language())
    reaction = await _react_to_status(reply)
    failure = _failure_text(reply, "Reading the owner's profile")
    if failure:
        return {"text": " ".join(filter(None, [failure, reaction])), "is_error": True}
    if reply.get("status") == STATUS_EMPTY:
        return {"text": f'The owner\'s profile holds nothing about "{query}".'}
    return {"text": _reply_text(reply)}


async def save_info(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    text = (args.get("text") or "").strip()
    if not text:
        return {"text": 'save_info needs "text": what to remember, in your own words.', "is_error": True}
    try:
        materials = [_build_material(raw, index) for index, raw in enumerate(args.get("materials") or [])]
    except MemoryInputError as exc:
        return {"text": f"Nothing was saved: {exc}", "is_error": True}
    params: dict[str, Any] = {"text": text, "traceId": uuid.uuid4().hex, "language": _language()}
    if materials:
        params["materials"] = materials
    reply = await _memory("memory:save", **params)
    reaction = await _react_to_status(reply)
    for result in (reply.get("output") or {}).get("results") or []:
        if result.get("source") == SOURCE_TOPICS and result.get("status") == STATUS_OK:
            update_topic(WORKSPACE_DIR, (result.get("output") or {}).get("topic") or {})
    await _refresh_topics_if_stale()
    failure = _failure_text(reply, "Saving")
    if failure:
        return {"text": " ".join(filter(None, [failure, reaction])) + ("\n" + _reply_text(reply) if reply.get("output") else ""), "is_error": True}
    if reply.get("status") == STATUS_EMPTY:
        return {"text": "Nothing in this text was judged worth remembering, so nothing was saved."}
    return {"text": _reply_text(reply)}


async def request_info(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    refused = await _gate()
    if refused:
        return refused
    topic, material = args.get("topic"), args.get("material")
    if material:
        if not topic:
            return {"text": 'To read a material in full give both "topic" and "material" (their ids from an earlier request_info reply).', "is_error": True}
        return await _read_material(topic, material, args.get("offset"), args.get("save_file_to"))
    query = (args.get("query") or "").strip()
    if not query:
        return {"text": 'request_info needs "query": what you want to recall.', "is_error": True}
    params: dict[str, Any] = {"query": query, "traceId": uuid.uuid4().hex, "language": _language()}
    if args.get("history"):
        params["history"] = int(args["history"])
    reply = await _memory("memory:request", **params)
    reaction = await _react_to_status(reply)
    await _refresh_topics_if_stale()
    failure = _failure_text(reply, "The memory request")
    if failure:
        return {"text": " ".join(filter(None, [failure, reaction])) + ("\n" + _reply_text(reply) if reply.get("output") else ""), "is_error": True}
    if reply.get("status") == STATUS_EMPTY:
        return {"text": f'Memory holds nothing relevant to "{query}".'}
    return {"text": _reply_text(reply)}


async def _read_material(topic: str, material: str, offset: Any, save_file_to: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"topic": topic, "material": material}
    if offset:
        params["offset"] = int(offset)
    if save_file_to:
        params["file"] = True
    reply = await _memory("memory:material", **params)
    reaction = await _react_to_status(reply)
    failure = _failure_text(reply, "Reading the material")
    if failure:
        return {"text": " ".join(filter(None, [failure, reaction])), "is_error": True}
    output = dict(reply.get("output") or {})
    content = output.pop("content_b64", None)
    if save_file_to:
        if content is None:
            output["file"] = "this material has no file, only its text"
        else:
            target = Path(str(save_file_to)).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(content))
            output["file"] = f"saved to {target}"
    return {"text": json.dumps(output, ensure_ascii=False)}


_USAGE_INSTRUCTIONS = (
    "save_info and request_info are your memory. You decide what goes in and you pass it yourself -- nothing is "
    "fetched for you: if a letter or a message should be remembered, read it with your usual tools first and "
    "pass what matters here.\n"
    "save_info(text, materials?): \"text\" is what to remember, in your own words, complete enough to make sense "
    "on its own later (full names, not pronouns; dates, not \"tomorrow\"). One call may mix kinds of things; "
    "memory sorts them itself: lasting facts about people, organizations and things; the course of a topic you "
    "are discussing or working on (what happened, what was decided, what is open); credentials. Save right when "
    "you learn something, not at the end of the conversation.\n"
    "What concerns your OWNER themselves -- who they are, their preferences, their personal details -- does NOT "
    "go through save_info and is not found by request_info: it has tools of its own, owner_profile_remember and "
    "owner_profile_recall. Choose them deliberately when, and only when, the thing is about your owner.\n"
    "\"materials\" (optional) are the letters, messages and documents of the topic, kept WHOLE alongside its "
    "summary -- pass them when the exact wording may be needed later. Each is an object: {\"kind\": \"message\", "
    "\"source\": \"email\"|\"sms\"|\"telegram\"|..., \"ref\": the message's own id, \"ts\": when it was sent, "
    "\"from\", \"to\": [..], \"subject\", \"text\": its full text} or {\"kind\": \"document\", \"name\", "
    "\"text\": the document's text as you read it, \"path\": the local file} -- with \"path\" the file itself "
    "is stored too. Always give \"source\" and \"ref\" when the thing has them: the same letter sent again is "
    "then kept once.\n"
    "request_info(query): ask in plain words what you want to recall. The reply lists what each memory found, "
    "marked by \"source\" (facts, topics, notes.vault, ...); combine them yourself. A fact comes "
    "with \"people\": the gender of the people it names, where memory knows it -- use it to speak of them "
    "correctly (he/she, the endings your language needs); for a person not listed there the gender is not "
    "known, do not guess it from the name. A fact may be "
    "marked doubtful or denied when something saved later contradicted it -- say so rather than stating it as "
    "certain. A topic comes with its summary, results, open questions and a list of its materials without their "
    "text; add \"history\": N to also get its last N episodes. To read one material in full call "
    "request_info(topic=<topic id>, material=<material id>); long texts come in pages (\"next_offset\" -> pass "
    "it as \"offset\"); add \"save_file_to\": <path> to also get a document's file written there.\n"
    "Call request_info BEFORE searching Notes, mail or old conversation for something you may already know, and "
    "before asking the user to repeat it.\n"
    "The older memory tools -- recall_memory, working_memory_*, topic_upsert/topics_list/topic_close -- are "
    "deprecated and read-only: what they hold can still be read, nothing new is written "
    "through them. notes_* tools work as before for notes the user asks about by name."
)


PLUGIN = Plugin(
    name="memory",
    usage_instructions=_USAGE_INSTRUCTIONS,
    tools=[
        PluginTool(
            "save_info",
            "Remembers something for later: a fact, where a topic you are discussing or working on stands, a "
            "credential. NOT for facts about your owner themselves -- use owner_profile_remember for those. "
            "\"text\" is what to remember, in your own words; optional "
            "\"materials\" are the whole letters/messages/documents of the topic (see this tool's usage "
            "instructions for their shape). You decide what to save and pass it yourself -- nothing is fetched "
            "for you.",
            {"text": str, "materials": list | None}, save_info,
        ),
        PluginTool(
            "request_info",
            "Recalls from your memory: facts, topics (summary, decisions, open questions, their letters and "
            "documents), credentials. Not the owner's profile -- that is owner_profile_recall. Give \"query\" in plain words; or \"topic\" and "
            "\"material\" ids from an earlier reply to read one letter/document in full. Call this before "
            "searching Notes or mail for something you may already know.",
            {
                "query": str | None, "history": int | None, "topic": str | None, "material": str | None,
                "offset": int | None, "save_file_to": str | None,
            },
            request_info,
        ),
    ],
)
