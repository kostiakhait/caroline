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

InjectFn = Callable[[str, str], None]  # (chain_id, text)
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
      chat     -- true for chat.message: answered in the chat room, or passed (agent.pass)."""
    path = Path(os.environ.get("AKINAKES_REPLY_SPECS") or Path(__file__).with_name("akinakes_reply_specs.json"))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 -- no specs means "accept any result.*", not a dead channel
        log_event("plugin:akinakes-channel", "reply_specs_unreadable", path=str(path), error=str(exc))
        return {}


def reply_spec(message_type: str) -> dict[str, Any]:
    return load_reply_specs().get(message_type) or {}


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
    if original is None or not is_chat(chain_id):
        raise ValueError(f'No chat message is in progress for chainId "{chain_id}". Use the exact chainId from the message.')
    return original


async def post_chat(chain_id: str, text: str) -> None:
    """Answers the chat message of this chain in its room, as agent_lib's agents do (Chat Service's agent_message)."""
    assert _config is not None and _http is not None
    room_id = (_chat_message(chain_id).get("payload") or {}).get("room_id") or ""
    if not text.strip():
        raise ValueError("The message text is empty.")
    resp = await _http.post(
        f"{_config.chat_url}/internal/rooms/{room_id}/agent_message",
        json={"sender_uid": _config.agent_uid, "sender_type": "agent", "msg_type": "agent_message", "payload": {"text": text}},
        headers={"x-internal-key": _config.chat_key},
        timeout=15,
    )
    resp.raise_for_status()
    _answered.add(chain_id)
    status.results_published += 1
    log_event("plugin:akinakes-channel", "chat_posted", chain_id=chain_id, room_id=room_id, text_len=len(text))


async def chat_history(chain_id: str) -> list[dict[str, Any]]:
    """The room's messages, oldest first (Chat Service GET /rooms/{room_id}/messages, all pages)."""
    assert _config is not None and _http is not None
    room_id = (_chat_message(chain_id).get("payload") or {}).get("room_id") or ""
    messages: list[dict[str, Any]] = []
    after_seq = 0
    while True:
        resp = await _http.get(f"{_config.chat_url}/rooms/{room_id}/messages",
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


async def on_turn_end(chain_id: str) -> None:
    """Call when a session's turn ended. Nothing published for the chain -> the task's blocked reply (agent.pass for a
    chat message)."""
    if chain_id in _pending and chain_id not in _answered:
        try:
            await publish_blocked(chain_id, "The agent's turn ended without publishing a result.")
        except Exception as exc:  # noqa: BLE001
            status.last_error = str(exc)
            log_event("plugin:akinakes-channel", "blocked_publish_failed", chain_id=chain_id, error=str(exc))
    _pending.pop(chain_id, None)
    _answered.discard(chain_id)


# --- inbound ---------------------------------------------------------------------------------------------------------

def render_task_text(msg: dict[str, Any]) -> str:
    env = msg["envelope"]
    payload = msg.get("payload") or {}
    task_type = payload.get("type", "")
    if reply_spec(task_type).get("chat"):
        return (
            f'[Agent Bus chat message. chainId="{env["chainId"]}" room="{payload.get("room_id", "")}" '
            f'from="{payload.get("sender_uid", "")}".\n'
            f"Text:\n{payload.get('text', '')}\n\n"
            "Every agent of the room receives it. If it is for you -- addressed to you, or what your role does -- "
            "do it with your tools (chat_history shows the conversation so far) and answer in the room with "
            "chat_post_message and this exact chainId. If it is not for you, call akinakes_pass with the chainId and "
            "do nothing else. Nobody reads this conversation, only what you post in the room.]"
        )
    return (
        f'[Agent Bus task. chainId="{env["chainId"]}" type="{task_type}" from="{env.get("senderUid", "")}".\n'
        f"Payload:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f'"{task_type}" is the kind of task, not a tool: there is no tool by that name. '
        "Do the task with your tools. Finish by calling akinakes_publish_result with this exact chainId and "
        f"{expected_reply_text(task_type)}. If you cannot do it, call akinakes_report_blocked with the chainId and the "
        "reason. Something the task needs to exist already -- the document, a run, a section to read or to change -- "
        "that Document Service reports as not found is such a case: report it blocked at once, do not look for it "
        "anywhere else. A section the task asks you to write may not exist yet; that is no reason to stop. Nobody reads "
        "this conversation, only the published result is delivered.]"
    )


async def _register(client: httpx.AsyncClient, config: ChannelConfig) -> None:
    global _agent_token
    if not config.registry_url:
        return
    try:
        r = await client.post(f"{config.registry_url}/agents", json={
            "uid": config.agent_uid, "role": config.role, "specialization": config.specialization,
            "scope": "workspace", "deployment_mode": "unmanaged", "description": config.description,
            "personal_office_member": False,
        }, timeout=5)
        if r.status_code == 201:
            _agent_token = r.json().get("token", "") or ""
            log_event("plugin:akinakes-channel", "registered", uid=config.agent_uid)
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
                inject(chain_id, render_task_text(msg))
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
