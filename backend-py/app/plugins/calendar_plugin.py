"""calendar -- ICS files and CalDAV calendars (2026-10-24, the user: "Создание
.ics, Чтение приглашений, Подключение к календарю"; ICS through Camerlengo's own
commands -- "Чинить серверный misc:addCalendar" -- and CalDAV first; Google and
Outlook need registered OAuth apps and come later).

ICS files, through Camerlengo (misc:addCalendar / misc:parseCalendar, free):
  calendar_make_ics   an event or an invitation as a .ics file, to attach to a
                      letter or to import
  calendar_read_ics   every event of a .ics file -- an invitation that came
                      with a letter, an exported calendar

CalDAV (iCloud, Yandex, Fastmail, Nextcloud, ... -- app password, no app
registration), two accounts like the messengers: "self" is Caroline's own
calendar, "owner" the user's:
  calendar_connect        log in; the credentials are kept like a mailbox's
  calendar_list           the account's calendars
  calendar_events         events in a time range (recurring ones expanded)
  calendar_add_event      add an event (or an invitation) to a calendar
  calendar_delete_event   delete one by its uid

Credentials, as for mail (email_credentials.py, the user's rule of 2026-10-22):
a local encrypted copy, a "vault:caldav:<account>" note in Caroline:Vault, and
memory -- saved once a login worked."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path
from typing import Any

from app.context_cap import cap_count
from app.logging_setup import log_event
from app.plugins.loader import Plugin, PluginTool
from app.plugins.sw_api import CAROLINE_SW_KEY, SwApiError, call_v2
from app.secret_store import decrypt_text_from_file, encrypt_text_to_file
from app.workspace_dir import WORKSPACE_DIR

ACCOUNTS = ("self", "owner")
VAULT_FOLDER = "Caroline:Vault"
ICS_DIR = Path(WORKSPACE_DIR) / "calendar"
_background: set[asyncio.Task[Any]] = set()

# misc:addCalendar's own arguments, as the tools take them.
_EVENT_FIELDS = ("title", "start", "end", "durationMinutes", "tzid", "allDay", "description", "location",
                 "attendees", "organizer", "rrule", "remindersMinutes", "uid", "url")


# --- ICS through Camerlengo ---------------------------------------------------------

async def _make_ics(args: dict[str, Any]) -> dict[str, Any]:
    """misc:addCalendar for the event the tool arguments describe."""
    if not str(args.get("title") or "").strip() or not str(args.get("start") or "").strip():
        raise ValueError('an event needs "title" and "start" (ISO 8601; with "tzid", a time without offset is read in that zone)')
    command = {k: args[k] for k in _EVENT_FIELDS if args.get(k) not in (None, "", [])}
    command["time"] = command.pop("start")
    return await call_v2("misc:addCalendar", key=CAROLINE_SW_KEY, **command)


async def _parse_ics(raw: bytes) -> dict[str, Any]:
    envelope = await call_v2("misc:parseCalendar", key=CAROLINE_SW_KEY, data=base64.b64encode(raw).decode())
    return {"method": envelope.get("method"), "events": envelope.get("events") or []}


async def calendar_make_ics(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    try:
        made = await _make_ics(args)
    except (ValueError, SwApiError) as exc:
        return {"text": f"The event could not be made: {exc}", "is_error": True}
    data = base64.b64decode(made["file"]["data"])
    save_path = str(args.get("savePath") or "").strip()
    path = Path(save_path) if save_path else ICS_DIR / f"{re.sub(r'[^A-Za-z0-9._-]+', '_', str(args['title']))[:60] or 'event'}.ics"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    log_event("plugin:calendar", "ics_made", path=str(path), uid=made.get("uid"), invitation=made.get("invitation"))
    kind = "an invitation (METHOD:REQUEST)" if made.get("invitation") else "an event to import"
    return {"text": f"Saved {kind} to {path} (uid {made.get('uid')}). Attach it to a letter, or give the same uid "
                    "to update this event later."}


async def calendar_read_ics(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    path = Path(str(args.get("path") or "").strip())
    if not path.is_file():
        return {"text": f"No file at {path} -- give the path of a downloaded .ics attachment or file.", "is_error": True}
    try:
        parsed = await _parse_ics(path.read_bytes())
    except SwApiError as exc:
        return {"text": f"{path} could not be read as a calendar file: {exc}", "is_error": True}
    method = parsed["method"]
    meaning = {"REQUEST": "an invitation", "CANCEL": "a cancellation", "REPLY": "an answer to an invitation",
               "PUBLISH": "a published event"}.get(str(method or "").upper(), "a calendar file")
    log_event("plugin:calendar", "ics_read", path=str(path), method=method, events=len(parsed["events"]))
    return {"text": f"{path} is {meaning}" + (f" (METHOD:{method})" if method else "") + f", {len(parsed['events'])} event(s):\n"
                    + json.dumps(parsed["events"], ensure_ascii=False, indent=1)}


# --- CalDAV credentials -------------------------------------------------------------

def _vault_title(account: str) -> str:
    return f"vault:caldav:{account}"


def _local_path(account: str) -> Path:
    return Path(WORKSPACE_DIR) / "calendar-accounts" / f"{account}.bin"


def _load_local(account: str) -> dict[str, Any] | None:
    try:
        text = decrypt_text_from_file(_local_path(account))
        return json.loads(text) if text else None
    except Exception as exc:  # noqa: BLE001 -- a damaged copy falls back to the vault note
        log_event("plugin:calendar", "credentials_local_read_failed", account=account, error=str(exc)[:200])
        return None


async def _find_vault_note(account: str) -> tuple[str | None, dict[str, Any] | None]:
    from app.plugins import notes_plugin

    title = _vault_title(account)
    found = json.loads((await notes_plugin.notes_search({"query": title}, None))["text"])
    for entry in found if isinstance(found, list) else []:
        if str(entry.get("title") or "").strip().lower() != title:
            continue
        note = json.loads((await notes_plugin.notes_get({"id": entry["id"]}, None))["text"])
        body = (note.get("text") or "").split("\n", 1)[-1]
        start, end = body.find("{"), body.rfind("}")
        try:
            return entry["id"], json.loads(body[start:end + 1]) if 0 <= start < end else None
        except ValueError:
            return entry["id"], None
    return None, None


async def _credentials(account: str) -> dict[str, Any]:
    kept = _load_local(account)
    if not kept:
        try:
            _note_id, kept = await _find_vault_note(account)
        except Exception as exc:  # noqa: BLE001
            log_event("plugin:calendar", "credentials_vault_read_failed", account=account, error=str(exc)[:200])
            kept = None
    if not kept or not all(kept.get(k) for k in ("url", "username", "password")):
        raise ValueError(f'The "{account}" calendar is not connected yet -- use calendar_connect(account="{account}", url, '
                         "username, password) first; ask the user for the CalDAV address and an app password.")
    return kept


def _remember(account: str, creds: dict[str, Any]) -> None:
    """A login that worked: kept locally now, in Notes and memory in the background."""
    if _load_local(account) == creds:
        return
    try:
        path = _local_path(account)
        path.parent.mkdir(parents=True, exist_ok=True)
        encrypt_text_to_file(path, json.dumps(creds, ensure_ascii=False), f"Caroline calendar {account}")
        log_event("plugin:calendar", "credentials_saved_local", account=account)
    except Exception as exc:  # noqa: BLE001
        log_event("plugin:calendar", "credentials_save_local_failed", account=account, error=str(exc)[:200])
    task = asyncio.ensure_future(_save_to_notes_and_memory(account, creds))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _save_to_notes_and_memory(account: str, creds: dict[str, Any]) -> None:
    from app.plugins import memory_plugin, notes_plugin

    text = _vault_title(account) + "\n" + json.dumps(creds, ensure_ascii=False)
    try:
        note_id, _old = await _find_vault_note(account)
        if note_id:
            await notes_plugin.notes_update({"id": note_id, "text": text, "folder": VAULT_FOLDER}, None)
        else:
            await notes_plugin.notes_create({"text": text, "folder": VAULT_FOLDER}, None)
        log_event("plugin:calendar", "credentials_saved_notes", account=account, updated=bool(note_id))
    except Exception as exc:  # noqa: BLE001
        log_event("plugin:calendar", "credentials_save_notes_failed", account=account, error=str(exc)[:200])
    try:
        who = "Caroline's own" if account == "self" else "the user's"
        await memory_plugin.save_info({"text": f"{who} CalDAV calendar ({account}): server {creds['url']}, "
                                               f"login {creds['username']}, password {creds['password']}."}, None)
        log_event("plugin:calendar", "credentials_saved_memory", account=account)
    except Exception as exc:  # noqa: BLE001
        log_event("plugin:calendar", "credentials_save_memory_failed", account=account, error=str(exc)[:200])


# --- CalDAV -------------------------------------------------------------------------

def _account(args: dict[str, Any]) -> str:
    account = str(args.get("account") or "self").strip()
    if account not in ACCOUNTS:
        raise ValueError(f'"account" must be one of {", ".join(ACCOUNTS)}, not {account!r}')
    return account


def _principal(creds: dict[str, Any]) -> Any:
    import caldav   # late: a build without it still starts, these tools say so

    client = caldav.DAVClient(url=creds["url"], username=creds["username"], password=creds["password"])
    return client.principal()


def _pick_calendar(principal: Any, name: str | None) -> Any:
    calendars = principal.calendars()
    if not calendars:
        raise ValueError("this account has no calendars")
    if name:
        for calendar in calendars:
            if (calendar.name or "").strip().lower() == name.strip().lower():
                return calendar
        raise ValueError(f"no calendar named {name!r}; there are: {', '.join(c.name or '(unnamed)' for c in calendars)}")
    if len(calendars) == 1:
        return calendars[0]
    raise ValueError(f"this account has several calendars -- say which one (`calendar`): "
                     f"{', '.join(c.name or '(unnamed)' for c in calendars)}")


async def _caldav(args: dict[str, Any], work: Any) -> Any:
    """Runs `work(principal)` for the account in a worker thread (caldav is blocking)."""
    creds = await _credentials(_account(args))
    return await asyncio.to_thread(lambda: work(_principal(creds)))


def _error(action: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ModuleNotFoundError):
        return {"text": "CalDAV is not available in this build of Caroline (the caldav library is missing).", "is_error": True}
    return {"text": f"{action} failed: {type(exc).__name__}: {exc}", "is_error": True}


async def calendar_connect(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    try:
        account = _account(args)
        creds = {k: str(args.get(k) or "").strip() for k in ("url", "username", "password")}
        if not all(creds.values()):
            return {"text": 'calendar_connect needs "url" (the CalDAV address), "username" and "password" (an app password).',
                    "is_error": True}
        names = await asyncio.to_thread(lambda: [c.name for c in _principal(creds).calendars()])
    except Exception as exc:  # noqa: BLE001 -- reported, logged
        log_event("plugin:calendar", "connect_failed", account=args.get("account"), error=str(exc)[:300])
        return _error("Connecting the calendar", exc)
    _remember(account, creds)
    log_event("plugin:calendar", "connected", account=account, calendars=len(names))
    return {"text": f'Connected the "{account}" calendar account: {len(names)} calendar(s): {", ".join(n or "(unnamed)" for n in names)}.'}


async def calendar_list(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    try:
        rows = await _caldav(args, lambda p: [{"name": c.name, "url": str(c.url)} for c in p.calendars()])
    except Exception as exc:  # noqa: BLE001
        return _error("Listing calendars", exc)
    return {"text": json.dumps(rows, ensure_ascii=False)}


async def calendar_events(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    from datetime import datetime

    try:
        start = datetime.fromisoformat(str(args["start"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(args["end"]).replace("Z", "+00:00"))
    except Exception:
        return {"text": 'calendar_events needs "start" and "end" (ISO 8601).', "is_error": True}
    try:
        raw = await _caldav(args, lambda p: [e.data for e in _pick_calendar(p, args.get("calendar"))
                                             .search(start=start, end=end, event=True, expand=True)])
        parsed = await asyncio.gather(*(_parse_ics(text.encode("utf-8") if isinstance(text, str) else text) for text in raw))
    except Exception as exc:  # noqa: BLE001
        return _error("Reading the calendar", exc)
    events = [event for one in parsed for event in one["events"]]
    events.sort(key=lambda e: str((e.get("start") or {}).get("datetime") or (e.get("start") or {}).get("date") or ""))
    events, _cut = cap_count(events)
    log_event("plugin:calendar", "events_read", account=args.get("account") or "self", count=len(events))
    return {"text": f"{len(events)} event(s) from {args['start']} to {args['end']}:\n" + json.dumps(events, ensure_ascii=False, indent=1)}


async def calendar_add_event(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    try:
        made = await _make_ics(args)
        text = base64.b64decode(made["file"]["data"]).decode("utf-8")
        # A calendar object resource carries no METHOD (RFC 4791 4.1); the server sends invitations itself.
        text = re.sub(r"(?m)^METHOD:[^\r\n]*\r?\n", "", text)
        await _caldav(args, lambda p: _pick_calendar(p, args.get("calendar")).save_event(text))
    except Exception as exc:  # noqa: BLE001
        log_event("plugin:calendar", "add_failed", account=args.get("account") or "self", error=str(exc)[:300])
        return _error("Adding the event", exc)
    log_event("plugin:calendar", "event_added", account=args.get("account") or "self", uid=made.get("uid"))
    return {"text": f"Added \"{args['title']}\" to the calendar (uid {made.get('uid')})."}


async def calendar_delete_event(args: dict[str, Any], _rp: Any) -> dict[str, Any]:
    uid = str(args.get("uid") or "").strip()
    if not uid:
        return {"text": 'calendar_delete_event needs "uid" (from calendar_events).', "is_error": True}
    try:
        await _caldav(args, lambda p: _pick_calendar(p, args.get("calendar")).event_by_uid(uid).delete())
    except Exception as exc:  # noqa: BLE001
        log_event("plugin:calendar", "delete_failed", account=args.get("account") or "self", uid=uid, error=str(exc)[:300])
        return _error("Deleting the event", exc)
    log_event("plugin:calendar", "event_deleted", account=args.get("account") or "self", uid=uid)
    return {"text": f"Deleted the event {uid}."}


_EVENT_ARGS = {
    "title": str, "start": str, "end": str | None, "durationMinutes": int | None, "tzid": str | None,
    "allDay": bool | None, "description": str | None, "location": str | None, "attendees": list | None,
    "organizer": str | None, "rrule": str | None, "remindersMinutes": list | None, "uid": str | None, "url": str | None,
}

_EVENT_HELP = (
    "`start`/`end` ISO 8601; with `tzid` (an IANA zone like \"Europe/Moscow\") a time without an offset is read in "
    "that zone -- always give the zone the event really happens in. No `end`: `durationMinutes`, else one hour. "
    "`allDay`: a whole-day event, `end` being its last day. `attendees`: e-mails (or {\"email\",\"name\"}); with an "
    "`organizer` too it is an invitation. `rrule` RFC 5545 recurrence (\"FREQ=WEEKLY;COUNT=5\"). `remindersMinutes`: "
    "alarms, minutes before. `uid`: the uid of an event made before, to update it."
)

PLUGIN = Plugin(
    name="calendar",
    tools=[
        PluginTool("calendar_make_ics",
                   "Makes an event or an invitation as a .ics file (to attach to a letter, or to import into any "
                   "calendar) and saves it -- under the workspace's calendar folder, or at `savePath`. " + _EVENT_HELP,
                   {**_EVENT_ARGS, "savePath": str | None}, calendar_make_ics),
        PluginTool("calendar_read_ics",
                   "Reads a .ics file -- an invitation that came with a letter (download the attachment first), a "
                   "cancellation, an exported calendar: what it is, and every event with its time, place, organizer, "
                   "attendees and their answers. `path` is the local file.",
                   {"path": str}, calendar_read_ics),
        PluginTool("calendar_connect",
                   "Connects a CalDAV calendar account (iCloud, Yandex, Fastmail, Nextcloud...). `account`: \"self\" "
                   "(Caroline's own calendar, default) or \"owner\" (the user's). `url` the CalDAV address, `username`, "
                   "`password` -- an app password. Kept for later calls.",
                   {"account": str | None, "url": str, "username": str, "password": str}, calendar_connect),
        PluginTool("calendar_list", "Lists the calendars of a connected account. `account`: \"self\" (default) or \"owner\".",
                   {"account": str | None}, calendar_list),
        PluginTool("calendar_events",
                   "Events of a calendar from `start` to `end` (ISO 8601), recurring ones expanded. `calendar`: its name "
                   "when the account has several. `account`: \"self\" (default) or \"owner\".",
                   {"start": str, "end": str, "calendar": str | None, "account": str | None}, calendar_events),
        PluginTool("calendar_add_event",
                   "Adds an event to a calendar of a connected account (with attendees and an organizer, the calendar "
                   "server sends the invitations). `calendar`: its name when there are several. `account`: \"self\" "
                   "(default) or \"owner\" -- add to the owner's calendar only when the user asked for it. " + _EVENT_HELP,
                   {**_EVENT_ARGS, "calendar": str | None, "account": str | None}, calendar_add_event),
        PluginTool("calendar_delete_event",
                   "Deletes an event, by its `uid` (from calendar_events). `calendar`: its name when there are several. "
                   "`account`: \"self\" (default) or \"owner\" -- delete from the owner's calendar only when the user asked.",
                   {"uid": str, "calendar": str | None, "account": str | None}, calendar_delete_event),
    ],
)
