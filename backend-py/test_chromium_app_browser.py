"""Caroline's one browser (_chromium_app_browser.py): a label's profile is shared between the
visible window and the headless browser, kept between runs, downloadable and forgettable.
Uses the bundled Chromium (dist/browsers) and a local HTTP server; the visible-window test
opens one small window for a moment."""
import asyncio
import http.server
import threading
from pathlib import Path

import pytest

from app.plugins import _chromium_app_browser as b
from app.web_browser import bundled_chromium_path

pytestmark = pytest.mark.skipif(bundled_chromium_path() is None, reason="the bundled Chromium is not in this tree")


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/file":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="got.bin"')
            self.end_headers()
            self.wfile.write(b"payload")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Set-Cookie", "session=signed-in; Max-Age=86400; Path=/")
        self.end_headers()
        self.wfile.write(b'<title>t</title><a id="dl" href="/file">get</a><script>localStorage.setItem("k","v")</script>')


@pytest.fixture()
def site(tmp_path, monkeypatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(b, "PROFILES_DIR", tmp_path / "profiles")
    b._windows.clear()
    b._modes.clear()
    b._playwright_instance = None   # Playwright belongs to the event loop of its test
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


def _run(coro):
    async def guarded():
        try:
            return await asyncio.wait_for(coro, 120)
        finally:
            for label in list(b._windows):
                await b.close_window(label)
    return asyncio.run(guarded())


async def _state(label):
    window = b._windows[label]
    cookies = await window.context.cookies()
    stored = await window.page.evaluate("localStorage.getItem('k')")
    return [c["name"] for c in cookies], stored


def test_headless_profile_is_kept_between_runs(site):
    async def go():
        await b.open_window("svc", site, headless=True)
        await b.close_window("svc")
        await b.open_window("svc", site + "other", headless=True)   # the server sets nothing new we rely on
        names, _ = await _state("svc")
        assert "session" in names
        assert any(p["label"] == "svc" for p in b.list_profiles())
        await b.close_window("svc")
    _run(go())


def test_visible_window_and_headless_share_one_profile(site):
    async def go():
        opened = await b.open_window("svc", site, headless=False)
        assert opened["headless"] is False
        names, stored = await _state("svc")
        assert "session" in names and stored == "v"
        moved = await b.set_mode("svc", True)              # same label, same profile, no window now
        assert moved["headless"] is True and moved["url"].startswith(site)
        names, stored = await _state("svc")
        assert "session" in names and stored == "v"
        assert (await b.is_visible_on_top("svc"))["visible"] is False
        back = await b.set_mode("svc", False)
        assert back["headless"] is False
        await b.close_window("svc")
    _run(go())


def test_download_and_forget(site, tmp_path):
    async def go():
        await b.open_window("svc", site, headless=True)
        saved = await b.download("svc", None, "#dl", str(tmp_path / "out" / "got.bin"))
        assert Path(saved["saved"]).read_bytes() == b"payload"
        gone = await b.forget_profile("svc")
        assert gone["deleted"] is True
        assert not any(p["label"] == "svc" for p in b.list_profiles())
        assert (await b.forget_profile("../x"))["deleted"] is False
    _run(go())
