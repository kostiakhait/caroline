"""The calendar plugin (app/plugins/calendar_plugin.py) against a fake server and
a fake CalDAV account -- no network, no real calendar. Run with Caroline's
bundled Python."""

import asyncio
import base64

import pytest

from app.plugins import calendar_plugin as c

INVITE = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Camerlengo//EN\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\n"
    "UID:u1@camerlengo\r\nSUMMARY:Planning\r\nDTSTART:20261103T120000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def server(monkeypatch, tmp_path):
    sent = []

    async def call_v2(command, **extra):
        sent.append((command, extra))
        if command == "misc:addCalendar":
            return {"file": {"name": "x.ics", "data": base64.b64encode(INVITE.encode()).decode()}, "uid": "u1@camerlengo", "invitation": True}
        if command == "misc:parseCalendar":
            text = base64.b64decode(extra["data"]).decode()
            return {"method": "REQUEST" if "METHOD:REQUEST" in text else None,
                    "events": [{"uid": "u1@camerlengo", "summary": "Planning", "start": {"datetime": "2026-11-03T12:00:00+00:00"}}]}
        raise AssertionError(command)

    monkeypatch.setattr(c, "call_v2", call_v2)
    monkeypatch.setattr(c, "ICS_DIR", tmp_path)
    return sent


def test_make_ics_sends_the_event_and_saves_the_file(server, tmp_path):
    text = run(c.calendar_make_ics({"title": "Planning", "start": "2026-11-03T15:00", "tzid": "Europe/Moscow",
                                    "attendees": ["a@example.com"], "organizer": "me@example.com"}, None))["text"]
    command, sent = server[0]
    assert command == "misc:addCalendar" and sent["time"] == "2026-11-03T15:00" and sent["tzid"] == "Europe/Moscow"
    assert "start" not in sent and "an invitation" in text
    assert (tmp_path / "Planning.ics").read_bytes() == INVITE.encode()


def test_make_ics_needs_a_title_and_a_start(server):
    assert run(c.calendar_make_ics({"title": "x"}, None))["is_error"]


def test_read_ics_says_what_the_file_is(server, tmp_path):
    path = tmp_path / "invite.ics"
    path.write_text(INVITE)
    text = run(c.calendar_read_ics({"path": str(path)}, None))["text"]
    assert "an invitation (METHOD:REQUEST), 1 event(s)" in text and "Planning" in text


class FakeEvent:
    def __init__(self, data):
        self.data, self.deleted = data, False

    def delete(self):
        self.deleted = True


class FakeCalendar:
    def __init__(self, name):
        self.name, self.url, self.saved, self.events = name, f"https://dav/{name}", [], [FakeEvent(INVITE)]

    def search(self, **kwargs):
        assert kwargs["event"] and kwargs["expand"]
        return self.events

    def save_event(self, text):
        self.saved.append(text)

    def event_by_uid(self, uid):
        return self.events[0]


class FakePrincipal:
    def __init__(self, names):
        self.list = [FakeCalendar(n) for n in names]

    def calendars(self):
        return self.list


@pytest.fixture
def account(monkeypatch, server):
    principal = FakePrincipal(["Home"])

    async def credentials(name):
        return {"url": "https://dav", "username": "u", "password": "p"}

    monkeypatch.setattr(c, "_credentials", credentials)
    monkeypatch.setattr(c, "_principal", lambda creds: principal)
    return principal


def test_events_are_read_and_parsed_by_the_server(account, server):
    text = run(c.calendar_events({"start": "2026-11-01T00:00", "end": "2026-11-30T00:00"}, None))["text"]
    assert text.startswith("1 event(s)") and "Planning" in text and server[-1][0] == "misc:parseCalendar"


def test_add_event_strips_method_before_saving(account):
    run(c.calendar_add_event({"title": "Planning", "start": "2026-11-03T15:00"}, None))
    saved = account.list[0].saved[0]
    assert "METHOD:" not in saved and "BEGIN:VEVENT" in saved


def test_delete_event_by_uid(account):
    run(c.calendar_delete_event({"uid": "u1@camerlengo"}, None))
    assert account.list[0].events[0].deleted


def test_several_calendars_and_none_named_asks_which(monkeypatch, account):
    two = FakePrincipal(["Home", "Work"])
    monkeypatch.setattr(c, "_principal", lambda creds: two)
    text = run(c.calendar_events({"start": "2026-11-01T00:00", "end": "2026-11-30T00:00"}, None))["text"]
    assert "several calendars" in text and "Home, Work" in text


def test_an_unknown_account_is_refused(account):
    assert "must be one of self, owner" in run(c.calendar_list({"account": "boss"}, None))["text"]


def test_not_connected_says_how_to_connect(monkeypatch, server, tmp_path):
    monkeypatch.setattr(c, "_load_local", lambda account: None)

    async def no_note(account):
        return None, None

    monkeypatch.setattr(c, "_find_vault_note", no_note)
    text = run(c.calendar_list({"account": "owner"}, None))["text"]
    assert 'calendar_connect(account="owner"' in text
