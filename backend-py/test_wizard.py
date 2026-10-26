"""The start-up wizard's backend (app/wizard.py): what it asks, what it remembers, what it skips.
Run: python -m pytest test_wizard.py -q"""

import asyncio
import json

import pytest

from app import settings_mirror, wizard


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings_mirror, "schedule_push", lambda *_a, **_k: None)
    state = {"logged_in": True, "vault": [], "claude": False, "openai": False}
    monkeypatch.setattr(wizard, "is_logged_in", lambda: state["logged_in"])
    monkeypatch.setattr(wizard, "logged_in_email", lambda: "owner@example.com" if state["logged_in"] else None)

    async def vault():
        return list(state["vault"])

    async def claude(wait_s=8.0):
        return state["claude"]

    monkeypatch.setattr(wizard, "_vault_mailboxes", vault)
    monkeypatch.setattr(wizard, "_claude_signed_in", claude)
    monkeypatch.setattr(wizard, "_openai_signed_in", lambda _ws: state["openai"])
    return state


def run(coro):
    return asyncio.run(coro)


def test_without_a_login_only_the_account_step_is_open_and_the_wizard_is_needed(tmp_path, env):
    env["logged_in"] = False
    s = run(wizard.get_state(str(tmp_path)))
    assert s["needed"] and s["steps"][0] == {"id": "account", "status": "todo"} and s["loggedIn"] is False


def test_a_fresh_login_asks_for_everything_still_missing(tmp_path, env):
    s = run(wizard.get_state(str(tmp_path)))
    assert [x["id"] for x in s["steps"]] == ["account", "subscriptions", "mailboxes", "messengers"]
    assert [x["status"] for x in s["steps"]] == ["done", "todo", "todo", "todo"]
    assert s["needs"] == {"caroline_mail": True, "owner_mail": True, "caroline_messengers": True, "owner_messengers": True}
    assert s["needed"] is True


def test_mailboxes_found_in_notes_are_offered_not_asked_again(tmp_path, env):
    env["vault"] = ["cat@navlink.net", "kostia.khait@gmail.com"]
    s = run(wizard.get_state(str(tmp_path)))
    assert s["mailboxes"]["unassigned"] == ["cat@navlink.net", "kostia.khait@gmail.com"]
    assert wizard.answer(str(tmp_path), "emailRole", {"address": "cat@navlink.net", "role": "caroline"})["ok"]
    s = run(wizard.get_state(str(tmp_path)))
    assert s["mailboxes"]["unassigned"] == ["kostia.khait@gmail.com"] and s["needs"]["caroline_mail"] is False


def test_an_existing_subscription_counts_as_answered(tmp_path, env):
    env["claude"] = True
    assert run(wizard.get_state(str(tmp_path)))["steps"][1]["status"] == "done"


def test_everything_answered_means_all_steps_done_and_completed_ends_the_wizard(tmp_path, env):
    ws = str(tmp_path)
    wizard.answer(ws, "subscriptionsAnswered", True)
    for need in wizard.NEEDS:
        assert wizard.answer(ws, "decline", need)["ok"]
    s = run(wizard.get_state(ws))
    assert [x["status"] for x in s["steps"]] == ["done", "done", "done", "done"] and s["needed"] is True   # not finished until the user says so
    wizard.answer(ws, "completed", True)
    assert run(wizard.get_state(ws))["needed"] is False


def test_finish_later_stops_the_nagging_but_not_before_a_login(tmp_path, env):
    wizard.answer(str(tmp_path), "dismissed", True)
    assert run(wizard.get_state(str(tmp_path)))["needed"] is False
    env["logged_in"] = False
    assert run(wizard.get_state(str(tmp_path)))["needed"] is True


def test_mailboxes_and_messengers_are_separate_steps(tmp_path, env):
    ws = str(tmp_path)
    wizard.answer(ws, "decline", "caroline_mail")
    wizard.answer(ws, "decline", "owner_mail")
    status = {x["id"]: x["status"] for x in run(wizard.get_state(ws))["steps"]}
    assert status["mailboxes"] == "done" and status["messengers"] == "todo"


def test_the_login_answer_does_not_wait_for_the_claude_status(tmp_path, env, monkeypatch):
    seen = []

    async def claude(wait_s=8.0):
        seen.append(wait_s)
        return False

    monkeypatch.setattr(wizard, "_claude_signed_in", claude)
    run(wizard.get_state(str(tmp_path), quick=True))
    run(wizard.get_state(str(tmp_path)))
    assert seen == [0.3, 8.0]


def test_messenger_handles_are_kept_per_role(tmp_path, env):
    ws = str(tmp_path)
    assert wizard.answer(ws, "contact", {"role": "owner", "kind": "telegram", "handle": " @kostia "})["ok"]
    s = run(wizard.get_state(ws))
    assert s["contacts"] == {"owner": {"telegram": "@kostia"}}
    assert s["needs"]["owner_messengers"] is False and s["needs"]["caroline_messengers"] is True


def test_bad_answers_are_refused_and_change_nothing(tmp_path, env):
    ws = str(tmp_path)
    assert wizard.answer(ws, "emailRole", {"address": "not-an-address", "role": "caroline"})["ok"] is False
    assert wizard.answer(ws, "emailRole", {"address": "a@b.co", "role": "stranger"})["ok"] is False
    assert wizard.answer(ws, "contact", {"role": "owner", "kind": "icq", "handle": "1"})["ok"] is False
    assert wizard.answer(ws, "decline", "something_else")["ok"] is False
    assert wizard.answer(ws, "rm -rf", True)["ok"] is False
    assert wizard.load(ws) == {}


def test_login_with_bad_input_never_reaches_the_server(tmp_path, env):
    assert run(wizard.login(str(tmp_path), "nope", "pw", False)) == {"ok": False, "error": "invalid_input"}
    assert run(wizard.login(str(tmp_path), "a@b.co", "", False)) == {"ok": False, "error": "invalid_input"}


def test_login_syncs_settings_before_it_answers(tmp_path, env, monkeypatch):
    calls = []

    class R:
        ok = True
        error = None

    async def verify(email, password):
        calls.append(("verify", email))
        return R()

    async def sync(ws):
        calls.append(("sync", ws))
        return {"ok": True, "restored": ["persona"], "written": []}

    monkeypatch.setattr(wizard, "verify_and_save_login", verify)
    monkeypatch.setattr(settings_mirror, "sync_now", sync)
    out = run(wizard.login(str(tmp_path), " Me@Example.com ", "pw", False))
    assert out["ok"] and out["restored"] == ["persona"] and out["state"]["loggedIn"]
    assert [c[0] for c in calls] == ["verify", "sync"]


def test_a_refused_login_does_not_sync(tmp_path, env, monkeypatch):
    class R:
        ok = False
        error = "bad password"

    async def verify(email, password):
        return R()

    async def sync(ws):
        raise AssertionError("must not sync")

    monkeypatch.setattr(wizard, "verify_and_save_login", verify)
    monkeypatch.setattr(settings_mirror, "sync_now", sync)
    assert run(wizard.login(str(tmp_path), "a@b.co", "pw", False)) == {"ok": False, "error": "bad password"}


def test_removing_a_messenger_handle(tmp_path, env):
    ws = str(tmp_path)
    wizard.answer(ws, "contact", {"role": "owner", "kind": "telegram", "handle": "@kostia"})
    wizard.answer(ws, "contact", {"role": "owner", "kind": "signal", "handle": "+1555"})
    assert wizard.answer(ws, "removeContact", {"role": "owner", "kind": "telegram"})["ok"]
    assert run(wizard.get_state(ws))["contacts"] == {"owner": {"signal": "+1555"}}
    assert wizard.answer(ws, "removeContact", {"role": "owner", "kind": "icq"})["ok"] is False
    assert wizard.answer(ws, "removeContact", {"role": "caroline", "kind": "signal"})["ok"]     # nothing there: no error


def test_a_mailbox_added_here_is_forgotten_when_removed(tmp_path, env, monkeypatch):
    from app.plugins import email_credentials, email_plugin

    ws = str(tmp_path)
    kept = []

    async def fake_list(args, _rp):
        return {"text": "{}"}

    async def fake_forget(address):
        kept.append(address)
        return {"local": True, "note": True}

    monkeypatch.setattr(email_plugin, "email_list_folders", fake_list)
    monkeypatch.setattr(email_credentials, "forget", fake_forget)
    body = {"address": "New@Example.com", "password": "pw", "role": "caroline", "imapHost": "imap.example.com"}
    assert run(wizard.save_mailbox(ws, body))["ok"]
    s = run(wizard.get_state(ws))
    assert s["mailboxes"]["roles"] == {"new@example.com": "caroline"} and s["mailboxes"]["createdHere"] == ["new@example.com"]
    out = run(wizard.remove_mailbox(ws, "new@example.com"))
    assert out == {"ok": True, "credentialsDeleted": True} and kept == ["new@example.com"]
    s = run(wizard.get_state(ws))
    assert s["mailboxes"]["roles"] == {} and s["mailboxes"]["createdHere"] == []


def test_a_mailbox_that_was_already_in_the_notes_is_only_unassigned(tmp_path, env, monkeypatch):
    from app.plugins import email_credentials

    ws = str(tmp_path)
    env["vault"] = ["cat@navlink.net"]
    wizard.answer(ws, "emailRole", {"address": "cat@navlink.net", "role": "caroline"})

    async def never(address):
        raise AssertionError("must not delete credentials the wizard did not add")

    monkeypatch.setattr(email_credentials, "forget", never)
    assert run(wizard.remove_mailbox(ws, "cat@navlink.net")) == {"ok": True, "credentialsDeleted": False}
    assert run(wizard.get_state(ws))["mailboxes"]["unassigned"] == ["cat@navlink.net"]


def test_removing_an_unknown_mailbox_is_refused(tmp_path, env):
    assert run(wizard.remove_mailbox(str(tmp_path), "nobody@example.com")) == {"ok": False, "error": "unknown_mailbox"}


def test_a_mailbox_that_does_not_answer_is_not_recorded(tmp_path, env, monkeypatch):
    from app.plugins import email_plugin

    async def failing(args, _rp):
        raise RuntimeError("login refused")

    monkeypatch.setattr(email_plugin, "email_list_folders", failing)
    body = {"address": "x@example.com", "password": "pw", "role": "owner", "imapHost": "imap.example.com"}
    out = run(wizard.save_mailbox(str(tmp_path), body))
    assert out["ok"] is False and out["error"] == "mailbox_check_failed" and "pw" not in json.dumps(out)
    assert wizard.load(str(tmp_path)) == {}


def test_the_wizard_file_is_what_the_mirror_carries(tmp_path):
    assert any(i.filename == "wizard.json" and not i.all_secret and not i.secret_keys for i in settings_mirror.STATIC_ITEMS)
