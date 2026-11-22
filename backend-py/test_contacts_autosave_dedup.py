"""The email autosave writes an address to the paid address book once per name, not with every mail read.
Run: python -m pytest test_contacts_autosave_dedup.py -q"""
import asyncio

from app.plugins import contacts_plugin as cp


def test_known_address_is_not_written_again(monkeypatch, tmp_path):
    puts = []

    async def fake_book(command, doing, contact):
        puts.append(contact)
        return {"ok": True}

    monkeypatch.setattr(cp, "_address_book", fake_book)
    monkeypatch.setattr(cp, "_autosaved_path", lambda: tmp_path / "contacts-autosaved.json")
    monkeypatch.setattr(cp, "_autosaved", None)

    asyncio.run(cp.upsert_contacts_from_emails([("Anna", "anna@x.org"), (None, "bob@x.org")]))
    asyncio.run(cp.upsert_contacts_from_emails([("Anna", "Anna@x.org"), (None, "bob@x.org"), ("", "bob@x.org")]))
    assert [c["emails"][0]["value"] for c in puts] == ["anna@x.org", "bob@x.org"]

    # A new name for a known address still goes to the server (it merges by meaning).
    asyncio.run(cp.upsert_contacts_from_emails([("Anna Petrova", "anna@x.org"), ("Bob Stone", "bob@x.org")]))
    assert [c["name"] for c in puts[2:]] == ["Anna Petrova", "Bob Stone"]

    # Survives a restart.
    monkeypatch.setattr(cp, "_autosaved", None)
    asyncio.run(cp.upsert_contacts_from_emails([("Anna Petrova", "anna@x.org")]))
    assert len(puts) == 4


def test_failed_write_is_tried_again(monkeypatch, tmp_path):
    puts = []

    async def failing(command, doing, contact):
        puts.append(contact)
        return {"is_error": True, "text": "no funds"}

    monkeypatch.setattr(cp, "_address_book", failing)
    monkeypatch.setattr(cp, "_autosaved_path", lambda: tmp_path / "contacts-autosaved.json")
    monkeypatch.setattr(cp, "_autosaved", None)
    asyncio.run(cp.upsert_contacts_from_emails([("Anna", "anna@x.org")]))
    asyncio.run(cp.upsert_contacts_from_emails([("Anna", "anna@x.org")]))
    assert len(puts) == 2
