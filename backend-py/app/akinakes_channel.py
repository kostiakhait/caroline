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

InjectFn = Callable[[str, str, "str | None"], None]  # (chain_id, text, the requester's own words or None)
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
# chains the coordinator published a bus task for -> the room to tell, who asked there and what. Kept past the turn:
# the result comes later, and not every agent echoes room_id in its result (the secretaries' result.secretary_ok
# does not). The request goes into the relay turn's requester words (relay_words).
_bus_tasks: dict[str, dict[str, str]] = {}


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
    )


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


def make_reply(original: dict[str, Any], sender_uid: str, payload_type: str, **payload_extra: Any) -> dict[str, Any]:
    env = original["envelope"]
    routing = env.get("routing") or {}
    route = list(routing.get("route") or [])
    return make_message(
        sender_uid=sender_uid,
        payload_type=payload_type,
        chain_id=env["chainId"],
        parent_id=env["messageId"],
        ttl_seconds=(env.get("lifecycle") or {}).get("ttlSeconds", DEFAULT_TTL_S),
        route=route[1:] if route else [],
        return_to=routing.get("returnTo"),
        **payload_extra,
    )


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
    ack = await _publish(make_reply(original, _config.agent_uid, payload_type, **{**_echoed(original, spec), **fields}))
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
        reply = make_reply(original, _config.agent_uid, blocked["payload_type"],
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


async def publish_bus_task(chain_id: str, agent_uid: str, task_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    """A task for the agent the model chose (bus_agents / room_agents show who takes what). Sent with route=[agent] and
    returnTo=this agent: without a route the bus lets any poller claim it, this coordinator included (it does not skip
    the sender), and the result must come back here to be told in the room."""
    assert _config is not None
    original = _orchestrator_message(chain_id)
    if not task_type.startswith(("task.", "query.")):
        raise ValueError('task_type must start with "task." or "query.".')
    if not agent_uid or agent_uid == _config.agent_uid:
        raise ValueError("agent_uid must be the agent that takes this task (from bus_agents or room_agents).")
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
        return answer
    return f'{asked.get("asked_by") or "someone"} asked in the chat room:\n{asked["asked"]}\n\n{answer}'


def render_relay_text(msg: dict[str, Any], key: str) -> str:
    payload = msg.get("payload") or {}
    return (
        f'[Agent Bus result. chainId="{key}" type="{payload.get("type", "")}" room="{payload.get("room_id", "")}".\n'
        f"Payload:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "An agent has finished work that came from this room. Tell the room what happened, in the language of the "
        "conversation (chat_history shows it) and in plain words: what was done, with the title or id the people can use "
        "to find it. Post it with chat_post_message. If the result is an error, say what failed "
        "and what the person can do. Nobody reads this conversation, only what you post in the room.]"
    )


async def on_turn_end(chain_id: str) -> None:
    """Call when a session's turn ended. Nothing published for the chain -> the task's blocked reply (agent.pass for a
    chat message)."""
    if chain_id in _pending and chain_id not in _answered and chain_id not in _relay_keys:
        try:
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
    _asked.pop(chain_id, None)
    _passes.pop(chain_id, None)


# --- inbound ---------------------------------------------------------------------------------------------------------

def requester_words(msg: dict[str, Any]) -> str:
    """What the sender of this message asked, without our instructions around it: the formalizer and every check of the
    turn read this, the model gets render_task_text (2026-11-02: the whole template was formalized, and "hand the task
    to the room agents" from our own instructions counted as the person's request). A chat message is the person's
    text; a bus task is its type and fields."""
    env = msg.get("envelope") or {}
    payload = msg.get("payload") or {}
    if reply_spec(payload.get("type") or "").get("chat"):
        return f'{payload.get("sender_uid") or "someone"} wrote in the chat room:\n{payload.get("text") or ""}'
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
            "it. When you hand a request on or publish a task, say that it is in progress.\n"
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
                if config.orchestrator and msg_type == "agent.pass":
                    _passes.setdefault(chain_id, set()).add((msg.get("payload") or {}).get("agent_uid") or "")
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
                    key = f'{chain_id}-relay-{(msg.get("envelope") or {}).get("messageId", "")[:8]}'
                    _pending[key] = msg
                    _relay_keys.add(key)
                    # relay_words(msg, asked) is built but not passed yet (2026-11-02): fed to the formalizer it reads
                    # the person's request as work still to do (3/3, "set the objective" again). How a relay turn gets
                    # its task and its yardstick is an open decision, see the akinakes plan.
                    inject(key, render_relay_text(msg, key), None)
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
                inject(chain_id, render_task_text(msg, catalog), requester_words(msg))
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
    log_event("plugin:akinakes-channel", "starting", uid=config.agent_uid, role=config.role, bus=config.bus_url)
    client = _http

    async def _poll() -> None:
        await _poll_forever(client, config, inject, before_task)

    async def _beat() -> None:
        await _heartbeat_forever(client, config)

    return [supervise("akinakes_channel_poll", _poll), supervise("akinakes_channel_heartbeat", _beat)]
