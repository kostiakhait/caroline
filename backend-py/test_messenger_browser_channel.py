"""messenger_channel.py over a page with Messenger's structure (a stand-in, not the real site: the
real selectors are checked live). Covers the page scripts, the watch's note-making and bookkeeping."""
import asyncio

import pytest

from app import messenger_channel as ms
from app.web_browser import bundled_chromium_path

pytestmark = pytest.mark.skipif(bundled_chromium_path() is None, reason="the bundled Chromium is not in this tree")

PAGE = """<!doctype html><body>
<div role="navigation"><div role="grid">
 <a href="/t/1001/"><span dir="auto">Anna Berg</span><span dir="auto">see you</span><span>Unread message</span></a>
 <a href="/t/2002/"><span dir="auto">Team</span><span dir="auto">ok</span></a>
</div></div>
<div role="main" id="main"><h1>Anna Berg</h1><div id="msgs"></div>
<div contenteditable="true" role="textbox" aria-label="Message"></div></div>
<script>
const rows = {
 1001: [['you sent', 'hello', 1], ['', 'are you free tonight?', 0], ['', 'ping me', 0]],
};
document.querySelector('a[href="/t/1001/"]').addEventListener('click', e => {
  e.preventDefault();
  document.getElementById('msgs').innerHTML = rows[1001].map(r =>
    '<div role="row"><div dir="auto">' + r[1] + '</div>' + (r[2] ? '<span>You sent</span>' : '') + '</div>').join('');
});
</script></body>"""


async def _with_page(test):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=str(bundled_chromium_path()), headless=True)
        page = await browser.new_page()
        await page.set_content(PAGE)
        try:
            await test(page)
        finally:
            await browser.close()


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "messenger_secrets_dir", lambda ws, name: tmp_path / name)
    for a in ms.ACCOUNTS:
        ms._status[a] = ms.MessengerChannelStatus(account=a)


def test_state_and_chat_list():
    async def go(page):
        assert await ms._page_state(page) == "ready"
        rows = await page.evaluate(ms._JS_CHAT_LIST, ms._SEL)
        assert [(r["id"], r["name"], r["unread"]) for r in rows] == [("1001", "Anna Berg", True), ("2002", "Team", False)]
        await page.set_content("<body><input name='email'><input type='password'></body>")
        assert await ms._page_state(page) == "login"
        await page.set_content("<body>spinner</body>")
        assert await ms._page_state(page) == "loading"
    asyncio.run(_with_page(go))


def test_new_message_becomes_a_note_once():
    notes = []

    async def go(page):
        baseline = {"1001": "earlier text", "2002": "ok"}
        # collect_new navigates by URL; the stand-in has no server, so open the thread by click instead.
        async def no_goto(*a, **k):
            await page.locator('a[href="/t/1001/"]').click()
        page.goto = no_goto
        await ms._collect_new("owner", page, baseline, notes.append)
        assert len(notes) == 2                                          # the run of incoming messages after mine
        assert "are you free tonight?" in notes[0] and "ping me" in notes[1]
        assert "hello" not in "".join(notes)
        assert "threadID=1001" in notes[0] and 'account="owner"' in notes[0] and "OWNER'S OWN" in notes[0]
        await ms._collect_new("owner", page, {"1001": "earlier text"}, notes.append)   # seen already: no repeat
        assert len(notes) == 2
    asyncio.run(_with_page(go))


def test_messages_script_and_keys():
    async def go(page):
        await page.locator('a[href="/t/1001/"]').click()
        msgs = await ms._read_messages(page, "1001")
        assert [m["mine"] for m in msgs] == [True, False, False]
        assert len({m["id"] for m in msgs}) == 3
        assert msgs[1]["id"] == ms._msg_key("1001", msgs[1])
    asyncio.run(_with_page(go))


def test_not_connected_and_bad_ids():
    with pytest.raises(ms.MessengerError, match="isn't connected"):
        asyncio.run(ms.list_chats("self"))
    with pytest.raises(ms.MessengerError):
        asyncio.run(ms.send_message("nobody", "1", "x"))
