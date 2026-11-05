"""Akinakes Agent Bus channel: this process IS one akinakes agent.

Same shape as ratatosk_channel.py: a supervised background loop is the primary interface, no chat window. It ports
agent_lib's wire protocol (POST /messages/poll, POST /messages/publish, the make_reply() envelope) and what AgentLoop
does around the poll (Registry registration and the 30 s heartbeat), to async httpx.

A delivered task becomes a proactive turn on a headless ChatSession, one session per chainId (main.py). The model
answers through the akinakes_publish_result tool (plugins/akinakes_bus_plugin.py), which calls publish_result() here.
The original message is kept by chainId, so the model never builds routing itself. A turn that ends with nothing
published produces the task's "blocked" reply, system.skill_blocked by default (on_turn_end).

A chat.message (a role that talks in chat rooms) passes AgentLoop._intake()'s gate first (scope, then ACL). The model
answers in the room (plugins/akinakes_chat_plugin.py) or passes; a turn that does neither publishes agent.pass, so
chatSecretary moves on at once.

What each message type expects in reply is configuration, not code: akinakes_reply_specs.json (load_reply_specs).

Active only when BUS_URL and AGENT_UID are set.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

import httpx

from app.logging_setup import log_event
from app.task_supervisor import supervise

LONG_POLL_S = 5
POLL_LIMIT = 10
HEARTBEAT_S = 30
ERROR_BACKOFF_S = 2.0
DEFAULT_TTL_S = 600
# Tab ids of the chains' sessions (main.akinakes_tab_id); settings_mirror leaves these tabs out.
TAB_PREFIX = "akinakes-"

InjectFn = Callable[..., None]  # (chain_id, text, the requester's own words or None, relay=False)
BeforeTaskFn = Callable[[], Awaitable[None]]


@dataclass
class ChannelConfig:
    bus_url: str
    agent_uid: str
    role: str
    specialization: list[str]
    description: str
    registry_url: str
    # chat.message intake (AgentLoop._intake): the agent.toml scope as JSON, and ACL Service; empty = no check.
    scope: list[dict[str, Any]] = field(default_factory=list)
    acl_url: str = ""
    chat_url: str = ""
    chat_key: str = ""
    # chatSecretary: sees every human chat.message of every room and decides itself what happens to it (see the
    # "orchestrator" section below). AKINAKES_CHAT_ORCHESTRATOR=1.
    orchestrator: bool = False
    # task./query. types this role takes, declared to the Registry with their description and fields from the reply
    # specs, so chatSecretary (and any other caller) can find the agent and fill the task. AGENT_ACCEPTS, comma separated.
    accepts: list[str] = field(default_factory=list)
    # The secretary of a level, "project" or "bureau" (see the "level secretary" section below).
    # AKINAKES_LEVEL_SECRETARY; empty for every other role.
    level: str = ""


@dataclass
class ChannelStatus:
    configured: bool = False
    last_poll_at_iso: str | None = None
    last_task_at_iso: str | None = None
    last_error: str | None = None
    tasks_received: int = 0
    results_published: int = 0
    blocked_published: int = 0


status = ChannelStatus()
_config: ChannelConfig | None = None
_http: httpx.AsyncClient | None = None
_agent_token = ""
_pending: dict[str, dict[str, Any]] = {}  # chainId -> the task message being worked on
_answered: set[str] = set()  # chainIds with a result already published
# orchestrator state, by chainId of the human message: agents asked, agents that passed; keys of relay turns
_asked: dict[str, set[str]] = {}
_passes: dict[str, set[str]] = {}
_relay_keys: set[str] = set()
_posted: set[str] = set()  # chains the coordinator already answered in the room
_nudged: set[str] = set()  # chains whose turn ended with nothing for the room and got one more turn
_inject: "InjectFn | None" = None
# chains the coordinator published a bus task for -> the room to tell, who asked there and what. Kept past the turn:
# the result comes later, and not every agent echoes room_id in its result (the secretaries' result.secretary_ok
# does not). The request goes into the relay turn's requester words (relay_words).
_bus_tasks: dict[str, dict[str, str]] = {}
# chains the coordinator broadcast a task on (no addressee) that no agent has claimed yet
_claimed: set[str] = set()
# level secretary, by chain of a request it took: its level's room and where the request came from; the agents it gave
# subtasks to and their results as they arrive
_level: dict[str, dict[str, Any]] = {}
_subtasks: dict[str, set[str]] = {}
_subtask_results: dict[str, list[dict[str, Any]]] = {}
_background: set["asyncio.Task[None]"] = set()


def load_config() -> ChannelConfig | None:
    bus_url = (os.environ.get("BUS_URL") or "").strip().rstrip("/")
    agent_uid = (os.environ.get("AGENT_UID") or "").strip()
    if not bus_url or not agent_uid:
        return None
    return ChannelConfig(
        bus_url=bus_url,
        agent_uid=agent_uid,
        role=(os.environ.get("AGENT_ROLE") or "agent").strip(),
        specialization=[s.strip() for s in (os.environ.get("AGENT_SPECIALIZATION") or "").split(",") if s.strip()],
        description=(os.environ.get("AGENT_DESCRIPTION") or "").strip(),
        registry_url=(os.environ.get("AGENT_REGISTRY_URL") or "").strip().rstrip("/"),
        scope=_scope_from_env(),
        acl_url=(os.environ.get("ACL_SERVICE_URL") or "").strip().rstrip("/"),
        chat_url=(os.environ.get("CHAT_SERVICE_URL") or "").strip().rstrip("/"),
        chat_key=(os.environ.get("CHAT_INTERNAL_KEY") or "").strip(),
        orchestrator=(os.environ.get("AKINAKES_CHAT_ORCHESTRATOR") or "").strip().lower() in ("1", "true", "yes"),
        accepts=[t.strip() for t in (os.environ.get("AGENT_ACCEPTS") or "").split(",") if t.strip()],
        level=_level_from_env(),
    )


LEVEL_KINDS = ("project", "bureau")


def _level_from_env() -> str:
    level = (os.environ.get("AKINAKES_LEVEL_SECRETARY") or "").strip().lower()
    if level and level not in LEVEL_KINDS:
        log_event("plugin:akinakes-channel", "level_secretary_unknown", value=level)
        return ""
    return level


def _scope_from_env() -> list[dict[str, Any]]:
    raw = (os.environ.get("AGENT_SCOPE") or "").strip()
    if not raw:
        return []
    try:
        scope = json.loads(raw)
    except ValueError:
        log_event("plugin:akinakes-channel", "agent_scope_unreadable", value=raw[:200])
        return []
    return [entry for entry in scope if isinstance(entry, dict)] if isinstance(scope, list) else []


def get_akinakes_channel_status() -> ChannelStatus:
    return status


CHAT_MODES = ("sw", "claude", "openai")


def chat_mode() -> str:
    """The engine a chain's session runs on: AKINAKES_CHAT_MODE, else sw. main.py writes it into a new chain tab before
    the session starts, because the session's own default-to-sw rule runs later, in its run loop: the task injected
    right after start() was read in the "claude" default and went to a Claude CLI that a container cannot run (the
    silent turn of the 2026-10-31 run). An unknown value falls back to sw."""
    mode = (os.environ.get("AKINAKES_CHAT_MODE") or "").strip().lower()
    if mode and mode not in CHAT_MODES:
        log_event("engine", "akinakes_chat_mode_unknown", value=mode)
    return mode if mode in CHAT_MODES else "sw"


# --- envelope builders, ported from agent_lib/builders.py -------------------------------------------------------------

def make_message(
    sender_uid: str,
    payload_type: str,
    chain_id: str | None = None,
    parent_id: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_S,
    route: list[str] | None = None,
    return_to: str | None = None,
    target_agent_uids: list[str] | None = None,
    **payload_extra: Any,
) -> dict[str, Any]:
    return {
        "envelope": {
            "messageId": str(uuid4()),
            "chainId": chain_id or str(uuid4()),
            "parentId": parent_id,
            "senderUid": sender_uid,
            "scope": {"roles": [], "domains": [], "specialization": [], "stage": []},
            "routing": {
                "targetAgentUids": target_agent_uids or [],
                "targetGroups": [],
                "targetPipelines": [],
                "route": route or [],
                "returnTo": return_to,
                "maxDeliveries": None,
                "clientTag": None,
            },
            "lifecycle": {"ttlSeconds": ttl_seconds, "createdAt": datetime.now(timezone.utc).isoformat(), "expiresAt": None},
            "trace": {"producedBy": sender_uid, "path": []},
            "scores": {"matchScore": None, "selfScore": None},
        },
        "payload": {"type": payload_type, **payload_extra},
    }


def make_reply(original: dict[str, Any], sender_uid: str, payload_type: str, to_sender_if_unrouted: bool = False,
               **payload_extra: Any) -> dict[str, Any]:
    """to_sender_if_unrouted: a reply to a broadcast (no route, no returnTo) goes to its sender by name; without it the
    reply would itself be a broadcast. Used by the level secretaries, whose requests come broadcast."""
    env = original["envelope"]
    routing = env.get("routing") or {}
    route = list(routing.get("route") or [])
    unrouted = not route and not routing.get("returnTo")
    return make_message(
        sender_uid=sender_uid,
        payload_type=payload_type,
        chain_id=env["chainId"],
        parent_id=env["messageId"],
        ttl_seconds=(env.get("lifecycle") or {}).get("ttlSeconds", DEFAULT_TTL_S),
        route=route[1:] if route else [],
        return_to=routing.get("returnTo"),
        target_agent_uids=[env["senderUid"]] if to_sender_if_unrouted and unrouted and env.get("senderUid") else None,
        **payload_extra,
    )


def addressed_to(msg: dict[str, Any], uid: str) -> bool:
    """The message names this agent: first of its route, or among its target agents."""
    routing = (msg.get("envelope") or {}).get("routing") or {}
    route = routing.get("route") or []
    return (bool(route) and route[0] == uid) or uid in (routing.get("targetAgentUids") or [])


def make_agent(config: ChannelConfig) -> dict[str, Any]:
    return {
        "uid": config.agent_uid,
        "version": "1.0.0",
        "groups": [],
        "pipelines": [],
        "capabilities": {
            "role": config.role,
            "specialization": config.specialization,
            "domains": [],
            "description": config.description or None,
            "personal_office_member": False,
        },
    }


# --- reply specs -----------------------------------------------------------------------------------------------------

def load_reply_specs() -> dict[str, dict[str, Any]]:
    """message payload type -> what it expects in reply. From AKINAKES_REPLY_SPECS (a path), else the file next to this
    module. A spec has:
      replies  -- [{payload_type, required, when, schema}]: the replies allowed, "when" says which fits (several for
                  one task: editor's task.edit_section answers one way for a section_address, another for a
                  section_id); "schema" is a JSON schema of the fields, shown to the model and checked on publish
                  (the second real run published article_draft sections as bare names, and Human Gate reads
                  section["name"] and section["content"]);
      blocked  -- {payload_type, reason_field}: the reply when the task cannot be done (result.plan_error with "reason",
                  say), as the old agent sent it; without it, system.skill_blocked;
      echo     -- task payload fields copied into every reply (room_id, which chatSecretary relays by);
      chat     -- true for chat.message: answered in the chat room, or passed (agent.pass);
      description, fields -- what the task is and its payload fields, declared to the Registry by the roles that take it
                  (accepted_messages), so a caller knows whom to send it and how to fill it."""
    path = Path(os.environ.get("AKINAKES_REPLY_SPECS") or Path(__file__).with_name("akinakes_reply_specs.json"))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 -- no specs means "accept any result.*", not a dead channel
        log_event("plugin:akinakes-channel", "reply_specs_unreadable", path=str(path), error=str(exc))
        return {}


def reply_spec(message_type: str) -> dict[str, Any]:
    return load_reply_specs().get(message_type) or {}


def accepted_messages(config: ChannelConfig) -> list[dict[str, Any]]:
    """What this role declares to the Registry (agent_registry AcceptedMessage): its AGENT_ACCEPTS types with the
    task's own description and fields from the reply specs."""
    out = []
    for task_type in config.accepts:
        spec = reply_spec(task_type)
        out.append({"type": task_type, "description": spec.get("description"), "fields": spec.get("fields") or {}})
    return out


def expected_reply_text(task_type: str) -> str:
    replies = reply_spec(task_type).get("replies") or []
    if not replies:
        return "payload_type: a result.* type that fits this task, fields: its result"
    lines = [f'payload_type="{r["payload_type"]}", fields with: {", ".join(r.get("required") or [])}'
             + (f' (fields JSON schema: {json.dumps(r["schema"], ensure_ascii=False)})' if r.get("schema") else "")
             + (f' -- when {r["when"]}' if r.get("when") else "") for r in replies]
    return lines[0] if len(lines) == 1 else "one of:\n" + "\n".join(f"- {line}" for line in lines) + "\n"


def is_chat(chain_id: str) -> bool:
    return bool(reply_spec(pending_task_type(chain_id) or "").get("chat"))


def _echoed(original: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    payload = original.get("payload") or {}
    return {k: payload[k] for k in spec.get("echo") or [] if k in payload}


# --- publishing ------------------------------------------------------------------------------------------------------

async def _publish(message: dict[str, Any]) -> dict[str, Any]:
    assert _config is not None and _http is not None
    resp = await _http.post(f"{_config.bus_url}/messages/publish", json=message)
    resp.raise_for_status()
    return resp.json()


def session_chain_id(given: str) -> str:
    """The chain of the session a tool call runs in: its tab is akinakes-<chainId> (main.akinakes_tab_id). The model's
    own chain_id is used only outside such a session. (2026-11-02, live: the model copied the 36-character chainId
    with one digit wrong, 4099 -> 4096, every akinakes tool refused, and it went on "fixing" it for two minutes.)"""
    from app import session_context

    tab = session_context.get_tab_id() or ""
    return tab[len(TAB_PREFIX):] if tab.startswith(TAB_PREFIX) else given


def pending_task_type(chain_id: str) -> str | None:
    msg = _pending.get(chain_id)
    return ((msg or {}).get("payload") or {}).get("type")


async def publish_result(chain_id: str, payload_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Validates against the task's reply spec, then publishes the reply. Raises ValueError with a message the model can act on."""
    assert _config is not None
    original = _pending.get(chain_id)
    if original is None:
        raise ValueError(f'No task is in progress for chainId "{chain_id}". Use the exact chainId from the task message.')
    _refuse_second_answer(chain_id)
    task_type = pending_task_type(chain_id) or ""
    spec = reply_spec(task_type)
    if spec.get("chat"):
        raise ValueError("A chat message is answered in its room (chat_post_message), or passed (akinakes_pass).")
    replies = {r["payload_type"]: r for r in spec.get("replies") or []}
    if replies:
        if payload_type not in replies:
            raise ValueError(f'For {task_type} the payload_type must be one of: {", ".join(replies)}; not "{payload_type}".')
        missing = [k for k in replies[payload_type].get("required") or [] if k not in fields]
        if missing:
            raise ValueError(f"Missing fields for {payload_type}: {', '.join(missing)}.")
        _check_schema(payload_type, replies[payload_type].get("schema"), fields)
    elif not payload_type.startswith("result."):
        raise ValueError('payload_type must start with "result.".')
    ack = await _publish(make_reply(original, _config.agent_uid, payload_type, to_sender_if_unrouted=bool(_config.level),
                                    **{**_echoed(original, spec), **fields}))
    _answered.add(chain_id)
    status.results_published += 1
    log_event("plugin:akinakes-channel", "result_published", chain_id=chain_id, payload_type=payload_type)
    return ack


def _refuse_second_answer(chain_id: str) -> None:
    """One reply per task. On the second real run a turn published its result, its closing reply then failed
    verification, and the retry -- not knowing -- reported the same task blocked as well."""
    if chain_id in _answered:
        raise ValueError(f'The task with chainId "{chain_id}" is already answered; nothing more is published for it.')


def _check_schema(payload_type: str, schema: dict[str, Any] | None, fields: dict[str, Any]) -> None:
    if not schema:
        return
    import jsonschema

    try:
        jsonschema.validate(fields, schema)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path) or "fields"
        raise ValueError(f"{payload_type}: {where}: {exc.message}. The fields must follow: "
                         f"{json.dumps(schema, ensure_ascii=False)}") from None


async def publish_blocked(chain_id: str, reason: str) -> dict[str, Any]:
    assert _config is not None
    original = _pending.get(chain_id)
    if original is None:
        raise ValueError(f'No task is in progress for chainId "{chain_id}".')
    _refuse_second_answer(chain_id)
    spec = reply_spec(pending_task_type(chain_id) or "")
    if spec.get("chat") and _config.orchestrator:
        # Nobody to pass a message to: chatSecretary is the one others pass to. The user is told instead.
        await post_chat(chain_id, reason)
        return {"messageId": None}
    if spec.get("chat"):
        return await publish_pass(chain_id)
    blocked = spec.get("blocked") or {}
    if blocked.get("payload_type"):
        reply = make_reply(original, _config.agent_uid, blocked["payload_type"], to_sender_if_unrouted=bool(_config.level),
                           **{**_echoed(original, spec), blocked.get("reason_field") or "reason": reason})
    else:
        routing = (original["envelope"].get("routing") or {})
        skill = ((original.get("payload") or {}).get("type") or "").removeprefix("task.")
        reply = make_message(
            sender_uid=_config.agent_uid,
            payload_type="system.skill_blocked",
            chain_id=chain_id,
            target_agent_uids=[routing.get("returnTo") or "chatGateway"],
            ttl_seconds=DEFAULT_TTL_S,
            reason=reason,
            skill=skill,
        )
    ack = await _publish(reply)
    _answered.add(chain_id)
    status.blocked_published += 1
    log_event("plugin:akinakes-channel", "blocked_published", chain_id=chain_id, reason=reason[:200])
    return ack


# --- chat rooms (a chat.message in progress) -------------------------------------------------------------------------

CHAT_SECRETARY_UID = "chatSecretary"
CHAT_HISTORY_LIMIT = 200


def _chat_message(chain_id: str) -> dict[str, Any]:
    original = _pending.get(chain_id)
    if original is None or not (is_chat(chain_id) or chain_id in _relay_keys):
        raise ValueError(f'No chat message is in progress for chainId "{chain_id}". Use the exact chainId from the message.')
    return original


async def post_chat(chain_id: str, text: str) -> None:
    """Answers the chat message of this chain in its room, as agent_lib's agents do (Chat Service's agent_message)."""
    assert _config is not None and _http is not None
    room_id = (_chat_message(chain_id).get("payload") or {}).get("room_id") or ""
    if not text.strip():
        raise ValueError("The message text is empty.")
    # The coordinator says one thing per request; a retry of the turn (the small model's verification) must not say it twice.
    if _config.orchestrator and chain_id in _posted:
        raise ValueError("This request already has your answer in the room.")
    resp = await _http.post(
        f"{_config.chat_url}/internal/rooms/{room_id}/agent_message",
        json={"sender_uid": _config.agent_uid, "sender_type": "agent", "msg_type": "agent_message", "payload": {"text": text}},
        headers={"x-internal-key": _config.chat_key},
        timeout=15,
    )
    resp.raise_for_status()
    _answered.add(chain_id)
    _posted.add(chain_id)
    status.results_published += 1
    log_event("plugin:akinakes-channel", "chat_posted", chain_id=chain_id, room_id=room_id, text_len=len(text))


async def chat_history(chain_id: str) -> list[dict[str, Any]]:
    """The room's messages, oldest first (Chat Service GET /rooms/{room_id}/messages, all pages)."""
    assert _config is not None and _http is not None
    room_id = (_chat_message(chain_id).get("payload") or {}).get("room_id") or ""
    messages: list[dict[str, Any]] = []
    after_seq = 0
    while True:
        resp = await _http.get(f"{_config.chat_url}/rooms/{room_id}/messages", headers={"X-User-Email": _config.agent_uid},
                               params={"after_seq": after_seq, "limit": CHAT_HISTORY_LIMIT}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        page = data if isinstance(data, list) else data.get("messages") or []
        messages += page
        if len(page) < CHAT_HISTORY_LIMIT:
            return messages
        after_seq = max(int(m.get("seq") or 0) for m in page)


async def publish_pass(chain_id: str) -> dict[str, Any]:
    """This chat message is not for this agent: agent.pass to chatSecretary (agent_lib/agent_pass.py)."""
    assert _config is not None
    room_id = (_chat_message(chain_id).get("payload") or {}).get("room_id") or ""
    message = make_message(_config.agent_uid, "agent.pass", chain_id=chain_id, ttl_seconds=60,
                           target_agent_uids=[CHAT_SECRETARY_UID], room_id=room_id, agent_uid=_config.agent_uid)
    ack = await _publish(message)
    _answered.add(chain_id)
    log_event("plugin:akinakes-channel", "passed", chain_id=chain_id, room_id=room_id)
    return ack


async def admits_chat_message(msg: dict[str, Any], config: ChannelConfig, client: httpx.AsyncClient) -> bool:
    """AgentLoop._intake(): stage 1 the agent's scope (skipped when the message is addressed to this agent), stage 2
    ACL Service (always; fail-open when it is unreachable). Also drops the agent's own messages and empty ones."""
    payload = msg.get("payload") or {}
    room_id, sender_uid = payload.get("room_id") or "", payload.get("sender_uid") or ""
    if not (payload.get("text") or "").strip() or not room_id or sender_uid == config.agent_uid:
        return False
    # chatSecretary: what agents say in a room is the answer to a request, not a request (Chat Service republishes
    # every agent message on the bus; treating each as a new human message started a cascade storm in the old agent).
    if config.orchestrator and payload.get("sender_type") == "agent":
        return False
    routing = (msg.get("envelope") or {}).get("routing") or {}
    if config.agent_uid not in (routing.get("targetAgentUids") or []) and config.scope and not match_scope(config.scope, room_id):
        log_event("plugin:akinakes-channel", "intake_scope_rejected", room_id=room_id)
        return False
    if sender_uid and config.acl_url:
        try:
            resp = await client.post(f"{config.acl_url}/check", json={
                "subject": sender_uid, "object_type": "agent", "object_id": config.agent_uid, "operation": "use",
            }, timeout=3)
            if not bool(resp.json().get("allowed", True)):
                log_event("plugin:akinakes-channel", "intake_acl_rejected", sender=sender_uid)
                return False
        except Exception as exc:  # noqa: BLE001 -- fail-open, as AgentLoop._acl_allows
            log_event("plugin:akinakes-channel", "intake_acl_unreachable", error=str(exc))
    return True


def match_scope(scope: list[dict[str, Any]], room_id: str) -> bool:
    """agent_lib/scope.py: "all", the room itself, or (recursive) a room under it."""
    for entry in scope:
        entry_room = entry.get("room_id", "")
        if entry_room in ("all", room_id) or (entry.get("recursive") and room_id.startswith(entry_room + "/")):
            return True
    return False


# --- chatSecretary (orchestrator) -------------------------------------------------------------------------------------
# It sees every human chat.message of every room. What happens to the message is the model's decision, made with the
# tools below: nothing here classifies, matches or routes by rule.

WAIT_REPLIES_MAX_S = 90
WAIT_POLL_S = 2.0


def _orchestrator_message(chain_id: str) -> dict[str, Any]:
    if _config is None or not _config.orchestrator:
        raise ValueError("This tool is for the chat coordinator only.")
    return _chat_message(chain_id)


async def _registry_agents() -> dict[str, dict[str, Any]]:
    assert _config is not None and _http is not None
    if not _config.registry_url:
        return {}
    try:
        reg = await _http.get(f"{_config.registry_url}/agents", timeout=10)
        reg.raise_for_status()
        return {a.get("uid"): a for a in reg.json()}
    except Exception as exc:  # noqa: BLE001 -- the caller still has what it read elsewhere
        log_event("plugin:akinakes-channel", "registry_unreadable", error=str(exc))
        return {}


def _describe_agent(uid: str, a: dict[str, Any]) -> dict[str, Any]:
    return {"uid": uid, "role": a.get("role"), "specialization": a.get("specialization"), "description": a.get("description"),
            "accepts": a.get("accepts") or []}


async def _room_agent_uids(room_id: str) -> list[str]:
    """The agent members of the room (Chat Service), this coordinator left out."""
    assert _config is not None and _http is not None
    resp = await _http.get(f"{_config.chat_url}/rooms/{room_id}/members", headers={"X-User-Email": _config.agent_uid}, timeout=10)
    resp.raise_for_status()
    return [m["uid"] for m in resp.json() if m.get("ptype") == "agent" and m.get("uid") != _config.agent_uid]


async def room_agents(chain_id: str) -> dict[str, Any]:
    """The room of this message (its name, type -- workspace, project, team, bureau -- and parent; a project's id is its
    room's id) and the agents in it (Chat Service members) with what the Registry says they do and the bus tasks they take."""
    assert _config is not None and _http is not None
    room_id = (_orchestrator_message(chain_id).get("payload") or {}).get("room_id") or ""
    headers = {"X-User-Email": _config.agent_uid}
    uids = await _room_agent_uids(room_id)
    room: dict[str, Any] = {"id": room_id}
    try:
        r = await _http.get(f"{_config.chat_url}/rooms/{room_id}", headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        room.update({"name": data.get("name"), "type": data.get("room_type"), "parent_id": data.get("parent_id")})
    except Exception as exc:  # noqa: BLE001 -- the member list alone is still useful
        log_event("plugin:akinakes-channel", "room_unreadable", room_id=room_id, error=str(exc))
    known = await _registry_agents()
    return {"room": room, "agents": [_describe_agent(uid, known.get(uid) or {}) for uid in uids]}


async def bus_agents(chain_id: str) -> list[dict[str, Any]]:
    """Every agent on the bus that declares tasks it takes (Registry `accepts`), in the room or not."""
    assert _config is not None
    _orchestrator_message(chain_id)
    known = await _registry_agents()
    return [_describe_agent(uid, a) for uid, a in known.items() if uid != _config.agent_uid and a.get("accepts")]


async def hand_to_agents(chain_id: str, agent_uids: list[str], text: str | None) -> list[str]:
    """Publishes the human's message to each agent, addressed to it (what _forward_to_agent did). The agents answer in the
    room themselves, or pass."""
    assert _config is not None
    original = _orchestrator_message(chain_id)
    payload = original.get("payload") or {}
    if not agent_uids:
        raise ValueError("agent_uids is empty.")
    # Once per agent: the model may repeat the call (a retried turn) and an agent must not get the same message twice.
    agent_uids = [u for u in dict.fromkeys(agent_uids) if u not in _asked.get(chain_id, set())]
    if not agent_uids:
        raise ValueError("The message is already handed to these agents; they answer in the room themselves.")
    # Only agents of this room (2026-11-01, live server: the model called it with invented uids agent-01..03 before
    # ever reading the room, the channel published to them, and nobody got the request). The whole call is refused,
    # nothing is sent, and the reply names the real agents, so the next call can be right.
    try:
        members = await _room_agent_uids(payload.get("room_id") or "")
    except Exception as exc:  # noqa: BLE001 -- without the member list nothing can be checked, so nothing is sent
        raise ValueError(f"Could not read the room's members, nothing was sent: {exc}") from None
    strangers = [u for u in agent_uids if u not in members]
    if strangers:
        raise ValueError(f"Not agents of this room, nothing was sent: {', '.join(strangers)}. The room's agents are: "
                         f"{', '.join(members) or 'none'} (room_agents tells what each does).")
    sent: list[str] = []
    for uid in agent_uids:
        msg = make_message(_config.agent_uid, "chat.message", chain_id=chain_id, ttl_seconds=300, target_agent_uids=[uid])
        msg["payload"].update({"room_id": payload.get("room_id"), "sender_uid": payload.get("sender_uid"),
                               "text": text or payload.get("text", ""), "seq": payload.get("seq", 0)})
        await _publish(msg)
        sent.append(uid)
    _asked.setdefault(chain_id, set()).update(sent)
    _answered.add(chain_id)
    log_event("plugin:akinakes-channel", "handed_to_agents", chain_id=chain_id, agents=sent)
    return sent


LEVEL_REQUEST = "task.level_request"
# How long the coordinator waits for some agent to claim a task it broadcast before it tells the room nobody took it.
CLAIM_WAIT_S = 120.0
NOT_TAKEN = "system.not_taken"


async def publish_bus_task(chain_id: str, agent_uid: str, task_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    """A task for the agent the model chose (bus_agents / room_agents show who takes what), sent with route=[agent] and
    returnTo=this agent, so the result comes back here to be told in the room.

    With no agent_uid the task is broadcast (owner, 2026-11-05: "it is a bus, it was designed for broadcast; addressed
    sending is a special case"): no route and no returnTo, every agent sees it and the one whose work it is claims it
    (the level secretaries: event.level_claimed) and answers to this coordinator by name. Nobody claims it within
    CLAIM_WAIT_S: the room is told."""
    assert _config is not None
    original = _orchestrator_message(chain_id)
    if not task_type.startswith(("task.", "query.")):
        raise ValueError('task_type must start with "task." or "query.".')
    if agent_uid == _config.agent_uid:
        raise ValueError("to_agent is the agent that takes this task, not you; leave it out to send the task to everyone.")
    if not agent_uid:
        return await _broadcast_bus_task(chain_id, original, task_type, fields)
    known = await _registry_agents()
    if known:
        accepted = {a.get("type") for a in (known.get(agent_uid) or {}).get("accepts") or []}
        if agent_uid not in known:
            raise ValueError(f'No agent "{agent_uid}" is registered. bus_agents lists the agents and their tasks.')
        if task_type not in accepted:
            raise ValueError(f'{agent_uid} does not take {task_type}; it takes: {", ".join(sorted(accepted)) or "nothing declared"}.')
    payload = original.get("payload") or {}
    room_id = payload.get("room_id") or ""
    # The message's room is the default; a task whose own fields name a room (docCreator's room_id: the room to
    # announce in) keeps the model's choice. Who asked and their words always come from the message. (2026-11-02,
    # live: a task with room_id among its fields crashed with "multiple values for keyword argument 'room_id'".)
    task_fields = {"room_id": room_id, **fields, "user_uid": payload.get("sender_uid"), "original_text": payload.get("text", "")}
    msg = make_message(_config.agent_uid, task_type, chain_id=chain_id, route=[agent_uid], return_to=_config.agent_uid,
                       **task_fields)
    ack = await _publish(msg)
    _answered.add(chain_id)
    _bus_tasks[chain_id] = {"room_id": room_id, "asked_by": payload.get("sender_uid") or "", "asked": payload.get("text") or ""}
    log_event("plugin:akinakes-channel", "bus_task_published", chain_id=chain_id, task_type=task_type, agent=agent_uid)
    return ack


async def _broadcast_bus_task(chain_id: str, original: dict[str, Any], task_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    assert _config is not None
    if chain_id in _bus_tasks:
        raise ValueError("A task for this request is already on the bus; its answer comes back to you.")
    known = await _registry_agents()
    if known and not any(task_type in {t.get("type") for t in a.get("accepts") or []}
                         for uid, a in known.items() if uid != _config.agent_uid):
        raise ValueError(f"No agent on the bus takes {task_type}. bus_agents lists the tasks agents take.")
    payload = original.get("payload") or {}
    room_id = payload.get("room_id") or ""
    # The room is where the person asked: the agent that claims the request finds its own place for it.
    task_fields = {**fields, "room_id": room_id, "asked_by": payload.get("sender_uid"),
                   "user_uid": payload.get("sender_uid"), "original_text": payload.get("text", "")}
    task_fields.setdefault("text", payload.get("text", ""))
    ack = await _publish(make_message(_config.agent_uid, task_type, chain_id=chain_id, **task_fields))
    _answered.add(chain_id)
    _bus_tasks[chain_id] = {"room_id": room_id, "asked_by": payload.get("sender_uid") or "", "asked": payload.get("text") or ""}
    _claimed.discard(chain_id)
    log_event("plugin:akinakes-channel", "bus_task_broadcast", chain_id=chain_id, task_type=task_type)
    task = asyncio.get_running_loop().create_task(_claim_deadline(chain_id))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return ack


async def _claim_deadline(chain_id: str) -> None:
    """Nobody claimed a broadcast task: the room hears it through a relay turn, like any answer."""
    await asyncio.sleep(CLAIM_WAIT_S)
    if chain_id in _claimed or chain_id not in _bus_tasks or _inject is None:
        return
    asked = _bus_tasks.pop(chain_id)
    log_event("plugin:akinakes-channel", "bus_task_not_taken", chain_id=chain_id)
    msg = make_message("agentBus", NOT_TAKEN, chain_id=chain_id, room_id=asked["room_id"],
                       reason="No agent on the bus took this request within two minutes: none of them found it to be its work.")
    _start_relay(msg, asked, _inject)


async def wait_for_agent_replies(chain_id: str, seconds: float) -> dict[str, Any]:
    """Waits (up to WAIT_REPLIES_MAX_S) until every agent handed the message has posted in the room or passed."""
    assert _config is not None and _http is not None
    original = _orchestrator_message(chain_id)
    payload = original.get("payload") or {}
    room_id, seq = payload.get("room_id") or "", int(payload.get("seq") or 0)
    asked = set(_asked.get(chain_id) or [])
    deadline = time.monotonic() + max(1.0, min(float(seconds), WAIT_REPLIES_MAX_S))
    while True:
        resp = await _http.get(f"{_config.chat_url}/rooms/{room_id}/messages", headers={"X-User-Email": _config.agent_uid},
                               params={"after_seq": seq, "limit": CHAT_HISTORY_LIMIT}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        page = data if isinstance(data, list) else data.get("messages") or []
        replies = [m for m in page if m.get("sender_type") == "agent" and m.get("sender_uid") != _config.agent_uid
                   and (not asked or m.get("sender_uid") in asked)]
        replied = {m.get("sender_uid") for m in replies}
        passed = set(_passes.get(chain_id) or [])
        silent = asked - replied - passed
        if (asked and not silent) or (not asked and replies) or time.monotonic() >= deadline:
            return {"replies": replies, "passed": sorted(passed), "silent": sorted(silent)}
        await asyncio.sleep(WAIT_POLL_S)


# The coordinator's job, in the words its checks read (2026-11-05). Its chat and relay turns run without the formalizer
# (which took the person's request for work to do and had the person's own words posted back), so sw.action.scope had
# only "set the objective of this project" and refused every chat_post_message as not part of the task.
ROOM_SECRETARY_TASK = ("Your task as the room's secretary: answer the person in the room, or pass the request on to the "
                       "agents that do the work and tell the room what they answered.")


def relay_words(msg: dict[str, Any], asked: dict[str, str]) -> str:
    """A relay turn's requester words: what the person asked (when this coordinator published the task) and what the
    agent answered, without our instructions around them. The formalizer and the checks read these (2026-11-02, live:
    with none, the relay template itself was formalized -- "Content: [Agent Bus result. chainId=...]" -- and the
    room was told "An agent finished the work for project_id ...")."""
    env = msg.get("envelope") or {}
    payload = msg.get("payload") or {}
    fields = {k: v for k, v in payload.items() if k not in ("type", "room_id")}
    answer = (f'{env.get("senderUid") or "an agent"} answered {payload.get("type", "")} over the Agent Bus, with:\n'
              f"{json.dumps(fields, ensure_ascii=False, indent=2)}")
    if not asked.get("asked"):
        return f"{answer}\n\n{ROOM_SECRETARY_TASK}"
    return f'{asked.get("asked_by") or "someone"} asked in the chat room:\n{asked["asked"]}\n\n{answer}\n\n{ROOM_SECRETARY_TASK}'


def render_relay_text(msg: dict[str, Any], key: str) -> str:
    payload = msg.get("payload") or {}
    return (
        # No chainId, type or room id in the header (2026-11-05, live runs 4 and 6): the model copied them into the room
        # post ("Update from the Agent Bus ... Reference task id ... Room: ..."). The tools know this turn without them.
        f"[An agent has finished work that came from this room. What it answered:\n"
        f"{json.dumps({k: v for k, v in payload.items() if k not in ('type', 'room_id')}, ensure_ascii=False, indent=2)}\n\n"
        "Look at chat_history first: if you already told the room this (an earlier message of yours says the same), "
        "post nothing; if you said it was in progress or was not yet known, post what is new. Otherwise tell the room "
        "what happened, in the language of the conversation and in plain words, as one person tells another: what was "
        "done, with the title the people can use to find it. Say only what the answer above says. Never show ids, "
        "chain or task names, \"Agent Bus\" or other internal words. Post one message with chat_post_message. If the "
        "result is an error, say what failed and what the person can do. Nobody reads this conversation, only what "
        "you post in the room.]"
    )


NUDGE_TEXT = (
    "Nothing you wrote reached the room: only chat_post_message, hand_to_agents and publish_bus_task do anything, and "
    "plain text at the end of a turn is thrown away. You do not do the person's work yourself. Act now: answer in the "
    "room if you can answer yourself, or pass the request on to the agent that does that work (hand_to_agents or "
    "publish_bus_task), using what room_agents and the task list show."
)


async def on_turn_end(chain_id: str) -> None:
    """Call when a session's turn ended. Nothing published for the chain -> the task's blocked reply (agent.pass for a
    chat message)."""
    if chain_id in _pending and chain_id not in _answered and chain_id not in _relay_keys:
        try:
            if _config is not None and _config.orchestrator and _inject is not None and chain_id not in _nudged:
                # The small model sometimes does the requested work itself and ends the turn in plain text, which nobody
                # reads (a plan written in the log, nothing in the room). One more turn says so; the chain stays open.
                _nudged.add(chain_id)
                log_event("plugin:akinakes-channel", "coordinator_nudged", chain_id=chain_id)
                _inject(chain_id, NUDGE_TEXT, requester_words(_pending[chain_id]), True)
                return
            if _config is not None and _config.orchestrator:
                await post_chat(chain_id, "I could not handle that request. Could you say it another way?")
            else:
                await publish_blocked(chain_id, "The agent's turn ended without publishing a result.")
        except Exception as exc:  # noqa: BLE001
            status.last_error = str(exc)
            log_event("plugin:akinakes-channel", "blocked_publish_failed", chain_id=chain_id, error=str(exc))
    _pending.pop(chain_id, None)
    _answered.discard(chain_id)
    _relay_keys.discard(chain_id)
    _posted.discard(chain_id)
    _nudged.discard(chain_id)
    _asked.pop(chain_id, None)
    _passes.pop(chain_id, None)
    _level.pop(chain_id, None)
    _subtasks.pop(chain_id, None)
    _subtask_results.pop(chain_id, None)


# --- level secretary (project, bureau) --------------------------------------------------------------------------------
# akinakes plan, group 3, design F with the owner's decisions of 2026-11-05: one secretary per kind of level, for all its
# rooms; requests reach it broadcast; the akinakes.level.ownership microagent decides whether a request is its own and
# of which project or bureau; the turn then records it and/or distributes it among the level's members, and answers the
# sender once. Nothing here reads the request's text: the microagent and the turn's model decide.

LEVEL_CLAIMED = "event.level_claimed"
WAIT_LEVEL_MAX_S = 600


def _level_intake(msg: dict[str, Any], msg_type: str, chain_id: str, config: ChannelConfig, inject: InjectFn,
                  before_task: BeforeTaskFn | None) -> bool:
    """True when the message is dealt with here; False sends it down the ordinary role path (an addressed
    task.project_* / task.bureau_* is answered there, with its reply spec)."""
    if msg_type.startswith("result.") or msg_type == "system.skill_blocked":
        if chain_id in _subtasks:
            payload = msg.get("payload") or {}
            _subtask_results.setdefault(chain_id, []).append({
                "from": (msg.get("envelope") or {}).get("senderUid"), "type": msg_type,
                "fields": {k: v for k, v in payload.items() if k != "type"}})
            log_event("plugin:akinakes-channel", "level_subtask_result", chain_id=chain_id, payload_type=msg_type)
        return True
    if msg_type != LEVEL_REQUEST:
        return False
    if chain_id in _pending:
        return True
    task = asyncio.get_running_loop().create_task(
        _consider_level_request(msg, config, inject, before_task, addressed_to(msg, config.agent_uid)))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return True


def _describe_member(member: dict[str, Any], known: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if member.get("ptype") == "agent":
        a = known.get(member.get("uid") or "") or {}
        return {"uid": member.get("uid"), "kind": "agent", "name": member.get("display_name"), "role": a.get("role"),
                "description": a.get("description"), "accepts": [t.get("type") for t in a.get("accepts") or []]}
    return {"uid": member.get("uid"), "kind": "person", "name": member.get("display_name")}


async def _room_members(room_id: str) -> list[dict[str, Any]]:
    assert _config is not None and _http is not None
    resp = await _http.get(f"{_config.chat_url}/rooms/{room_id}/members", headers={"X-User-Email": _config.agent_uid}, timeout=10)
    resp.raise_for_status()
    return [m for m in resp.json() if m.get("uid") != _config.agent_uid]


async def _rooms_view(level: str, origin_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The rooms of this level with their members (Chat Service), and the room the request came from."""
    assert _config is not None and _http is not None
    resp = await _http.get(f"{_config.chat_url}/rooms", headers={"X-User-Email": _config.agent_uid},
                           params={"all": "1"}, timeout=15)
    resp.raise_for_status()
    every = {r.get("id"): r for r in resp.json()}

    def brief(r: dict[str, Any]) -> dict[str, Any]:
        parent = every.get(r.get("parent_id")) or {}
        return {"id": r.get("id"), "name": r.get("name"), "type": r.get("room_type"), "parent_id": r.get("parent_id"),
                "parent_name": parent.get("name")}

    mine = [brief(r) for r in every.values() if r.get("room_type") == level]
    known = await _registry_agents()

    async def members(room_id: str) -> list[dict[str, Any]]:
        try:
            return [_describe_member(m, known) for m in await _room_members(room_id)]
        except Exception as exc:  # noqa: BLE001 -- a room without its member list is still a room
            log_event("plugin:akinakes-channel", "level_members_unreadable", room_id=room_id, error=str(exc))
            return []

    for room, mems in zip(mine, await asyncio.gather(*(members(r["id"]) for r in mine))):
        room["members"] = mems
    origin = brief(every[origin_id]) if origin_id in every else {"id": origin_id}
    return mine, origin


async def _decide_ownership(level: str, text: str, origin: dict[str, Any], rooms: list[dict[str, Any]]) -> dict[str, Any] | None:
    from app.microagents.akinakes_level_ownership import LEVEL_OWNERSHIP
    from app.plugins.voice_api import _run_local_microagent

    try:
        result = await _run_local_microagent(LEVEL_OWNERSHIP, "app.microagents.akinakes_level_ownership",
                                             {"level": level, "request": text, "origin": origin, "rooms": rooms})
    except Exception as exc:  # noqa: BLE001 -- undecided: not claimed, the coordinator's deadline tells the room
        log_event("plugin:akinakes-channel", "level_ownership_failed", error=str(exc)[:200])
        return None
    output = (result or {}).get("output") if (result or {}).get("status") == "ok" else None
    if not isinstance(output, dict) or not isinstance(output.get("mine"), bool):
        log_event("plugin:akinakes-channel", "level_ownership_undecided", error=(result or {}).get("error"))
        return None
    return output


async def _consider_level_request(msg: dict[str, Any], config: ChannelConfig, inject: InjectFn,
                                  before_task: BeforeTaskFn | None, addressed: bool) -> None:
    env = msg.get("envelope") or {}
    chain_id = env.get("chainId") or ""
    payload = msg.get("payload") or {}
    text = str(payload.get("text") or payload.get("original_text") or "")
    try:
        rooms, origin = await _rooms_view(config.level, str(payload.get("room_id") or ""))
    except Exception as exc:  # noqa: BLE001
        status.last_error = str(exc)
        log_event("plugin:akinakes-channel", "level_rooms_unreadable", chain_id=chain_id, error=str(exc))
        return
    decision = await _decide_ownership(config.level, text, origin, rooms) or {}
    why = str(decision.get("why") or "")
    room = next((r for r in rooms if r["id"] == decision.get("room_id")), None) if decision.get("mine") else None
    log_event("plugin:akinakes-channel", "level_ownership", chain_id=chain_id, mine=room is not None,
              room_id=(room or {}).get("id"), why=why[:200], addressed=addressed)
    if room is None:
        if not addressed:
            return
        room = next((r for r in rooms if r["id"] == origin.get("id")), {})
    if chain_id in _pending:
        return
    try:
        await _publish(make_message(config.agent_uid, LEVEL_CLAIMED, chain_id=chain_id, ttl_seconds=300,
                                    target_agent_uids=[env.get("senderUid") or CHAT_SECRETARY_UID],
                                    room_id=payload.get("room_id"), level=config.level, level_room_id=room.get("id")))
    except Exception as exc:  # noqa: BLE001 -- the work goes on; the coordinator may tell the room nobody took it
        log_event("plugin:akinakes-channel", "level_claim_failed", chain_id=chain_id, error=str(exc))
    _pending[chain_id] = msg
    _answered.discard(chain_id)
    _level[chain_id] = {"room": room, "origin": origin, "why": why}
    if before_task is not None:
        try:
            await before_task()
        except Exception as exc:  # noqa: BLE001
            log_event("plugin:akinakes-channel", "before_task_failed", error=str(exc))
    log_event("plugin:akinakes-channel", "task_received", chain_id=chain_id, task_type=LEVEL_REQUEST)
    inject(chain_id, render_level_text(msg, config.level, room, origin, why), requester_words(msg))


def _level_state(chain_id: str) -> dict[str, Any]:
    if _config is None or not _config.level:
        raise ValueError("This tool is for a project or bureau secretary only.")
    state = _level.get(chain_id)
    if state is None:
        raise ValueError(f'No request of your {_config.level} is in progress for chainId "{chain_id}".')
    return state


async def level_room(chain_id: str) -> dict[str, Any]:
    """The level's room this request belongs to, its members now (people; agents with role, what they do and the bus
    tasks they take) and where the request was asked."""
    state = _level_state(chain_id)
    room = state["room"]
    if not room.get("id"):
        return {"room": None, "asked_in": state["origin"], "note": f"No {_config.level if _config else 'level'} is known for this request."}
    known = await _registry_agents()
    members = [_describe_member(m, known) for m in await _room_members(room["id"])]
    return {"room": {k: room.get(k) for k in ("id", "name", "type", "parent_id", "parent_name")}, "members": members,
            "asked_in": state["origin"]}


async def level_post(chain_id: str, text: str) -> int:
    """Posts in the level's room, as this secretary. Returns the message's seq."""
    assert _config is not None and _http is not None
    room_id = (_level_state(chain_id)["room"] or {}).get("id")
    if not room_id:
        raise ValueError("This request has no room of your level to post in.")
    if not text.strip():
        raise ValueError("The message text is empty.")
    resp = await _http.post(
        f"{_config.chat_url}/internal/rooms/{room_id}/agent_message",
        json={"sender_uid": _config.agent_uid, "sender_type": "agent", "msg_type": "agent_message", "payload": {"text": text}},
        headers={"x-internal-key": _config.chat_key}, timeout=15)
    resp.raise_for_status()
    data = resp.json() if resp.content else {}
    seq = int((data or {}).get("seq") or 0) if isinstance(data, dict) else 0
    _level[chain_id]["posted_seq"] = seq
    log_event("plugin:akinakes-channel", "level_posted", chain_id=chain_id, room_id=room_id, text_len=len(text))
    return seq


async def level_publish_subtask(chain_id: str, agent_uid: str, task_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    """A part of the work for an agent member of the level's room, addressed to it; its result comes back here."""
    assert _config is not None
    state = _level_state(chain_id)
    room_id = (state["room"] or {}).get("id")
    if not room_id:
        raise ValueError("This request has no room of your level, so it has no members to give work to.")
    if not task_type.startswith(("task.", "query.")):
        raise ValueError('task_type must start with "task." or "query.".')
    members = [m.get("uid") for m in await _room_members(room_id) if m.get("ptype") == "agent"]
    if agent_uid not in members:
        raise ValueError(f'"{agent_uid}" is not an agent of this room, nothing was sent. Its agents are: {", ".join(members) or "none"}.')
    known = await _registry_agents()
    accepted = {t.get("type") for t in (known.get(agent_uid) or {}).get("accepts") or []}
    if known and task_type not in accepted:
        raise ValueError(f'{agent_uid} does not take {task_type}; it takes: {", ".join(sorted(accepted)) or "nothing declared"}.')
    payload = (_pending.get(chain_id) or {}).get("payload") or {}
    task_fields = {"room_id": room_id, **fields, "user_uid": payload.get("asked_by") or payload.get("user_uid"),
                   "original_text": payload.get("text") or payload.get("original_text") or ""}
    ack = await _publish(make_message(_config.agent_uid, task_type, chain_id=chain_id, route=[agent_uid],
                                      return_to=_config.agent_uid, **task_fields))
    _subtasks.setdefault(chain_id, set()).add(agent_uid)
    log_event("plugin:akinakes-channel", "level_subtask_published", chain_id=chain_id, task_type=task_type, agent=agent_uid)
    return ack


async def wait_for_level_results(chain_id: str, seconds: float) -> dict[str, Any]:
    """Waits (up to WAIT_LEVEL_MAX_S) until every agent given a subtask has returned its result, then returns the
    results, who is still working, and what was said in the level's room since this secretary posted there."""
    assert _config is not None and _http is not None
    state = _level_state(chain_id)
    deadline = time.monotonic() + max(1.0, min(float(seconds), WAIT_LEVEL_MAX_S))
    while True:
        results = list(_subtask_results.get(chain_id) or [])
        waiting = sorted((_subtasks.get(chain_id) or set()) - {r.get("from") for r in results})
        if not waiting or time.monotonic() >= deadline:
            break
        await asyncio.sleep(WAIT_POLL_S)
    replies: list[dict[str, Any]] = []
    room_id = (state["room"] or {}).get("id")
    if room_id and "posted_seq" in state:
        try:
            resp = await _http.get(f"{_config.chat_url}/rooms/{room_id}/messages", headers={"X-User-Email": _config.agent_uid},
                                   params={"after_seq": state["posted_seq"], "limit": CHAT_HISTORY_LIMIT}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            page = data if isinstance(data, list) else data.get("messages") or []
            replies = [{"from": m.get("sender_uid"), "text": (m.get("payload") or {}).get("text") or m.get("text")}
                       for m in page if m.get("sender_uid") != _config.agent_uid]
        except Exception as exc:  # noqa: BLE001 -- the bus results are still returned
            log_event("plugin:akinakes-channel", "level_room_unreadable", room_id=room_id, error=str(exc))
    return {"results": results, "still_working": waiting, "room_replies": replies}


def render_level_text(msg: dict[str, Any], level: str, room: dict[str, Any], origin: dict[str, Any], why: str) -> str:
    env = msg.get("envelope") or {}
    payload = msg.get("payload") or {}
    text = payload.get("text") or payload.get("original_text") or ""
    asked_by = payload.get("asked_by") or payload.get("user_uid") or env.get("senderUid") or "someone"
    where = f'"{origin.get("name") or origin.get("id") or "unknown"}" (a {origin.get("type") or "room"})'
    if room.get("id"):
        whose = (f'You found that it belongs to the {level} "{room.get("name")}" (its id, also its room\'s id: {room["id"]})'
                 + (f": {why}" if why else ".") + "\n")
    else:
        whose = f"It was sent to you by name, but no {level} of yours is known for it.\n"
    if level == "project":
        records = ("project_state_get reads the project's state; project_set_objective sets the objective (replacing "
                   "the earlier one), project_add_objective adds one more; project_add_decision records a decision; "
                   "project_assign_task records who does which task")
    else:
        records = ("bureau_capture_decision records a decision of the bureau; bureau_add_summary stores a summary of its "
                   "discussion; bureau_list_decisions reads the decisions recorded")
    return (
        f'[Agent Bus request for the {level} secretary. chainId="{env.get("chainId", "")}".\n'
        f"{asked_by} asked in the room {where}:\n<<<\n{text}\n>>>\n\n"
        f"{whose}\n"
        f"You are the secretary of this {level}. Do what the request needs; your tools know which request this is.\n"
        f"- Something to record for the {level}: {records}. A {level}'s id is its room's id.\n"
        f"- Work for the {level}'s members: level_room shows the members (people, and agents with the bus tasks they "
        f"take); level_post puts the request, or the part for the people, in the {level}'s room; "
        "level_publish_subtask gives an agent member its part as one of the tasks it takes, in your own words; "
        "wait_for_level_results collects what they return and what was said in the room.\n"
        'Finish with akinakes_publish_result, payload_type "result.level_done", fields {"summary": what was done, in '
        "plain words and in the language of the request, with the titles people can use to find what was made}. If "
        "it cannot be done, call akinakes_report_blocked with the reason. Never say something was recorded, posted or "
        "done unless one of your tools did it. Nobody reads this conversation; the published result goes back to the "
        "room where the request was asked.]"
    )


# --- inbound ---------------------------------------------------------------------------------------------------------

def requester_words(msg: dict[str, Any]) -> str:
    """What the sender of this message asked, without our instructions around it: the formalizer and every check of the
    turn read this, the model gets render_task_text (2026-11-02: the whole template was formalized, and "hand the task
    to the room agents" from our own instructions counted as the person's request). A chat message is the person's
    text; a bus task is its type and fields."""
    env = msg.get("envelope") or {}
    payload = msg.get("payload") or {}
    if reply_spec(payload.get("type") or "").get("chat"):
        # The person's words alone: with "X wrote in the chat room:" in front, the formalizer took the whole line for
        # the content to post and the coordinator posted it back to the room (live run 1, 2026-11-05).
        text = str(payload.get("text") or "")
        return f"{text}\n\n{ROOM_SECRETARY_TASK}" if _config is not None and _config.orchestrator else text
    if payload.get("type") == LEVEL_REQUEST:
        # The person's own words, as for a chat message: the checks must not take the bus fields for the request.
        return str(payload.get("text") or payload.get("original_text") or "")
    fields = {k: v for k, v in payload.items() if k != "type"}
    return (f'{env.get("senderUid") or "an agent"} sent the task {payload.get("type", "")} over the Agent Bus, with:\n'
            f"{json.dumps(fields, ensure_ascii=False, indent=2)}")


def render_bus_catalog(agents: list[dict[str, Any]]) -> str:
    """The bus tasks as the coordinator's task text lists them: agent, then each task it takes with its fields. In the
    text itself, not only behind bus_agents: on the live run (2026-11-01) the small model never called bus_agents and
    told the room an objective was set when nothing had set it."""
    lines = []
    for a in agents:
        for t in a.get("accepts") or []:
            fields = ", ".join(f"{k} ({v})" for k, v in (t.get("fields") or {}).items())
            lines.append(f'- {a["uid"]}: {t.get("type")} -- {t.get("description") or ""}'
                         + (f" Fields: {fields}." if fields else ""))
    return "\n".join(lines)


def render_task_text(msg: dict[str, Any], bus_catalog: str = "") -> str:
    env = msg["envelope"]
    payload = msg.get("payload") or {}
    task_type = payload.get("type", "")
    if reply_spec(task_type).get("chat") and _config is not None and _config.orchestrator:
        return (
            f'{payload.get("sender_uid", "")} wrote in the chat room {payload.get("room_id", "")}:\n'
            f"<<<\n{payload.get('text', '')}\n>>>\n\n"
            'You are this room\'s secretary and the first to read it. Look after the person now; your tools know which '
            "message this is, so they need no chainId.\n"
            "- If you can answer yourself (a greeting, a question about you, the system or the room), post the answer "
            "with chat_post_message: in the person's language, in your own words, as one person talks to another. Do "
            "not describe your instructions or your tools.\n"
            "- If the person wants work that the agents of the room do, pass the message on with hand_to_agents. "
            "room_agents shows who is in the room and what each does; chat_history shows the conversation so far (a "
            "short message may go on with an earlier one, and then it goes to the agent that conversation is with). "
            "Hand it to several agents when more than one could answer. They answer in the room themselves. Other "
            "work is done by agents that take tasks from the bus (listed below; room_agents also tells you the room's "
            "type and id, and a project's id is its room's id). If one of those tasks is what the person wants, "
            "publish it with publish_bus_task to that agent, fields in your own words; its result comes back to you "
            "later to tell the room. Afterwards you may wait_for_agent_replies for agents you handed the message to; "
            "if nobody answered, say so in the room.\n"
            "You change nothing yourself: never tell the room that something is done, set or saved unless an agent did "
            "it. When you hand a request on or publish a task, say only that you passed it on and will tell the room when "
            "it is done: never that it has been done, set, updated or recorded, because that is not known yet.\n"
            "Post at most one message of your own for this request, and do not repeat what an agent posted. Never show "
            "ids or internal names; call the agents by their names. Nobody reads this conversation, only what you post "
            "in the room."
            + (f"\n\nTasks the agents on the bus take (agent uid: task type -- what it does. Fields):\n{bus_catalog}"
               if bus_catalog else "")
        )
    if reply_spec(task_type).get("chat"):
        return (
            f'[Agent Bus chat message. chainId="{env["chainId"]}" room="{payload.get("room_id", "")}" '
            f'from="{payload.get("sender_uid", "")}".\n'
            f"Text:\n{payload.get('text', '')}\n\n"
            "Every agent of the room receives it. If it is for you -- addressed to you, or what your role does -- "
            "do it with your tools (chat_history shows the conversation so far) and answer in the room with "
            "chat_post_message. If it is not for you, call akinakes_pass and do nothing else. Nobody reads this conversation, only what you post in the room.]"
        )
    return (
        f'[Agent Bus task. chainId="{env["chainId"]}" type="{task_type}" from="{env.get("senderUid", "")}".\n'
        f"Payload:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f'"{task_type}" is the kind of task, not a tool: there is no tool by that name. '
        "Do the task with your tools. Finish by calling akinakes_publish_result with "
        f"{expected_reply_text(task_type)}. If you cannot do it, call akinakes_report_blocked with the reason. Something the task needs to exist already -- the document, a run, a section to read or to change -- "
        "that Document Service reports as not found is such a case: report it blocked at once, do not look for it "
        "anywhere else. A section the task asks you to write may not exist yet; that is no reason to stop. Nobody reads "
        "this conversation, only the published result is delivered.]"
    )


async def _register(client: httpx.AsyncClient, config: ChannelConfig) -> None:
    global _agent_token
    if not config.registry_url:
        return
    try:
        accepts = accepted_messages(config)
        r = await client.post(f"{config.registry_url}/agents", json={
            "uid": config.agent_uid, "role": config.role, "specialization": config.specialization,
            "scope": "workspace", "deployment_mode": "unmanaged", "description": config.description,
            "personal_office_member": False, "accepts": accepts,
        }, timeout=5)
        if r.status_code == 201:
            _agent_token = r.json().get("token", "") or ""
            log_event("plugin:akinakes-channel", "registered", uid=config.agent_uid)
        elif r.status_code == 409:
            # Registered before (the old agent of this uid, or an earlier start): keep the declared tasks current.
            await client.patch(f"{config.registry_url}/agents/{config.agent_uid}", json={"accepts": accepts}, timeout=5)
    except Exception as exc:  # noqa: BLE001 -- non-fatal, same as AgentLoop._auto_register
        log_event("plugin:akinakes-channel", "register_failed", error=str(exc))


async def _heartbeat_forever(client: httpx.AsyncClient, config: ChannelConfig) -> None:
    await _register(client, config)
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        if not (config.registry_url and _agent_token):
            continue
        try:
            await client.post(f"{config.registry_url}/agents/{config.agent_uid}/heartbeat", params={"token": _agent_token}, timeout=5)
        except Exception as exc:  # noqa: BLE001
            log_event("plugin:akinakes-channel", "heartbeat_failed", error=str(exc))


def _start_relay(msg: dict[str, Any], asked: dict[str, str], inject: InjectFn) -> None:
    """A relay turn: the coordinator tells the room what an agent answered (or that nobody took the request)."""
    key = f'{(msg.get("envelope") or {}).get("chainId", "")}-relay-{(msg.get("envelope") or {}).get("messageId", "")[:8]}'
    _pending[key] = msg
    _relay_keys.add(key)
    inject(key, render_relay_text(msg, key), relay_words(msg, asked), True)


async def _poll_forever(client: httpx.AsyncClient, config: ChannelConfig, inject: InjectFn, before_task: BeforeTaskFn | None) -> None:
    agent = make_agent(config)
    while True:
        try:
            resp = await client.post(
                f"{config.bus_url}/messages/poll",
                json={"agent": agent, "limit": POLL_LIMIT, "waitSeconds": LONG_POLL_S},
                timeout=LONG_POLL_S + 35,
            )
            resp.raise_for_status()
            status.last_poll_at_iso = datetime.now(timezone.utc).isoformat()
            for item in resp.json().get("messages", []):
                msg = item.get("message") or {}
                chain_id = (msg.get("envelope") or {}).get("chainId")
                if not chain_id:
                    continue
                status.tasks_received += 1
                status.last_task_at_iso = datetime.now(timezone.utc).isoformat()
                msg_type = (msg.get("payload") or {}).get("type") or ""
                # The bus delivers a broadcast to its sender too: what this agent published is not work for it.
                if (msg.get("envelope") or {}).get("senderUid") == config.agent_uid:
                    continue
                if config.level and _level_intake(msg, msg_type, chain_id, config, inject, before_task):
                    continue
                # A role takes tasks, queries and chat messages. Results and events broadcast on the bus are not work for it
                # (without this, a writer answered another agent's result.section_draft with a second one).
                if not config.orchestrator and not msg_type.startswith(("task.", "query.", "chat.")):
                    continue
                # A task sent to everyone is taken only by a role that declares its type (a check of the type, not a
                # reading of the text); one addressed to this role is always its own.
                if (not config.orchestrator and config.accepts and msg_type.startswith(("task.", "query."))
                        and msg_type not in config.accepts and not addressed_to(msg, config.agent_uid)):
                    continue
                if config.orchestrator and msg_type == "agent.pass":
                    _passes.setdefault(chain_id, set()).add((msg.get("payload") or {}).get("agent_uid") or "")
                    continue
                if config.orchestrator and msg_type == LEVEL_CLAIMED:
                    _claimed.add(chain_id)
                    log_event("plugin:akinakes-channel", "bus_task_claimed", chain_id=chain_id,
                              by=(msg.get("envelope") or {}).get("senderUid"))
                    continue
                # The other events on the bus (Document Service announces every section written) are not requests either:
                # the coordinator took one for a task and tried to answer it (live 2026-11-05, once the events arrived).
                if msg_type.startswith("event."):
                    continue
                relay = config.orchestrator and msg_type.startswith("result.") and bool((msg.get("payload") or {}).get("room_id"))
                if config.orchestrator and chain_id in _bus_tasks and (msg_type.startswith("result.") or msg_type == "system.skill_blocked"):
                    # The answer to a task this coordinator published; the room comes from the task.
                    asked = _bus_tasks.pop(chain_id)
                    msg = {**msg, "payload": {**(msg.get("payload") or {}), "room_id": asked["room_id"]}}
                    relay = True
                else:
                    asked = {}
                if relay:
                    _start_relay(msg, asked, inject)
                    continue
                if reply_spec(((msg.get("payload") or {}).get("type")) or "").get("chat") and not await admits_chat_message(msg, config, client):
                    continue
                _pending[chain_id] = msg
                _answered.discard(chain_id)
                if before_task is not None:
                    try:
                        await before_task()
                    except Exception as exc:  # noqa: BLE001 -- stale settings must not drop the task
                        log_event("plugin:akinakes-channel", "before_task_failed", error=str(exc))
                log_event("plugin:akinakes-channel", "task_received", chain_id=chain_id, task_type=pending_task_type(chain_id))
                catalog = ""
                if config.orchestrator and reply_spec(msg_type).get("chat"):
                    catalog = render_bus_catalog([a for uid, a in (await _registry_agents()).items()
                                                  if uid != config.agent_uid and a.get("accepts")])
                # The coordinator routes: it passes the work on and never does it. The formalizer reads a request as work
                # for the reader to do and told the first live run to post the person's own words back (2026-11-05),
                # so its chat turns run as relay turns: no formalizer, no start-rung advice; the checks read the words.
                turn = (chain_id, render_task_text(msg, catalog), requester_words(msg))
                if config.orchestrator and reply_spec(msg_type).get("chat"):
                    inject(*turn, True)
                else:
                    inject(*turn)
        except Exception as exc:  # noqa: BLE001 -- a bus outage is waited out, not a crash loop
            status.last_error = str(exc)
            log_event("plugin:akinakes-channel", "poll_failed", error=str(exc))
            await asyncio.sleep(ERROR_BACKOFF_S)


def start_akinakes_channel(inject: InjectFn, before_task: BeforeTaskFn | None = None) -> list["asyncio.Task[None]"]:
    global _config, _http
    config = load_config()
    if config is None:
        return []
    _config = config
    _http = httpx.AsyncClient(timeout=30)
    status.configured = True
    global _inject
    _inject = inject
    log_event("plugin:akinakes-channel", "starting", uid=config.agent_uid, role=config.role, bus=config.bus_url)
    client = _http

    async def _poll() -> None:
        await _poll_forever(client, config, inject, before_task)

    async def _beat() -> None:
        await _heartbeat_forever(client, config)

    return [supervise("akinakes_channel_poll", _poll), supervise("akinakes_channel_heartbeat", _beat)]
