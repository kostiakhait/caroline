"""A cached account value logs a refresh only when it changed or was slow (app/account_state.py, 2026-11-22).
Run: python -m pytest test_account_state_log.py -q"""

import asyncio

from app import account_state


def test_refreshes_are_logged_on_change_only(monkeypatch):
    logged = []
    monkeypatch.setattr(account_state, "log_event", lambda component, event, **f: logged.append((event, f)))
    values = iter([{"ok": True}, {"ok": True}, {"ok": True}, {"ok": False}])

    async def fetch():
        return next(values)

    cached = account_state.CachedAsyncValue("probe", fetch)

    async def go():
        for _ in range(4):
            await cached._do_fetch()

    asyncio.run(go())
    assert [(e, f["changed"]) for e, f in logged] == [("account_state_refreshed", True), ("account_state_refreshed", True)]


def test_a_slow_refresh_is_logged_even_unchanged(monkeypatch):
    logged = []
    monkeypatch.setattr(account_state, "log_event", lambda component, event, **f: logged.append(f))
    monkeypatch.setattr(account_state, "SLOW_REFRESH_LOG_MS", 0)

    async def fetch():
        return 1

    cached = account_state.CachedAsyncValue("probe", fetch)
    asyncio.run(cached._do_fetch())
    asyncio.run(cached._do_fetch())
    assert [f["changed"] for f in logged] == [True, False]
