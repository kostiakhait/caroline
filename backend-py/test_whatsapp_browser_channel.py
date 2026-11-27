"""whatsapp_channel.py over a page that has WhatsApp Web's structure (a stand-in, not the real
site: the real selectors are checked live). Covers the page scripts, the watch's note-making
and the seen/baseline bookkeeping."""
import asyncio

import pytest

from app import whatsapp_channel as wa
from app.web_browser import bundled_chromium_path

pytestmark = pytest.mark.skipif(bundled_chromium_path() is None, reason="the bundled Chromium is not in this tree")

PAGE = """<!doctype html><body><div id="app">
<div id="side"><div contenteditable="true" role="textbox" data-tab="3"></div>
<div id="pane-side">
 <div role="row"><span title="Anna">Anna</span><span dir="auto">hi there</span><span aria-label="2 unread messages">2</span></div>
 <div role="row"><span title="Team">Team</span><span dir="auto">ok</span></div>
</div></div>
<div id="main" style="display:none"><header><span title="Anna">Anna</span></header><div id="msgs"></div>
<footer><div contenteditable="true" data-tab="10"></div></footer></div>
<script>
document.querySelector('[role=row] span[title=Anna]').closest('[role=row]').addEventListener('click', () => {
  document.querySelectorAll('[aria-label*=unread]').forEach(b => b.remove());   // opening the chat reads it
  const m = document.getElementById('main'); m.style.display = 'block';
  document.getElementById('msgs').innerHTML =
   '<div data-id="false_491234@s.whatsapp.net_AAA1"><div data-pre-plain-text="[10:01, 11/25/2026] Anna: "><span class="selectable-text">old one</span></div></div>' +
   '<div data-id="true_491234@s.whatsapp.net_BBB2"><div data-pre-plain-text="[10:02, 11/25/2026] Me: "><span class="selectable-text">mine</span></div></div>' +
   '<div data-id="false_491234@s.whatsapp.net_CCC3"><div data-pre-plain-text="[10:03, 11/25/2026] Anna: "><span class="selectable-text">are you free?</span></div></div>' +
   '<div data-id="false_491234@s.whatsapp.net_DDD4"><div data-pre-plain-text="[10:04, 11/25/2026] Anna: "><span class="selectable-text">call me</span></div></div>';
});
</script></div></body>"""


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
    monkeypatch.setattr(wa, "messenger_secrets_dir", lambda ws, name: tmp_path / name)
    wa._names.clear()
    for a in wa.ACCOUNTS:
        wa._status[a] = wa.WhatsAppChannelStatus(account=a)


def test_page_state_and_chat_list():
    async def go(page):
        assert await wa._page_state(page) == "ready"
        rows = await page.evaluate(wa._JS_CHAT_LIST, wa._SEL)
        assert [(r["title"], r["unread"]) for r in rows] == [("Anna", 2), ("Team", 0)]
        await page.set_content("<body><canvas aria-label='Scan this QR code'></canvas></body>")
        assert await wa._page_state(page) == "qr"
        await page.set_content("<body>spinner</body>")
        assert await wa._page_state(page) == "loading"
    asyncio.run(_with_page(go))


def test_new_messages_become_notes_once():
    notes = []

    async def go(page):
        baseline = {"Anna": 0, "Team": 0}
        await wa._collect_new("owner", page, baseline, notes.append)
        assert len(notes) == 2                                   # unread = 2 -> the last two incoming
        assert "are you free?" in notes[0] and "call me" in notes[1]
        assert "old one" not in "".join(notes)
        assert 'account="owner"' in notes[0] and "OWNER'S OWN" in notes[0]
        assert "jid=491234@s.whatsapp.net" in notes[0]
        assert wa._load_names("owner")["491234@s.whatsapp.net"] == "Anna"
        assert wa._chat_list_id("owner", "Anna") == "491234@s.whatsapp.net"
        assert wa._chat_list_id("owner", "Team") == "Team@chat.name"
        await wa._collect_new("owner", page, baseline, notes.append)   # nothing new: no second note
        assert len(notes) == 2
    asyncio.run(_with_page(go))


def test_messages_script_reads_ids_and_senders():
    async def go(page):
        await page.locator("#pane-side [role=row]").first.click()
        msgs = await wa._read_messages(page)
        assert [m["fromMe"] for m in msgs] == [False, True, False, False]
        assert msgs[0]["chat"] == "491234@s.whatsapp.net" and msgs[0]["senderName"] == "Anna"
        assert msgs[1]["text"] == "mine"
    asyncio.run(_with_page(go))


def test_unknown_account_and_not_connected():
    with pytest.raises(wa.WhatsAppError):
        asyncio.run(wa.send_message("nobody", "1@s.whatsapp.net", "x"))
    with pytest.raises(wa.WhatsAppError, match="isn't connected"):
        asyncio.run(wa.list_chats("self"))
