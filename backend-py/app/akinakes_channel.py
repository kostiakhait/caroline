"""Akinakes Agent Bus channel: this process IS one akinakes agent.

Same shape as ratatosk_channel.py: a supervised background loop is the primary interface, no chat window. It ports
agent_lib's wire protocol (POST /messages/poll, POST /messages/publish, the make_reply() envelope) and what AgentLoop
does around the poll (Registry registration and the 30 s heartbeat), to async httpx.

A delivered task becomes a proactive turn on a headless ChatSession, one session per chainId (main.py). The model
answers through the akinakes_publish_result tool (plugins/akinakes_bus_plugin.py), which calls publish_result() here.
The original message is kept by chainId, so the model never builds routing itself. A turn that ends with nothing
published produces system.skill_blocked (on_turn_end).

Active only when BUS_URL and AGENT_UID are set.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
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
    )


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
    """task payload type -> {payload_type, required}. From AKINAKES_REPLY_SPECS (a path), else the file next to this module."""
    path = Path(os.environ.get("AKINAKES_REPLY_SPECS") or Path(__file__).with_name("akinakes_reply_specs.json"))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 -- no specs means "accept any result.*", not a dead channel
        log_event("plugin:akinakes-channel", "reply_specs_unreadable", path=str(path), error=str(exc))
        return {}


def expected_reply_text(task_type: str) -> str:
    spec = load_reply_specs().get(task_type)
    if not spec:
        return "payload_type: a result.* type that fits this task, fields: its result"
    return f'payload_type="{spec["payload_type"]}", fields with: {", ".join(spec.get("required") or [])}'


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
    task_type = pending_task_type(chain_id) or ""
    spec = load_reply_specs().get(task_type)
    if spec:
        if payload_type != spec["payload_type"]:
            raise ValueError(f'For {task_type} the payload_type must be "{spec["payload_type"]}", not "{payload_type}".')
        missing = [k for k in spec.get("required") or [] if k not in fields]
        if missing:
            raise ValueError(f"Missing fields for {payload_type}: {', '.join(missing)}.")
    elif not payload_type.startswith("result."):
        raise ValueError('payload_type must start with "result.".')
    ack = await _publish(make_reply(original, _config.agent_uid, payload_type, **fields))
    _answered.add(chain_id)
    status.results_published += 1
    log_event("plugin:akinakes-channel", "result_published", chain_id=chain_id, payload_type=payload_type)
    return ack


async def publish_blocked(chain_id: str, reason: str) -> dict[str, Any]:
    assert _config is not None
    original = _pending.get(chain_id)
    if original is None:
        raise ValueError(f'No task is in progress for chainId "{chain_id}".')
    routing = (original["envelope"].get("routing") or {})
    skill = ((original.get("payload") or {}).get("type") or "").removeprefix("task.")
    ack = await _publish(make_message(
        sender_uid=_config.agent_uid,
        payload_type="system.skill_blocked",
        chain_id=chain_id,
        target_agent_uids=[routing.get("returnTo") or "chatGateway"],
        ttl_seconds=DEFAULT_TTL_S,
        reason=reason,
        skill=skill,
    ))
    _answered.add(chain_id)
    status.blocked_published += 1
    log_event("plugin:akinakes-channel", "blocked_published", chain_id=chain_id, reason=reason[:200])
    return ack


async def on_turn_end(chain_id: str) -> None:
    """Call when a session's turn ended. No result published for the chain -> system.skill_blocked."""
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
    return (
        f'[Agent Bus task. chainId="{env["chainId"]}" type="{task_type}" from="{env.get("senderUid", "")}".\n'
        f"Payload:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "Do the task with your tools. Finish by calling akinakes_publish_result with this exact chainId and "
        f"{expected_reply_text(task_type)}. If you cannot do it, call akinakes_report_blocked with the chainId and the "
        "reason. A document, section or other item of the task that Document Service reports as not found is such a "
        "case: report it blocked at once, do not look for it anywhere else. Nobody reads this conversation, only the "
        "published result is delivered.]"
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
