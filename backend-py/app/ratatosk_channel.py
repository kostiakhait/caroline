"""Ports backend/src/ratatoskChannel.ts -- the headless owner-DM control
channel (watches EVERY Ratatosk group Caroline's own account is a member
of for new messages from anyone else, injecting each as a proactive turn)
plus the independent presence-heartbeat loop. Pure logic + module-level
status only; wiring a headless ChatSession to actually receive the
injected text lives in main.py (mirrors the original's own split between
this module and server.ts).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.logging_setup import log_event
from app.plugins.ratatosk_api import get_recent_messages, get_server_now, list_conversations, send_presence_heartbeat
from app.plugins.ratatosk_own_account import get_own_v2_session, has_own_ratatosk_account, own_ratatosk_email

# No push from Ratatosk -- its own UI polls every 4-5s; this owner-DM
# control channel is far less latency-sensitive (it's "give Caroline an
# instruction", not a live conversation someone's staring at), so a longer
# interval is fine and keeps this from hammering the backend.
POLL_INTERVAL_MS = 15_000

# Deliberately its OWN, much faster interval -- NOT piggybacked on
# POLL_INTERVAL_MS above. chat.js's presence TTL is 15s; reusing the 15s
# owner-DM poll for the heartbeat too would put every heartbeat right at
# the TTL boundary, so any tick that ran even slightly late would let
# Caroline visibly flicker offline. 5s matches chat.js's own
# PRESENCE_POLL_MS exactly, giving 3 heartbeats per TTL window.
PRESENCE_INTERVAL_MS = 5_000


def _cursors_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "ratatosk-groups-cursor.json"


def _load_cursors(workspace_dir: str) -> dict[str, float]:
    path = _cursors_path(workspace_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log_event("plugin:ratatosk-channel", "load_cursors_failed", error=str(exc))
        return {}


def _save_cursors(workspace_dir: str, cursors: dict[str, float]) -> None:
    try:
        _cursors_path(workspace_dir).write_text(json.dumps(cursors, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        log_event("plugin:ratatosk-channel", "save_cursors_failed", error=str(exc))


@dataclass
class ChannelStatus:
    """In-memory snapshot of the channel's own state, for the
    ratatosk_channel_status control op (Settings/external tooling) -- a
    poll loop with no introspection and thin logging is nearly impossible
    to diagnose after the fact."""

    enabled: bool = False
    tick_count: int = 0
    last_tick_at_iso: str | None = None
    last_tick_outcome: str | None = None
    caroline_email: str | None = None
    monitored_group_count: int = 0
    last_error: str | None = None
    last_error_at_iso: str | None = None


_status = ChannelStatus()


def get_ratatosk_channel_status() -> dict[str, Any]:
    return asdict(_status)


async def _owner_channel_tick(workspace_dir: str, inject_from_owner: Callable[[str], None]) -> None:
    _status.tick_count += 1
    _status.last_tick_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tick_label = f"tick #{_status.tick_count}"
    try:
        has_own = has_own_ratatosk_account(workspace_dir)
        _status.enabled = has_own
        if not has_own:
            _status.last_tick_outcome = "skipped (no own Ratatosk account yet)"
            log_event("plugin:ratatosk-channel", "tick", label=tick_label, outcome=_status.last_tick_outcome)
            return
        caroline_email = own_ratatosk_email(workspace_dir)
        assert caroline_email is not None
        _status.caroline_email = caroline_email

        session = await get_own_v2_session(workspace_dir)
        conversations = await list_conversations(session, caroline_email)
        _status.monitored_group_count = len(conversations)
        log_event("plugin:ratatosk-channel", "tick_monitoring", label=tick_label, group_count=len(conversations))

        cursors = _load_cursors(workspace_dir)
        per_group_new: list[tuple[dict[str, Any], list[str]]] = []
        total_new = 0

        for group in conversations:
            group_id = group["groupId"]
            messages = await get_recent_messages(session, group_id, 2)
            if not messages:
                continue
            max_ts = max(m.get("ts", 0) for m in messages)

            # On a genuinely first-ever check of a given group (no cursor
            # entry yet -- fresh workspace, or Caroline was just added to
            # this group), treat "already seen" as 24h ago rather than
            # "everything up to right now" -- the OLD behavior (seed cursor
            # to max_ts, reply to nothing) silently swallowed real messages
            # that had just arrived. Genuinely old history (predating this
            # group's first check by more than a day) still isn't replayed.
            last_seen_ts = cursors.get(group_id, get_server_now() - 24 * 3600_000)
            cursors[group_id] = max_ts

            new_messages = [
                m for m in messages
                if m.get("ts", 0) > last_seen_ts and (m.get("from") or "").lower() != caroline_email.lower()
            ]
            if not new_messages:
                continue
            texts = [f"{m.get('from') or 'unknown'}: {m.get('text') or ''}" for m in new_messages]
            texts = [t for t in texts if t.strip()]
            if not texts:
                continue
            per_group_new.append((group, texts))
            total_new += len(texts)

        _save_cursors(workspace_dir, cursors)

        if not per_group_new:
            _status.last_tick_outcome = "no new messages in any monitored group since last cursor"
            log_event("plugin:ratatosk-channel", "tick", label=tick_label, outcome=_status.last_tick_outcome)
            return

        _status.last_tick_outcome = f"{total_new} new message(s) across {len(per_group_new)} group(s), injecting into headless session"
        log_event("plugin:ratatosk-channel", "tick", label=tick_label, outcome=_status.last_tick_outcome)

        # Every group's exact groupId is spelled out right next to its own
        # messages -- a bare "reply there" gives the model no actual value
        # to latch onto, so it can pick a groupId from unrelated prior
        # context/memory instead of the real one, and the reply silently
        # lands in the wrong conversation. Spelled out once per group, not
        # once for the whole batch, since multiple groups can be pending.
        sections = [
            f'Group "{group["name"]}" (groupId="{group["groupId"]}") -- reply here via ratatosk_send_message '
            f'as:"caroline" groupId:"{group["groupId"]}" (this exact groupId, not one from earlier in your own '
            f"history/memory):\n" + "\n".join(texts)
            for group, texts in per_group_new
        ]
        inject_from_owner(
            f"[New Ratatosk message(s) since you last checked, across {len(per_group_new)} group(s) -- reply to "
            f"each using its OWN groupId shown below, not in any chat window:\n\n" + "\n\n".join(sections) + "]"
        )
    except Exception as exc:
        _status.last_error = str(exc)
        _status.last_error_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _status.last_tick_outcome = f"threw: {_status.last_error}"
        log_event("plugin:ratatosk-channel", "tick_failed", label=tick_label, error=_status.last_error)


def start_ratatosk_owner_channel(workspace_dir: str, inject_from_owner: Callable[[str], None]) -> asyncio.Task[None]:
    log_event("plugin:ratatosk-channel", "starting_poll_loop", interval_ms=POLL_INTERVAL_MS)

    async def _loop() -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL_MS / 1000)
            await _owner_channel_tick(workspace_dir, inject_from_owner)

    return asyncio.create_task(_loop())


def start_ratatosk_presence_heartbeat(workspace_dir: str) -> asyncio.Task[None]:
    log_event("plugin:ratatosk-presence", "starting_heartbeat_loop", interval_ms=PRESENCE_INTERVAL_MS)

    async def _loop() -> None:
        tick_count = 0
        while True:
            await asyncio.sleep(PRESENCE_INTERVAL_MS / 1000)
            tick_count += 1
            try:
                if not has_own_ratatosk_account(workspace_dir):
                    if tick_count == 1:
                        log_event("plugin:ratatosk-presence", "tick_skipped_no_account", tick=tick_count)
                    continue
                caroline_email = own_ratatosk_email(workspace_dir)
                assert caroline_email is not None
                session = await get_own_v2_session(workspace_dir)
                await send_presence_heartbeat(session, caroline_email)
            except Exception as exc:
                log_event("plugin:ratatosk-presence", "tick_failed", tick=tick_count, error=str(exc))

    return asyncio.create_task(_loop())
