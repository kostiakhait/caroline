"""Messages in Caroline's own chats go to a background session (app/main.py). Run: python -m pytest test_own_chat_routing.py -q"""

import asyncio

import pytest

main = pytest.importorskip("app.main")

OWN = ('[New WhatsApp message in your own WhatsApp account, from "Anna" (jid=1):\nhi\n'
       "This is Caroline's own separate WhatsApp account, not the user's -- replying here is Caroline's own correspondence.]")
OWNER = ('[New WhatsApp message in the owner\'s WhatsApp account, from "Anna" (jid=1):\nhi\n'
         "This is the OWNER'S OWN correspondence, not yours -- read it and tell the owner.]")


def test_own_account_notes_are_recognised():
    assert main.is_own_account_note(OWN)
    assert not main.is_own_account_note(OWNER)


def test_own_chat_goes_to_a_background_session_and_the_owners_chat_stays_in_the_primary(monkeypatch):
    calls = []

    class Primary:
        tab_id = "1"

        def inject_proactive(self, text, **kw):
            calls.append(("primary", text))
            return True

    class Pseudo:
        tab_id = "1-bg-x"

        def inject_proactive(self, text, **kw):
            calls.append(("pseudo", text))
            return True

    async def fake_create(real, activity, getter):
        return Pseudo()

    monkeypatch.setattr(main, "primary_session", lambda: Primary())
    monkeypatch.setattr(main.pseudo_tab, "get_or_create_pseudo_session", fake_create)

    async def run():
        main._inject_messenger_proactive(OWN)
        main._inject_messenger_proactive(OWNER)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    kinds = [k for k, _ in calls]
    assert kinds == ["pseudo", "primary"] or sorted(kinds) == ["primary", "pseudo"]
    pseudo_text = next(t for k, t in calls if k == "pseudo")
    assert "answer the correspondent yourself" in pseudo_text and "ONE short line" in pseudo_text
    assert "1-bg-x" in main.pseudo_tab._pseudo_turn_always_relay
