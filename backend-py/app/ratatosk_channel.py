"""Ports backend/src/ratatoskChannel.ts -- the headless Ratatosk control
channel plus the independent presence-heartbeat loop. Pure logic + module-
level status only; wiring a headless ChatSession to actually receive the
injected text lives in main.py (mirrors the original's own split between
this module and server.ts).

Per explicit instruction (2026-10-13): watches BOTH Ratatosk identities now
(ACCOUNTS = ("self", "owner")), same structure every other two-account
messenger channel uses (telegram_channel.py etc.) -- it used to watch only
Caroline's own ("self") account; the owner's own Ratatosk correspondence had
no automatic watcher at all, only the manual ratatosk_plugin.py tools. "self"
keeps its full behavior (the /stop command, reply-expected tracking, inviting
a reply via ratatosk_send_message); "owner" is read-only/informational, same
framing telegram_channel.py already uses for the owner's own account -- tell
the owner about it if it matters, never reply as them unless explicitly asked.
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
from app.plugins.ratatosk_api import get_recent_messages, get_server_now, list_conversations, send_message, send_presence_heartbeat
from app.plugins.ratatosk_own_account import get_own_v2_session, has_own_ratatosk_account, own_ratatosk_email
from app.plugins.ratatosk_plugin import resolve_ratatosk_session
from app.task_supervisor import supervise

ACCOUNTS = ("self", "owner")
DEFAULT_ACCOUNT = "self"

# A direct command, not something for the model to reply to -- handled here,
# immediately, without ever reaching inject_self. Per explicit instruction: a
# headless session (no WS/Settings UI connection reachable at all) still
# needs to be stoppable from wherever Ratatosk itself is reachable, e.g. the
# owner's phone, not only from the desktop app that happens to be open.
# Case-insensitive, exact match after stripping -- never a substring match,
# so it can't misfire on a real sentence that happens to contain the word.
# Only meaningful on the "self" account -- see _channel_tick.
STOP_COMMAND_TEXT = "/stop"

# No push from Ratatosk -- its own UI polls every 4-5s; this channel is far
# less latency-sensitive (it's "give Caroline an instruction" / "tell her
# about this", not a live conversation someone's staring at), so a longer
# interval is fine and keeps this from hammering the backend. Shared by both
# accounts -- there's no reason the owner's own correspondence needs tighter
# latency than Caroline's own.
POLL_INTERVAL_MS = 15_000

# Deliberately its OWN, much faster interval -- NOT piggybacked on
# POLL_INTERVAL_MS above. chat.js's presence TTL is 15s; reusing the 15s
# poll for the heartbeat too would put every heartbeat right at the TTL
# boundary, so any tick that ran even slightly late would let Caroline
# visibly flicker offline. 5s matches chat.js's own PRESENCE_POLL_MS
# exactly, giving 3 heartbeats per TTL window. Presence is inherently a
# "self"-only concept (Caroline's own online status) -- unaffected by this
# change.
PRESENCE_INTERVAL_MS = 5_000


def _cursors_path(workspace_dir: str, account: str) -> Path:
    # Per explicit instruction (2026-10-13): one cursor file PER ACCOUNT --
    # the two accounts watch entirely different sets of groups, sharing one
    # file would mix their "already seen" bookkeeping together.
    suffix = "" if account == DEFAULT_ACCOUNT else f"-{account}"
    return Path(workspace_dir) / f"ratatosk-groups-cursor{suffix}.json"


def _load_cursors(workspace_dir: str, account: str) -> dict[str, float]:
    path = _cursors_path(workspace_dir, account)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log_event("plugin:ratatosk-channel", "load_cursors_failed", account=account, error=str(exc))
        return {}


def _save_cursors(workspace_dir: str, account: str, cursors: dict[str, float]) -> None:
    try:
        _cursors_path(workspace_dir, account).write_text(json.dumps(cursors, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        log_event("plugin:ratatosk-channel", "save_cursors_failed", account=account, error=str(exc))


@dataclass
class ChannelStatus:
    """In-memory snapshot of one account's own channel state, for the
    ratatosk_channel_status control op (Settings/external tooling) -- a
    poll loop with no introspection and thin logging is nearly impossible
    to diagnose after the fact."""

    enabled: bool = False
    tick_count: int = 0
    last_tick_at_iso: str | None = None
    last_tick_outcome: str | None = None
    email: str | None = None
    monitored_group_count: int = 0
    last_error: str | None = None
    last_error_at_iso: str | None = None


_status: dict[str, ChannelStatus] = {account: ChannelStatus() for account in ACCOUNTS}


def get_ratatosk_channel_status() -> dict[str, Any]:
    """Per explicit instruction (2026-10-13): now keyed by account
    ({"self": {...}, "owner": {...}}) -- every consumer (main.py's control
    op, the companion phone app's own mirror) already treats this as an
    opaque diagnostic dict, nothing parses specific top-level fields."""
    return {account: asdict(status) for account, status in _status.items()}


async def _channel_tick(
    account: str,
    workspace_dir: str,
    inject_self: Callable[[str, list[str]], None],
    inject_owner_readonly: Callable[[str], None],
    stop_session: Callable[[], bool],
) -> None:
    status = _status[account]
    status.tick_count += 1
    status.last_tick_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tick_label = f"{account} tick #{status.tick_count}"
    try:
        if account == DEFAULT_ACCOUNT:
            has_own = has_own_ratatosk_account(workspace_dir)
            status.enabled = has_own
            if not has_own:
                status.last_tick_outcome = "skipped (no own Ratatosk account yet)"
                log_event("plugin:ratatosk-channel", "tick", label=tick_label, outcome=status.last_tick_outcome)
                return
        else:
            status.enabled = True  # the owner side just needs an SW login, checked by resolve_ratatosk_session's own gate
        try:
            session, email = await resolve_ratatosk_session(account)
        except Exception as exc:
            status.last_tick_outcome = f"skipped ({exc})"
            log_event("plugin:ratatosk-channel", "tick", label=tick_label, outcome=status.last_tick_outcome)
            return
        status.email = email

        conversations = await list_conversations(session, email)
        status.monitored_group_count = len(conversations)
        log_event("plugin:ratatosk-channel", "tick_monitoring", label=tick_label, group_count=len(conversations))

        cursors = _load_cursors(workspace_dir, account)
        per_group_new: list[tuple[dict[str, Any], list[str]]] = []
        total_new = 0

        for group in conversations:
            group_id = group["groupId"]
            messages = await get_recent_messages(session, group_id, 2)
            if not messages:
                continue
            max_ts = max(m.get("ts", 0) for m in messages)

            # On a genuinely first-ever check of a given group (no cursor
            # entry yet -- fresh workspace, or this account was just added
            # to this group), treat "already seen" as 24h ago rather than
            # "everything up to right now" -- the OLD behavior (seed cursor
            # to max_ts, reply to nothing) silently swallowed real messages
            # that had just arrived. Genuinely old history (predating this
            # group's first check by more than a day) still isn't replayed.
            last_seen_ts = cursors.get(group_id, get_server_now() - 24 * 3600_000)
            cursors[group_id] = max_ts

            new_messages = [
                m for m in messages
                if m.get("ts", 0) > last_seen_ts and (m.get("from") or "").lower() != email.lower()
            ]
            if not new_messages:
                continue

            # The /stop command and the invitation to reply as this account
            # only make sense on "self" -- replying to someone in the
            # OWNER's own correspondence as Caroline, or letting a stranger
            # in the owner's DMs kill Caroline's session, would both be
            # exactly the kind of impersonation/overreach every other
            # messenger's owner-account handling already refuses to do.
            if account == DEFAULT_ACCOUNT:
                command_messages = [m for m in new_messages if (m.get("text") or "").strip().lower() == STOP_COMMAND_TEXT]
                if command_messages:
                    stopped = stop_session()
                    log_event("plugin:ratatosk-channel", "stop_command_received", label=tick_label, group_id=group_id, stopped=stopped)
                    try:
                        await send_message(
                            session, group_id, email,
                            "Stopped." if stopped else "Nothing was running to stop.",
                        )
                    except Exception as exc:
                        log_event("plugin:ratatosk-channel", "stop_command_reply_failed", error=str(exc))
                    new_messages = [m for m in new_messages if m not in command_messages]
                    if not new_messages:
                        continue

            texts = [f"{m.get('from') or 'unknown'}: {m.get('text') or ''}" for m in new_messages]
            texts = [t for t in texts if t.strip()]
            if not texts:
                continue
            per_group_new.append((group, texts))
            total_new += len(texts)

        _save_cursors(workspace_dir, account, cursors)

        if not per_group_new:
            status.last_tick_outcome = "no new messages in any monitored group since last cursor"
            log_event("plugin:ratatosk-channel", "tick", label=tick_label, outcome=status.last_tick_outcome)
            return

        status.last_tick_outcome = f"{total_new} new message(s) across {len(per_group_new)} group(s), injecting"
        log_event("plugin:ratatosk-channel", "tick", label=tick_label, outcome=status.last_tick_outcome)

        if account == DEFAULT_ACCOUNT:
            # Every group's exact groupId is spelled out right next to its own
            # messages -- a bare "reply there" gives the model no actual value
            # to latch onto, so it can pick a groupId from unrelated prior
            # context/memory instead of the real one, and the reply silently
            # lands in the wrong conversation. Spelled out once per group, not
            # once for the whole batch, since multiple groups can be pending.
            sections = [
                f'Group "{group["name"]}" (groupId="{group["groupId"]}") -- reply here via ratatosk_send_message '
                f'account:"self" groupId:"{group["groupId"]}" (this exact groupId, not one from earlier in your own '
                f"history/memory):\n" + "\n".join(texts)
                for group, texts in per_group_new
            ]
            inject_self(
                f"[New Ratatosk message(s) since you last checked, across {len(per_group_new)} group(s) -- reply to "
                f"each using its OWN groupId shown below, not in any chat window:\n\n" + "\n\n".join(sections) + "]",
                [group["groupId"] for group, _texts in per_group_new],
            )
        else:
            # Per explicit instruction (2026-10-13): same read-only framing
            # telegram_channel.py already uses for the owner's own account --
            # this is the OWNER'S OWN correspondence, not Caroline's. Tell
            # the owner about it if it matters; never reply as them unless
            # they have explicitly asked for this specific reply.
            sections = [
                f'Group "{group["name"]}" (groupId="{group["groupId"]}"):\n' + "\n".join(texts)
                for group, texts in per_group_new
            ]
            inject_owner_readonly(
                f"[New message(s) in the owner's OWN Ratatosk correspondence since you last checked, across "
                f"{len(per_group_new)} group(s) -- this is the OWNER'S OWN correspondence, not yours: read it and "
                "tell the owner about it if it matters, but do not reply as them unless they have explicitly asked "
                'you to send this specific reply (ratatosk_send_message(account="owner", groupId=..., text=...)):'
                "\n\n" + "\n\n".join(sections) + "]"
            )
    except Exception as exc:
        status.last_error = str(exc)
        status.last_error_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        status.last_tick_outcome = f"threw: {status.last_error}"
        log_event("plugin:ratatosk-channel", "tick_failed", label=tick_label, error=status.last_error)


def start_ratatosk_owner_channel(
    workspace_dir: str,
    inject_self: Callable[[str, list[str]], None],
    inject_owner_readonly: Callable[[str], None],
    stop_session: Callable[[], bool],
) -> list[asyncio.Task[None]]:
    """Per explicit instruction (2026-10-13): one supervised poll loop PER
    ACCOUNT, same shape telegram_channel.py's start_telegram_channel already
    uses -- independent failure/restart per account, same as every other
    two-account messenger channel."""
    log_event("plugin:ratatosk-channel", "starting_poll_loop", interval_ms=POLL_INTERVAL_MS, accounts=list(ACCOUNTS))

    tasks: list[asyncio.Task[None]] = []
    for account in ACCOUNTS:
        def _make_loop(account: str) -> Callable[[], Awaitable[None]]:
            async def _loop() -> None:
                while True:
                    await asyncio.sleep(POLL_INTERVAL_MS / 1000)
                    await _channel_tick(account, workspace_dir, inject_self, inject_owner_readonly, stop_session)
            return _loop

        tasks.append(supervise(f"ratatosk_channel_{account}", _make_loop(account)))
    return tasks


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

    return supervise("ratatosk_presence", _loop)
