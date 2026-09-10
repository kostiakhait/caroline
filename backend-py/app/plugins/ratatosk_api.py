"""Ports backend/src/ratatosk.ts -- client for Ratatosk (SquirrelWisdom's
own messenger, portal/chat.html). NOT a dedicated bot/API surface (none
exists): a caller is just "whoever holds a valid v2 session," same as any
browser tab. Wraps the exact same Camerlengo commands chat.js itself uses
(group:create/addMember/get/getUserGroups for conversations, file:read/
file:append against day-bucketed JSONL message files for the actual
messages) -- ported byte-for-byte from the TS original, including every
documented production bug-fix (base64 decode on read, the sw_lastmsg
chat-list-sort bump, DM-name-matching disambiguation, clock-skew
correction) rather than re-derived.

Every function here takes an explicit `session` -- the exact same
functions serve both "act as the owner" (a fresh sw_api.mint_v2_session)
and "act as Caroline's own account" (ratatosk_own_account.py's own
session).
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import string
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.plugins.sw_api import API_URL

# Same shared v2 key portal/chat.js itself sends on every command. NOT a
# secret in the security sense (ships in that page's own client-side JS to
# every browser tab); access control is enforced by the session + per-
# command resource ACLs, same as the real web client.
RATATOSK_API_KEY = "bsqrl2_lkD3dxBD4E4TQyLQJsy5OcDiz63b6h-I3YzGw2SQGKE"


class RatatoskError(Exception):
    pass


# Best-effort clock-skew correction against the SquirrelWisdom server's own
# clock, kept updated from the standard HTTP Date header every Ratatosk API
# response carries -- getServerNow() below replaces a plain "now" wherever
# Caroline stamps a timestamp of her own, removing this machine's own
# clock as a source of message-ordering/presence-heartbeat skew.
_server_clock_offset_ms = 0.0


def _update_server_clock_offset(response: httpx.Response) -> None:
    global _server_clock_offset_ms
    date_header = response.headers.get("date")
    if not date_header:
        return
    try:
        server_dt = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return
    _server_clock_offset_ms = server_dt.timestamp() * 1000 - time.time() * 1000


def get_server_now() -> float:
    """Best current estimate of the SquirrelWisdom server's own clock
    (epoch ms) -- falls back to this machine's own clock (offset 0) until
    at least one Ratatosk API response has actually been seen."""
    return time.time() * 1000 + _server_clock_offset_ms


async def _ratatosk_command(body: dict[str, Any]) -> dict[str, Any]:
    """The one choke point all Ratatosk network activity goes through.
    Session tokens are never logged here (same reasoning as never logging
    a password) -- left for a future structured-logging pass to mirror the
    original's console.error call; not logged at all for now rather than
    logging the session by mistake."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                res = await client.post(API_URL, json={"key": RATATOSK_API_KEY, **body, ".msgid": uuid.uuid4().hex})
                break
            except httpx.TransportError as exc:
                last_err = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        else:
            raise last_err  # type: ignore[misc]
        _update_server_clock_offset(res)
        return res.json()


def _is_ok(resp: dict[str, Any] | None) -> bool:
    return bool(resp) and resp.get(".status") == "ok"


def _to_base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out: list[str] = []
    while n:
        n, rem = divmod(n, 36)
        out.append(digits[rem])
    return "".join(reversed(out))


def _new_msg_id() -> str:
    """Base36-timestamp + random suffix -- exact same scheme chat.js's own
    newMsgId() uses, so ids Caroline mints look/sort like any other."""
    suffix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(5))
    return _to_base36(int(time.time() * 1000)) + suffix


def _today_str() -> str:
    """UTC-ISO date, matching chat.js's own todayStr(). Server-time-based
    (see get_server_now()), not this machine's own clock -- a skewed local
    clock near a UTC-midnight boundary could otherwise file a message
    under the wrong day's JSONL bucket from the one its own `ts` says it
    belongs to."""
    return datetime.fromtimestamp(get_server_now() / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _msg_file_path(group_id: str, date: str) -> str:
    return f"chats/{group_id}/{date}.jsonl"


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


async def list_conversations(session: str, user: str) -> list[dict[str, Any]]:
    """`user` (the caller's own email) is required by group:getUserGroups
    itself -- it does NOT infer the caller from `session` the way most
    other commands do."""
    resp = await _ratatosk_command({"command": "group:getUserGroups", "user": user, "session": session})
    if not _is_ok(resp):
        raise RatatoskError(f'group:getUserGroups failed: {resp.get(".reason", resp)}')
    ids: list[str] = []
    for item in resp.get("groups") or []:
        gid = item if isinstance(item, str) else (item or {}).get("group_id")
        if isinstance(gid, str) and gid:
            ids.append(gid)

    conversations: list[dict[str, Any]] = []
    for group_id in ids:
        g = await _ratatosk_command({"command": "group:get", "group_id": group_id, "session": session})
        if not _is_ok(g) or not g.get("group"):
            continue
        group = g["group"]
        members = list(dict.fromkeys((group.get("admins") or []) + (group.get("members") or []) + (group.get("observers") or [])))
        name = (group.get("meta") or {}).get("name") or ", ".join(members)
        conversations.append({"groupId": group_id, "name": name, "members": members})
    return conversations


async def get_recent_messages(session: str, group_id: str, days: int = 2) -> list[dict[str, Any]]:
    """Messages from the last `days` days (today plus however many prior
    days are asked for), oldest first -- one file per day, so this is just
    N file:read calls concatenated."""
    messages: list[dict[str, Any]] = []
    base = datetime.now(timezone.utc)
    for i in range(days - 1, -1, -1):
        date_str = (base - timedelta(days=i)).strftime("%Y-%m-%d")
        resp = await _ratatosk_command({"command": "file:read", "path": _msg_file_path(group_id, date_str), "session": session})
        if not _is_ok(resp) or not isinstance(resp.get("content"), str):
            continue
        # resp.content is base64 (see file:read's response shape) -- MUST
        # be decoded before JSONL parsing (a real bug in the original,
        # already fixed there; ported already-fixed).
        decoded = base64.b64decode(resp["content"]).decode("utf-8")
        messages.extend(_parse_jsonl(decoded))
    messages.sort(key=lambda m: m.get("ts", 0))
    return messages


async def send_message(session: str, group_id: str, sender_email: str, text: str) -> None:
    """Sends as `sender_email` (must be the account `session` actually
    belongs to -- Ratatosk has no separate "send as" concept, the
    message's `from` is just a field the client stamps, trusted because
    the session already proves the account)."""
    msg = {"id": _new_msg_id(), "ts": get_server_now(), "from": sender_email, "text": text}
    content = base64.b64encode(json.dumps(msg).encode("utf-8")).decode("ascii")
    resp = await _ratatosk_command({
        "command": "file:append", "path": _msg_file_path(group_id, _today_str()),
        "content": content, "session": session, ".dedup_field": "id",
    })
    if not _is_ok(resp):
        raise RatatoskError(f'file:append failed: {resp.get(".reason", resp)}')

    # Confirmed in the original TS port as the actual reason a sent
    # message was invisible in the real Ratatosk client despite being
    # correctly persisted: the client's own chat list sorts/surfaces
    # conversations by a SEPARATE "sw_lastmsg" var index, not by reading
    # the message files directly -- file:append alone never touches it.
    # Best-effort: a failure here must not fail the send itself, since the
    # message is already correctly persisted either way.
    await _ratatosk_command({"command": "var:set", "path": f"sw_lastmsg/{group_id}", "value": str(msg["ts"])})


async def send_presence_heartbeat(session: str, email: str) -> None:
    """Publishes Caroline's own online-presence heartbeat -- the exact
    same sw_presence/{email} var:set mechanism portal/chat.js's own
    sendPresenceHeartbeat() uses (15s TTL). Not yet called from anywhere
    on the Python side -- the presence-heartbeat timer loop is Phase 3
    scope (ratatoskChannel.ts), exposed here so that loop has this ready
    to call once it exists."""
    await _ratatosk_command({
        "command": "var:set", "path": f"sw_presence/{email}", "value": str(int(get_server_now() / 1000)), "session": session,
    })


def _looks_like_auto_group_name(name: str) -> bool:
    """Matches chat.js's own `_looksLikeAutoGroupName()` pattern -- see
    find_or_create_dm's own doc comment for why this is the exact
    discriminator the real Ratatosk client uses to decide whether a
    2-person group can even be rendered/found as a DM at all."""
    return bool(re.match(r"^Group [0-9A-Z]{8}$", name))


async def find_or_create_dm(session: str, self_email: str, other_email: str) -> str:
    """Finds an existing 2-member group with exactly {self_email,
    other_email}, or creates one -- Ratatosk has no dedicated "DM"
    concept, a DM is just a group with two members."""
    conversations = await list_conversations(session, self_email)
    matches = [
        c for c in conversations
        if len({m.lower() for m in c["members"]}) == 2
        and self_email.lower() in {m.lower() for m in c["members"]}
        and other_email.lower() in {m.lower() for m in c["members"]}
    ]
    if matches:
        # When more than one match exists, prefer one whose name matches
        # the pattern the real client requires to treat it as a DM at all
        # (a real bug found in production: an old, differently-named
        # duplicate group was being latched onto forever, invisible to
        # the phone client) -- only fall back to "just pick one" if none do.
        existing = matches[0] if len(matches) == 1 else next((c for c in matches if _looks_like_auto_group_name(c["name"])), matches[0])
        return existing["groupId"]

    group_id = _new_msg_id()
    # A group created with name:"" and BOTH members in `members` looks
    # fine via the API but never shows up in the real Ratatosk web
    # client's chat list: (1) the creator's own email must NOT be in
    # `members` -- the session holder becomes admin automatically,
    # `members` is only the invitee(s); (2) the name must match
    # generateGroupName()'s exact "Group " + 8 uppercase-alnum-chars shape
    # -- the client's own _looksLikeAutoGroupName() specifically
    # recognizes that pattern to know "never renamed, show the peer's
    # identity instead" for a 2-person chat. Replicating both exactly.
    auto_name = "Group " + "".join(random.choice("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(8))
    resp = await _ratatosk_command({
        "command": "group:create", "group_id": group_id, "name": auto_name, "description": "",
        "members": [other_email], "session": session,
    })
    if not _is_ok(resp):
        raise RatatoskError(f'group:create failed: {resp.get(".reason", resp)}')
    return group_id
