"""Signed in with a session handed over by the desktop host (app/external_session.py), not with a password.
Run: python -m pytest test_external_session.py -q"""

import asyncio
import importlib

import pytest

from app import external_session
from app.plugins import notes_api, sw_api


@pytest.fixture
def session_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("CAROLINE_SW_USER", "Alice@Example.com")
    monkeypatch.setenv("CAROLINE_SW_SESSION", "sess-1")
    monkeypatch.setattr(external_session, "_LEFT_MARKER", tmp_path / "left")
    monkeypatch.setattr(sw_api, "CREDENTIALS_PATH", tmp_path / "credentials.json")
    external_session._load_from_env()
    yield external_session
    for name in ("CAROLINE_SW_USER", "CAROLINE_SW_SESSION"):
        monkeypatch.delenv(name, raising=False)
    external_session._load_from_env()


def test_without_the_variables_nothing_changes(monkeypatch, tmp_path):
    monkeypatch.delenv("CAROLINE_SW_USER", raising=False)
    monkeypatch.delenv("CAROLINE_SW_SESSION", raising=False)
    monkeypatch.setattr(external_session, "_LEFT_MARKER", tmp_path / "left")
    monkeypatch.setattr(sw_api, "CREDENTIALS_PATH", tmp_path / "credentials.json")
    external_session._load_from_env()
    assert not external_session.active()
    assert sw_api.load_credentials() is None
    sw_api.save_credentials("a@b.c", "pw")
    assert sw_api.load_credentials() == {"email": "a@b.c", "password": "pw"}


def test_the_user_is_logged_in_with_an_empty_password(session_mode):
    assert session_mode.active()
    assert sw_api.load_credentials() == {"email": "alice@example.com", "password": ""}


def test_a_credentialless_call_gets_the_session(session_mode):
    assert asyncio.run(sw_api.mint_v2_session("alice@example.com", "")) == "sess-1"
    assert asyncio.run(notes_api.verify_password("ALICE@example.com", "")) == "sess-1"


def test_a_real_password_still_goes_to_camerlengo(session_mode, monkeypatch):
    sent = []

    async def fake_post(body, **_kw):
        sent.append(body)
        return {".status": "ok", "session": "fresh"}

    monkeypatch.setattr(sw_api, "_post_json", fake_post)
    assert asyncio.run(sw_api.mint_v2_session("alice@example.com", "typed-pw")) == "fresh"
    assert sent and sent[0]["command"] == "user:verify" and sent[0]["password"] == "typed-pw"


def test_another_user_is_not_given_the_session(session_mode, monkeypatch):
    async def fake_post(body, **_kw):
        return {".status": "error", ".reason": "Invalid username or password"}

    monkeypatch.setattr(sw_api, "_post_json", fake_post)
    with pytest.raises(sw_api.SwApiError):
        asyncio.run(sw_api.mint_v2_session("bob@example.com", ""))


def test_a_password_login_takes_over_from_the_session(session_mode):
    sw_api.save_credentials("alice@example.com", "typed-pw")
    assert not session_mode.active()
    assert sw_api.load_credentials() == {"email": "alice@example.com", "password": "typed-pw"}


def test_the_session_is_never_written_to_disk(session_mode, tmp_path):
    sw_api.load_credentials()
    assert not (tmp_path / "credentials.json").exists()


def test_refresh_keeps_the_session(session_mode, monkeypatch):
    sent = []

    async def fake_post(body, **_kw):
        sent.append(body)
        return {".status": "ok", "user": "alice@example.com", "session": "sess-1"}

    monkeypatch.setattr(sw_api, "_post_json", fake_post)
    assert asyncio.run(session_mode.refresh_once()) is True
    assert sent == [{"command": "user:refreshSession", "session": "sess-1"}]
    assert session_mode.active()


def test_a_rejected_session_leaves_the_mode(session_mode, monkeypatch):
    async def fake_post(body, **_kw):
        return {".status": "error", ".errcode": "997", ".reason": "Missing or invalid session"}

    monkeypatch.setattr(sw_api, "_post_json", fake_post)
    assert asyncio.run(session_mode.refresh_once()) is False
    assert not session_mode.active()
    assert sw_api.load_credentials() is None  # the usual login window takes it from here


def test_an_unreachable_camerlengo_keeps_the_session(session_mode, monkeypatch):
    async def fake_post(body, **_kw):
        raise OSError("network down")

    monkeypatch.setattr(sw_api, "_post_json", fake_post)
    assert asyncio.run(session_mode.refresh_once()) is True
    assert session_mode.active()


def test_a_left_mode_is_not_re_entered_after_a_restart(session_mode):
    session_mode.leave("rejected")
    session_mode._load_from_env()  # the backend restarted; the environment still holds the old session
    assert not session_mode.active()


def test_logging_out_leaves_the_mode(session_mode, monkeypatch, tmp_path):
    from app import login_api
    login_api.clear_credentials()
    assert not session_mode.active()
    assert sw_api.load_credentials() is None


def test_a_session_error_in_a_call_checks_the_session_at_once(session_mode, monkeypatch):
    async def go():
        async def fake_post(body, **_kw):
            if body["command"] == "user:refreshSession":
                return {".status": "error", ".reason": "Missing or invalid session"}
            return {".status": "error", ".reason": "Missing or invalid session"}

        monkeypatch.setattr(sw_api, "_post_json", fake_post)
        with pytest.raises(sw_api.SessionExpiredError):
            await sw_api.call_v2("notes:list", session="sess-1")
        await asyncio.sleep(0.05)  # the check runs as a task

    asyncio.run(go())
    assert not session_mode.active()
