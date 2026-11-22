"""The phone inbox loop while the server cannot be reached (2026-11-22): one failed read per tick, not one per tab
plus one per sync. Run: python -m pytest test_companion_inbox_offline.py -q"""

import asyncio

import httpx
import pytest

from app.plugins import companion_api as ca
from app.plugins.sw_api import SwApiError


@pytest.fixture
def tick(monkeypatch):
    reads, syncs = [], []
    monkeypatch.setattr(ca, "is_logged_in", lambda: True)
    monkeypatch.setattr(ca, "_desktop_settings_tab_list", lambda: None)

    async def sync(*args, **kwargs):
        syncs.append(args)

    for name in ("_sync_tab_list", "_sync_history", "_sync_status"):
        monkeypatch.setattr(ca, name, sync)

    def run(error):
        async def read(path):
            reads.append(path)
            if error is not None:
                raise error
            return {}

        monkeypatch.setattr(ca, "get_all_mine", read)
        asyncio.run(ca._inbox_loop_tick(".", lambda: ["1", "2", "3"], lambda *a: True, lambda: {}, lambda: {}))
        return reads, syncs

    return run


def test_offline_one_read_and_no_syncs(tick):
    reads, syncs = tick(httpx.ConnectError("[Errno 11001] getaddrinfo failed"))
    assert reads == ["tabs/1/inbox"] and syncs == []


def test_an_http_error_still_reads_every_tab_and_syncs(tick):
    reads, syncs = tick(SwApiError('SquirrelWisdom API HTTP 504 for command "var:getAllMine"'))
    assert reads == ["tabs/1/inbox", "tabs/2/inbox", "tabs/3/inbox"] and len(syncs) == 3


def test_online_nothing_changes(tick):
    reads, syncs = tick(None)
    assert len(reads) == 3 and len(syncs) == 3
