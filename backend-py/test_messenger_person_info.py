"""Who-is-this and members tools of every messenger (2026-10-23: Caroline
could not get a phone number, a user id or a username out of a
conversation). Fake clients only -- no live account is touched.
Run with Caroline's bundled Python (the plugins import claude_agent_sdk)."""

import asyncio
from datetime import datetime, timezone

import pytest
from telethon.tl.types import Channel, User, UserStatusOffline, Username

from app import telegram_channel
from app.plugins import messenger_plugin, signal_plugin, slack_plugin, telegram_plugin, whatsapp_plugin


def run(coro):
    return asyncio.run(coro)


ANNA = User(id=42, first_name="Anna", last_name="Petrova", username="annap", phone="15551234567", contact=True,
            mutual_contact=True, bot=False, status=UserStatusOffline(was_online=datetime(2026, 10, 22, tzinfo=timezone.utc)))


def test_telegram_entity_fields_of_a_user_and_a_group():
    fields = telegram_channel.entity_fields(ANNA)
    assert fields["id"] == 42 and fields["username"] == "@annap" and fields["phone"] == "+15551234567"
    assert fields["firstName"] == "Anna" and fields["lastName"] == "Petrova" and fields["lastSeen"].startswith("2026-10-22")
    group = Channel(id=777, title="Team", photo=None, date=datetime.now(), megagroup=True, username=None,
                    usernames=[Username(username="team_x", active=True)], participants_count=12)
    assert telegram_channel.entity_fields(group) == {"id": -1000000000777, "kind": "group", "title": "Team", "username": "@team_x", "membersCount": 12}


def test_telegram_entity_fields_leave_out_what_telegram_hides():
    hidden = User(id=7, first_name="Ivan", status=None)
    assert telegram_channel.entity_fields(hidden) == {"id": 7, "kind": "user", "firstName": "Ivan"}


class FakeTelegramClient:
    def __init__(self, entity, members=()):
        self.entity, self.members, self.requests = entity, list(members), []

    async def get_entity(self, target):
        self.requests.append(("get_entity", target))
        return self.entity

    async def __call__(self, request):
        class Full:
            full_user = type("U", (), {"about": "Nurse in Kazan", "common_chats_count": 3})()
        return Full()

    async def iter_participants(self, entity):
        for member in self.members:
            yield member


@pytest.fixture
def telegram(monkeypatch):
    def use(client):
        monkeypatch.setattr(telegram_plugin, "_not_ready", lambda account: None)
        monkeypatch.setattr(telegram_plugin, "get_live_client", lambda account: client)
        return client
    return use


def test_telegram_get_info_by_username_adds_the_profile_and_changes_nothing(telegram):
    client = telegram(FakeTelegramClient(ANNA))
    text = run(telegram_plugin.telegram_get_info({"who": "@annap"}, None))["text"]
    assert "'phone': '+15551234567'" in text and "'about': 'Nurse in Kazan'" in text and "'commonChatsCount': 3" in text
    assert client.requests == [("get_entity", "annap")]


def test_telegram_get_info_by_phone_does_not_add_a_contact(telegram):
    client = telegram(FakeTelegramClient(ANNA))
    run(telegram_plugin.telegram_get_info({"who": "+1 555 123 4567"}, None))
    assert client.requests == [("get_entity", "+1 555 123 4567")]      # looked up, never imported as a contact


def test_telegram_list_members(telegram):
    telegram(FakeTelegramClient(None, members=[ANNA, User(id=8, first_name="Bob", username="bobb")]))
    text = run(telegram_plugin.telegram_list_members({"chat": "-1000000000777"}, None))["text"]
    assert text.startswith("2 member(s)") and "@annap" in text and "@bobb" in text


def test_whatsapp_get_info_shows_a_group_and_its_members(monkeypatch):
    async def fake_get_info(account, jid):
        assert jid == "123@g.us"
        return {"kind": "group", "subject": "Family", "participants": [{"id": "1555@s.whatsapp.net", "phone": "+1555", "admin": "admin"}]}
    monkeypatch.setattr(whatsapp_plugin, "get_info", fake_get_info)
    text = run(whatsapp_plugin.whatsapp_list_members({"group": "123@g.us"}, None))["text"]
    assert "'subject': 'Family'" in text and "members: [{'id': '1555@s.whatsapp.net', 'phone': '+1555'" in text


def test_whatsapp_get_info_turns_a_phone_number_into_a_jid(monkeypatch):
    seen = []

    async def fake_get_info(account, jid):
        seen.append(jid)
        return {"kind": "user", "jid": jid, "phone": "+15551234567"}
    monkeypatch.setattr(whatsapp_plugin, "get_info", fake_get_info)
    run(whatsapp_plugin.whatsapp_get_info({"who": "+1 555 123 4567"}, None))
    assert seen == ["15551234567@s.whatsapp.net"]


def test_messenger_history_names_its_senders(monkeypatch):
    async def history(account, thread_id, amount):
        return [{"messageID": "m1", "senderID": "100", "text": "hi"}, {"messageID": "m2", "senderID": "200", "text": "yo"}]

    async def users(account, ids):
        assert ids == ["100", "200"]
        return {"100": {"name": "Anna Petrova"}}
    monkeypatch.setattr(messenger_plugin, "read_thread_history", history)
    monkeypatch.setattr(messenger_plugin, "get_user_info", users)
    text = run(messenger_plugin.messenger_read_history({"threadId": "t", "amount": 2}, None))["text"]
    assert "'senderName': 'Anna Petrova'" in text and text.count("senderName") == 1


def test_messenger_get_info_by_name_returns_candidates(monkeypatch):
    async def find(account, name):
        return [{"userID": "100", "name": "Anna Petrova"}]
    monkeypatch.setattr(messenger_plugin, "find_user", find)
    text = run(messenger_plugin.messenger_get_info({"who": "Anna Petrova"}, None))["text"]
    assert "Candidates" in text and "'userID': '100'" in text


def test_signal_list_members_finds_the_group(monkeypatch):
    async def groups(account):
        return [{"id": "g1", "name": "Team", "members": [{"number": "+1555", "uuid": "u-1"}]}]
    monkeypatch.setattr(signal_plugin, "list_groups", groups)
    assert "'uuid': 'u-1'" in run(signal_plugin.signal_list_members({"groupId": "g1"}, None))["text"]
    assert run(signal_plugin.signal_list_members({"groupId": "nope"}, None))["is_error"]


def test_slack_find_person_reads_every_page(monkeypatch):
    class Client:
        async def users_list(self, cursor=None):
            if cursor is None:
                return {"members": [{"id": "U1", "profile": {"real_name": "Anna"}}], "response_metadata": {"next_cursor": "p2"}}
            return {"members": [{"id": "U2", "profile": {"real_name": "Zoe Late"}}], "response_metadata": {"next_cursor": ""}}

    async def by_name(query, rows):
        return [row for row in rows if query in row["name"]]
    monkeypatch.setattr(slack_plugin, "_client_or_error", lambda account: (Client(), None))
    monkeypatch.setattr(slack_plugin, "find_by_name", by_name)
    assert "U2" in run(slack_plugin.slack_find_person({"query": "Zoe"}, None))["text"]


def test_slack_person_row_keeps_phone_and_title():
    row = slack_plugin._person_row({"id": "U1", "name": "anna", "tz": "Europe/Moscow",
                                    "profile": {"real_name": "Anna Petrova", "phone": "+1555", "title": "CTO", "email": "a@x.org"}})
    assert row == {"id": "U1", "username": "anna", "realName": "Anna Petrova", "title": "CTO", "email": "a@x.org",
                   "phone": "+1555", "timeZone": "Europe/Moscow"}
