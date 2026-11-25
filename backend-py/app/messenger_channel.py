"""app/messenger_channel.py -- Facebook Messenger through Caroline's own browser (messenger.com).

Replaces the fca-unofficial Node sidecar and its cookie-capture login (owner's decision,
2026-11-24: for WhatsApp and Messenger the other ways of access are removed and the browser
takes their place). The person signs in ONCE in a visible window of Caroline's own Chromium
(Facebook login, with whatever 2FA or checkpoint Facebook asks for); from then on the same
profile is opened HEADLESS (_chromium_app_browser.py: one profile folder, two modes) and she
reads, watches and answers through the page.

TWO accounts as before: "self" (her own, label "messenger_self") and "owner" (the person's,
label "messenger_owner"; sent from only on request for that specific message). The tools keep
their names (messenger_*) and this module keeps the functions messenger_plugin.py calls.

A thread's id is the number in its address (messenger.com/t/<id>), which is what the tools use.
The page gives no stable message ids and no sender ids: a message is told apart by a hash of its
thread, text and attachment links, and a sender is known by name only.

All selectors and in-page scripts are in the table _SEL and the _JS_* constants: when Facebook
changes the page, that is the only place to change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.logging_setup import log_event
from app.plugins import _chromium_app_browser as browser
from app.secret_store import messenger_secrets_dir
from app.task_supervisor import supervise
from app.untrusted_text import third_party_label, third_party_text
from app.workspace_dir import WORKSPACE_DIR

ACCOUNTS = ("self", "owner")
DEFAULT_ACCOUNT = "self"
_ACCOUNT_LABEL = {"self": "your own", "owner": "the owner's"}

MESSENGER_URL = "https://www.messenger.com/"
ACTION_TIMEOUT_S = 30.0
DOWNLOAD_TIMEOUT_S = 60.0
POLL_INTERVAL_S = 8.0
LOADING_GIVE_UP_S = 180.0
CHANNEL_HEARTBEAT_TIMEOUT_S = 300.0
CHATS_PAGE_SIZE = 20
SEEN_CAP = 2000
MESSENGER_DOWNLOADS_DIR_NAME = "messenger_downloads"

_SEL = {
    "login": 'input[name="email"], input[type="password"], form[action*="login"]',
    "nav": '[role="navigation"]',
    "rows": '[role="navigation"] a[href*="/t/"], [role="grid"] a[href*="/t/"]',
    "list_scroller": '[role="navigation"] [role="grid"], [role="navigation"]',
    "search": 'input[type="search"], [role="navigation"] input[aria-label*="Search" i], [role="navigation"] input[placeholder*="Search" i]',
    "main": '[role="main"]',
    "composer": '[role="main"] [contenteditable="true"][role="textbox"], [role="main"] [contenteditable="true"][aria-label*="Message" i]',
    "messages": '[role="main"] [role="row"]',
    "file_input": '[role="main"] input[type="file"]',
    "attach": '[role="main"] [aria-label*="attach" i], [role="main"] [aria-label*="Open more actions" i]',
    "header": '[role="main"] h1, [role="main"] [role="banner"], [role="main"] header',
}

_TXT = {
    "block": ["Block", "Заблокировать"],
    "unblock": ["Unblock", "Разблокировать"],
    "confirm": ["Block", "Заблокировать", "Unblock", "Разблокировать", "Leave", "Покинуть", "Leave group",
                "Remove", "Удалить", "Add", "Добавить", "Create", "Создать", "OK", "ОК", "Done", "Готово"],
    "leave": ["Leave group", "Покинуть группу", "Leave chat"],
    "members": ["Chat members", "Участники чата", "Members"],
    "add_people": ["Add people", "Добавить людей", "Add members"],
    "remove_member": ["Remove from group", "Удалить из группы", "Remove member"],
    "make_admin": ["Make admin", "Назначить администратором"],
    "remove_admin": ["Remove admin", "Снять права администратора"],
    "new_message": ["New message", "Новое сообщение"],
    "create_group": ["Create group", "Создать группу"],
    "download": ["Download", "Скачать"],
}

_JS_STATE = """(sel) => {
  if (document.querySelector(sel.login)) return 'login';
  if (document.querySelector(sel.nav) && document.querySelector(sel.rows)) return 'ready';
  if (document.querySelector(sel.nav) && document.querySelector(sel.main)) return 'ready';
  return 'loading';
}"""

_JS_CHAT_LIST = """(sel) => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll(sel.rows)) {
    const m = /\\/t\\/(\\d+)/.exec(a.getAttribute('href') || '');
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    const spans = Array.from(a.querySelectorAll('span[dir="auto"], span')).map(s => (s.innerText || '').trim()).filter(Boolean);
    const uniq = spans.filter((s, i) => spans.indexOf(s) === i);
    const text = a.innerText || '';
    out.push({ id: m[1], name: uniq[0] || '', last: uniq.length > 1 ? uniq[1] : '', unread: /unread|непрочитан/i.test(text + ' ' + (a.getAttribute('aria-label') || '')) });
  }
  return out;
}"""

_JS_MESSAGES = """(sel) => {
  const out = [];
  for (const row of document.querySelectorAll(sel.messages)) {
    const textEl = row.querySelector('div[dir="auto"]');
    const text = textEl ? textEl.innerText : '';
    const links = Array.from(row.querySelectorAll('a[href*="attachment"], a[download], a[href*="scontent"], a[href*="fbcdn"]')).map(a => a.href);
    const imgs = Array.from(row.querySelectorAll('img[src*="scontent"], img[src*="fbcdn"]')).filter(i => i.width > 80).map(i => i.src);
    const vid = row.querySelector('video');
    const whole = (row.innerText || '').trim();
    if (!text && !links.length && !imgs.length && !vid) continue;
    const sentLabel = /^(you sent|вы отправили)$/i;
    const mine = /^(you sent|вы отправили)/i.test(whole) || !!row.querySelector('[aria-label*="You sent" i], [aria-label*="Вы отправили" i]')
      || Array.from(row.querySelectorAll('span, div')).some(e => sentLabel.test((e.innerText || '').trim()));
    const nameEl = row.querySelector('h4, [role="link"][aria-label]');
    out.push({ text, mine, senderName: nameEl ? (nameEl.innerText || nameEl.getAttribute('aria-label') || '').trim() : '',
               media: vid ? 'video' : (imgs.length ? 'image' : (links.length ? 'file' : '')), urls: imgs.concat(links) });
  }
  return out;
}"""

_JS_HEADER = """(sel) => {
  const h = document.querySelector(sel.header);
  if (!h) return null;
  return { title: (h.innerText || '').split('\\n')[0].trim(), text: h.innerText || '' };
}"""

_JS_CLICK_TEXT = """(args) => {
  const want = args.texts.map(s => s.toLowerCase());
  const nodes = Array.from(document.querySelectorAll('[role="button"], [role="menuitem"], button, [role="link"], [role="option"], span'));
  for (const n of nodes) {
    const label = ((n.getAttribute('aria-label') || '') + ' ' + (n.innerText || n.textContent || '')).trim().toLowerCase();
    if (!label || label.length > 60) continue;
    if (want.some(w => label === w || label.startsWith(w + '\\n') || label === w + ' ' + w)) {
      if (n.offsetParent === null) continue;
      n.click(); return true;
    }
  }
  return false;
}"""

_JS_PANEL_TEXT = """() => {
  const el = document.querySelector('[role="dialog"]') || document.querySelector('[role="complementary"]') || document.querySelector('[role="main"]');
  return el ? el.innerText : '';
}"""

_JS_SCROLL_LIST = """(sel) => {
  const s = document.querySelector(sel.list_scroller);
  if (!s) return false;
  const target = Array.from(s.querySelectorAll('*')).find(e => e.scrollHeight > e.clientHeight + 20 && getComputedStyle(e).overflowY !== 'visible') || s;
  target.scrollTop = target.scrollHeight;
  return true;
}"""

_JS_SCROLL_THREAD_UP = """(sel) => {
  const m = document.querySelector(sel.main);
  if (!m) return false;
  const target = Array.from(m.querySelectorAll('*')).find(e => e.scrollHeight > e.clientHeight + 50 && getComputedStyle(e).overflowY !== 'visible');
  if (!target) return false;
  target.scrollTop = 0;
  return true;
}"""


class MessengerError(Exception):
    pass


@dataclass
class MessengerChannelStatus:
    account: str = ""
    linked: bool = False
    connected: bool = False
    own_user_id: str | None = None
    last_event_at_iso: str | None = None
    last_error: str | None = None


_status: dict[str, MessengerChannelStatus] = {a: MessengerChannelStatus(account=a) for a in ACCOUNTS}
_login_requested: dict[str, bool] = {a: False for a in ACCOUNTS}
_locks: dict[str, asyncio.Lock] = {}
_lost_notified: dict[str, bool] = {a: False for a in ACCOUNTS}


def is_valid_account(account: str) -> bool:
    return account in ACCOUNTS


def _check_account(account: str) -> str:
    if account not in ACCOUNTS:
        raise MessengerError(f'"account" must be one of {", ".join(ACCOUNTS)}, not {account!r}.')
    return account


def browser_label(account: str) -> str:
    return f"messenger_{account}"


def _state_dir() -> Path:
    d = messenger_secrets_dir(WORKSPACE_DIR, "messenger")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _marker(account: str) -> Path:
    return _state_dir() / f"browser_linked_{account}"


def _has_prior_session(workspace_dir: str, account: str) -> bool:
    return _marker(account).exists()


def _seen_file(account: str) -> Path:
    return _state_dir() / f"seen_{account}.json"


def _load_seen(account: str) -> list[str]:
    try:
        return list(json.loads(_seen_file(account).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def _save_seen(account: str, seen: list[str]) -> None:
    try:
        _seen_file(account).write_text(json.dumps(seen[-SEEN_CAP:]), encoding="utf-8")
    except OSError:
        pass


def get_messenger_channel_status() -> dict[str, dict[str, Any]]:
    return {account: asdict(status) for account, status in _status.items()}


def is_connected(account: str) -> bool:
    return _status[account].connected and browser.get_page(browser_label(account)) is not None


def _lock(account: str) -> asyncio.Lock:
    if account not in _locks:
        _locks[account] = asyncio.Lock()
    return _locks[account]


async def _page_state(page: Any) -> str:
    try:
        return await page.evaluate(_JS_STATE, _SEL)
    except Exception:
        return "loading"


async def _ready_page(account: str) -> Any:
    _check_account(account)
    page = browser.get_page(browser_label(account))
    if page is None:
        raise MessengerError(f'Messenger ("{account}") isn\'t connected yet -- use messenger_login(account="{account}") first.')
    state = await _page_state(page)
    if state != "ready":
        raise MessengerError(
            f'Messenger ("{account}") is not signed in right now ({state}) -- use messenger_login(account="{account}") '
            "and log in in the window that opens."
        )
    return page


async def _click_text(page: Any, key: str) -> bool:
    return bool(await page.evaluate(_JS_CLICK_TEXT, {"texts": _TXT[key]}))


async def _wait_for(page: Any, selector: str, timeout_s: float = 10.0) -> bool:
    try:
        await page.wait_for_selector(selector, timeout=timeout_s * 1000)
        return True
    except Exception:
        return False


def _msg_key(thread_id: str, m: dict[str, Any]) -> str:
    raw = "|".join((thread_id, "1" if m.get("mine") else "0", m.get("text") or "", ",".join(m.get("urls") or [])))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


async def _open_thread(page: Any, thread_id: str) -> str:
    """Open a thread by its id; its title."""
    if not re.fullmatch(r"\d{1,30}", thread_id):
        raise MessengerError(f'"{thread_id}" is not a thread id (the number from messenger_list_chats).')
    if f"/t/{thread_id}" not in page.url:
        await page.goto(f"{MESSENGER_URL}t/{thread_id}", wait_until="domcontentloaded")
    if not await _wait_for(page, _SEL["composer"], 25.0):
        raise MessengerError(f"The thread {thread_id} did not open (no such thread, or the page is waiting on a PIN/checkpoint).")
    head = await page.evaluate(_JS_HEADER, _SEL)
    return (head or {}).get("title") or thread_id


async def _read_messages(page: Any, thread_id: str) -> list[dict[str, Any]]:
    rows = await page.evaluate(_JS_MESSAGES, _SEL)
    for m in rows:
        m["id"] = _msg_key(thread_id, m)
    return rows


# -- the functions messenger_plugin.py calls -------------------------------------------------------

async def send_message(account: str, to: str, text: str) -> None:
    async with _lock(account):
        page = await _ready_page(account)
        await _open_thread(page, to)
        before = {m["id"] for m in await _read_messages(page, to) if m["mine"]}
        composer = page.locator(_SEL["composer"]).first
        await composer.click()
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                await page.keyboard.insert_text(line)
            if i < len(lines) - 1:
                await page.keyboard.press("Shift+Enter")
        await page.keyboard.press("Enter")
        deadline = time.monotonic() + ACTION_TIMEOUT_S
        while time.monotonic() < deadline:
            await page.wait_for_timeout(500)
            if {m["id"] for m in await _read_messages(page, to) if m["mine"]} - before:
                return
        raise MessengerError("The message was typed but no new sent message appeared on the page within 30 s.")


async def _chats_all(page: Any, want: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = await page.evaluate(_JS_CHAT_LIST, _SEL)
    for _ in range(40):
        if len(rows) >= want:
            break
        await page.evaluate(_JS_SCROLL_LIST, _SEL)
        await page.wait_for_timeout(900)
        more = await page.evaluate(_JS_CHAT_LIST, _SEL)
        if len(more) <= len(rows):
            break
        rows = more
    return rows


async def list_chats_page(account: str, cursor: int | None = None) -> dict[str, Any]:
    """One page of CHATS_PAGE_SIZE chats, newest activity first (the list's own order); `cursor`
    is how many chats came before. nextCursor is None when the list has no more."""
    start = cursor or 0
    async with _lock(account):
        page = await _ready_page(account)
        rows = await _chats_all(page, start + CHATS_PAGE_SIZE + 1)
    chunk = rows[start:start + CHATS_PAGE_SIZE]
    more = len(rows) > start + CHATS_PAGE_SIZE
    return {
        "chats": [{"id": r["id"], "name": r["name"], "unread": r["unread"], "lastMessage": r["last"], "lastActivity": None} for r in chunk],
        "nextCursor": start + CHATS_PAGE_SIZE if more else None,
        "fromCache": False,
    }


async def list_chats(account: str) -> list[dict[str, Any]]:
    async with _lock(account):
        page = await _ready_page(account)
        rows = await _chats_all(page, 10_000)
    return [{"id": r["id"], "name": r["name"], "unread": r["unread"], "lastMessage": r["last"], "lastActivity": None} for r in rows]


async def get_user_info(account: str, user_ids: list[str]) -> dict[str, Any]:
    """{id: {"name": ...}} -- the name in the header of the one-to-one thread of that id (the
    page shows nothing more public than that)."""
    out: dict[str, Any] = {}
    async with _lock(account):
        page = await _ready_page(account)
        for uid in user_ids:
            try:
                out[uid] = {"id": uid, "name": await _open_thread(page, uid)}
            except MessengerError:
                continue
    return out


async def _panel(page: Any) -> str:
    if not await _click_text(page, "members"):
        # the thread's info panel: the header's info button
        await page.locator('[role="main"] [aria-label*="Conversation information" i], [role="main"] [aria-label*="Информация" i]').first.click()
    await page.wait_for_timeout(1200)
    return await page.evaluate(_JS_PANEL_TEXT)


async def get_thread_info(account: str, thread_id: str) -> dict[str, Any]:
    async with _lock(account):
        page = await _ready_page(account)
        title = await _open_thread(page, thread_id)
        try:
            panel = await _panel(page)
        except Exception:
            panel = ""
    names = [ln.strip() for ln in panel.splitlines() if ln.strip()]
    return {"threadID": thread_id, "name": title, "panelText": panel, "participants": names}


async def find_user(account: str, name: str) -> list[Any]:
    """Candidates for a name: the people the page's own search offers."""
    async with _lock(account):
        page = await _ready_page(account)
        box = page.locator(_SEL["search"]).first
        await box.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.insert_text(name)
        await page.wait_for_timeout(1800)
        rows = await page.evaluate(_JS_CHAT_LIST, _SEL)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.press("Escape")
    return [{"id": r["id"], "name": r["name"]} for r in rows]


async def set_block(account: str, user_id: str, block: bool) -> None:
    async with _lock(account):
        page = await _ready_page(account)
        await _open_thread(page, user_id)
        await _panel(page)
        key = "block" if block else "unblock"
        if not await _click_text(page, key):
            raise MessengerError(f"The {key} entry was not found in the thread's panel.")
        await page.wait_for_timeout(800)
        await _click_text(page, "confirm")
        await page.wait_for_timeout(800)
        await page.keyboard.press("Escape")


async def read_thread_history(account: str, thread_id: str, amount: int) -> list[dict[str, Any]]:
    """The last `amount` messages of a thread, oldest first, loaded by scrolling up as needed."""
    async with _lock(account):
        page = await _ready_page(account)
        await _open_thread(page, thread_id)
        await page.wait_for_timeout(800)
        messages = await _read_messages(page, thread_id)
        for _ in range(60):
            if len(messages) >= amount:
                break
            if not await page.evaluate(_JS_SCROLL_THREAD_UP, _SEL):
                break
            await page.wait_for_timeout(1000)
            more = await _read_messages(page, thread_id)
            if len(more) <= len(messages):
                break
            messages = more
    return [
        {"id": m["id"], "senderID": None, "senderName": "" if m["mine"] else m.get("senderName", ""), "fromSelf": m["mine"],
         "text": m["text"], "attachment": m["media"] or None, "attachmentRef": f"{thread_id}|{m['id']}" if m["media"] else None}
        for m in messages[-amount:]
    ]


async def download_attachment(workspace_dir: str, account: str, ref: str, save_path: str | None = None) -> str:
    """ref = "<thread id>|<message key>" from a note or messenger_read_history."""
    thread_id, _, key = ref.partition("|")
    if not save_path:
        downloads_dir = Path(workspace_dir) / MESSENGER_DOWNLOADS_DIR_NAME
        downloads_dir.mkdir(parents=True, exist_ok=True)
        save_path = str(downloads_dir / re.sub(r"[^A-Za-z0-9_.-]", "_", ref))
    async with _lock(account):
        page = await _ready_page(account)
        await _open_thread(page, thread_id)
        messages = await _read_messages(page, thread_id)
        match = next((m for m in messages if m["id"] == key), None)
        if match is None or not match.get("urls"):
            raise MessengerError("That attachment is not on the page any more: ask for it to be sent again.")
        response = await page.context.request.get(match["urls"][0], timeout=DOWNLOAD_TIMEOUT_S * 1000)
        if not response.ok:
            raise MessengerError(f"The attachment could not be downloaded (HTTP {response.status}).")
        target = Path(save_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await response.body())
    return str(target)


async def send_file(account: str, to: str, path: str, caption: str | None = None) -> None:
    if not Path(path).is_file():
        raise MessengerError(f"No such local file: {path}")
    async with _lock(account):
        page = await _ready_page(account)
        await _open_thread(page, to)
        inputs = page.locator(_SEL["file_input"])
        if not await inputs.count():
            await page.locator(_SEL["attach"]).first.click()
            await page.wait_for_timeout(600)
        if not await inputs.count():
            raise MessengerError("The thread offered no file input.")
        await inputs.first.set_input_files(path)
        await page.wait_for_timeout(1500)
        if caption:
            await page.locator(_SEL["composer"]).first.click()
            await page.keyboard.insert_text(caption)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(2000)


async def create_group(account: str, subject: str, participants: list[str]) -> str:
    async with _lock(account):
        page = await _ready_page(account)
        if not await _click_text(page, "new_message"):
            await page.locator('[aria-label*="New message" i], [aria-label*="Новое сообщение" i]').first.click()
        await page.wait_for_timeout(800)
        for who in participants:
            label = who
            if who.isdigit():
                await page.goto(f"{MESSENGER_URL}t/{who}", wait_until="domcontentloaded")
                head = await page.evaluate(_JS_HEADER, _SEL)
                label = (head or {}).get("title") or who
                await _click_text(page, "new_message")
                await page.wait_for_timeout(600)
            await page.keyboard.insert_text(label)
            await page.wait_for_timeout(1200)
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(600)
        before = {r["id"] for r in await page.evaluate(_JS_CHAT_LIST, _SEL)}
        composer = page.locator(_SEL["composer"]).first
        await composer.click()
        await page.keyboard.insert_text(subject)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(2500)
        match = re.search(r"/t/(\d+)", page.url)
        if not match or match.group(1) in before:
            raise MessengerError("The group chat did not open after creating it.")
        return match.group(1)


async def update_group_participants(account: str, thread_id: str, participants: list[str], action: str) -> None:
    async with _lock(account):
        page = await _ready_page(account)
        await _open_thread(page, thread_id)
        await _panel(page)
        for who in participants:
            label = who
            if action == "add":
                if not await _click_text(page, "add_people"):
                    raise MessengerError("The Add people entry was not found.")
                await page.wait_for_timeout(600)
                await page.keyboard.insert_text(label)
                await page.wait_for_timeout(1200)
                await page.keyboard.press("Enter")
                await _click_text(page, "confirm")
                await page.wait_for_timeout(800)
                continue
            row = page.locator('[role="dialog"] [role="row"], [role="complementary"] [role="row"], [role="main"] [role="listitem"]').filter(has_text=label).first
            if not await row.count():
                raise MessengerError(f"{label} was not found among the members.")
            await row.hover()
            await page.locator('[aria-label*="More" i], [aria-label*="Ещё" i]').last.click()
            await page.wait_for_timeout(500)
            key = {"remove": "remove_member", "promote": "make_admin", "demote": "remove_admin"}[action]
            if not await _click_text(page, key):
                raise MessengerError(f"The '{action}' entry was not offered for {label} (are you an admin?).")
            await page.wait_for_timeout(600)
            await _click_text(page, "confirm")
            await page.wait_for_timeout(800)
        await page.keyboard.press("Escape")


async def leave_group(account: str, thread_id: str) -> None:
    async with _lock(account):
        page = await _ready_page(account)
        await _open_thread(page, thread_id)
        await _panel(page)
        if not await _click_text(page, "leave"):
            raise MessengerError("The Leave group entry was not found.")
        await page.wait_for_timeout(600)
        await _click_text(page, "confirm")
        await page.wait_for_timeout(800)


async def delete_group(account: str, thread_id: str) -> None:
    """Messenger has no true "delete this group": every other member is removed, then she leaves."""
    info = await get_thread_info(account, thread_id)
    others = [p for p in info.get("participants", []) if p.lower() not in ("you", "вы") and len(p) < 60][:256]
    if others:
        await update_group_participants(account, thread_id, others, "remove")
    await leave_group(account, thread_id)


# -- sign-in and the watch ---------------------------------------------------------------------

async def login_start(account: str) -> str:
    """Open the account's profile in a VISIBLE window at messenger.com for the person to log in;
    the watch loop notices the chat list and moves the profile to headless."""
    _check_account(account)
    if is_connected(account):
        return f'Already connected (account="{account}").'
    _login_requested[account] = True
    _lost_notified[account] = False
    await browser.open_window(browser_label(account), MESSENGER_URL, headless=False)
    return (
        f'A Messenger window is open for the "{account}" account -- log in there yourself (including any 2FA code or '
        "checkpoint Facebook asks for, and the PIN if Messenger asks for one to restore chats). No need to tell me "
        "anything back: once the chats show I switch the window to the background and carry on quietly -- you sign in "
        "only this once."
    )


def _note_for(account: str, thread_id: str, title: str, m: dict[str, Any]) -> str:
    name = third_party_label(title or thread_id)
    text = (m.get("text") or "").strip()
    note = (
        f'[New Messenger message in {_ACCOUNT_LABEL[account]} Messenger account, from "{name}" '
        f'(threadID={thread_id}'
        + (f', senderName="{third_party_label(m["senderName"])}"' if m.get("senderName") else "")
        + f', account="{account}"):\n'
        f'{third_party_text(text, name) if text else "(no text)"}'
    )
    if m.get("media"):
        note += (
            f'\nAttached: {m["media"]}. Use messenger_download_attachment(ref="{thread_id}|{m["id"]}", '
            f'account="{account}") to save it locally -- only while the message is still on the page.'
        )
    if account == "self":
        note += (
            "\nThis is Caroline's own separate Messenger account, not the user's -- replying here is "
            "Caroline's own correspondence, not something done on the user's behalf, and needs no extra "
            f'permission. Reply with messenger_send_message(to="{thread_id}", text=..., account="self") '
            "if it needs a response.]"
        )
    else:
        note += (
            "\nThis is the OWNER'S OWN correspondence, not yours -- read it and tell the owner about it "
            "if it matters, but do not reply as them unless they have explicitly asked you to send this "
            f'specific reply (messenger_send_message(to="{thread_id}", text=..., account="owner")).]'
        )
    return note


async def _collect_new(account: str, page: Any, baseline: dict[str, str], inject: Callable[[str], None]) -> None:
    """A chat is new when it shows unread and its last-message preview differs from what was
    seen: it is opened and its newest incoming messages become notes."""
    rows = await page.evaluate(_JS_CHAT_LIST, _SEL)
    seen = _load_seen(account)
    seen_set = set(seen)
    for row in rows:
        tid, preview = row["id"], row["last"]
        previous = baseline.get(tid)
        baseline[tid] = preview
        if not row["unread"] or previous == preview:
            continue
        try:
            await page.goto(f"{MESSENGER_URL}t/{tid}", wait_until="domcontentloaded")
            if not await _wait_for(page, _SEL["composer"], 15.0):
                continue
            await page.wait_for_timeout(800)
            messages = await _read_messages(page, tid)
        except Exception as exc:
            log_event("plugin:messenger", "read_thread_failed", account=account, error=str(exc)[:200])
            continue
        incoming = [m for m in messages if not m["mine"]]
        # Only the trailing run of incoming messages (after my last one) can be new.
        tail: list[dict[str, Any]] = []
        for m in reversed(messages):
            if m["mine"]:
                break
            tail.append(m)
        for m in reversed(tail):
            if m["id"] in seen_set or (not m["text"].strip() and not m["media"]):
                continue
            seen_set.add(m["id"])
            seen.append(m["id"])
            _status[account].last_event_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            log_event("plugin:messenger", "message_received", account=account, thread_id=tid)
            inject(_note_for(account, tid, row["name"], m))
        _ = incoming
    _save_seen(account, seen)


async def _watch(workspace_dir: str, account: str, inject: Callable[[str], None], touch: Callable[[], None]) -> None:
    label = browser_label(account)
    status = _status[account]
    await browser.open_window(label, MESSENGER_URL, headless=not _login_requested[account])
    loading_since = time.monotonic()
    baseline: dict[str, str] | None = None
    while True:
        touch()
        page = browser.get_page(label)
        if page is None:
            _login_requested[account] = False   # the person closed the sign-in window: not reopened by itself
            raise MessengerError("the Messenger browser window was closed")
        state = await _page_state(page)
        if state == "ready":
            loading_since = time.monotonic()
            _lost_notified[account] = False
            if not status.linked:
                _marker(account).write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
                log_event("plugin:messenger", "connected", account=account)
            status.linked = status.connected = True
            status.last_error = None
            if browser.is_headless(label) is False and _login_requested[account]:
                _login_requested[account] = False
                await browser.set_mode(label, True)   # signed in: the window goes, the session stays
                baseline = None
                await asyncio.sleep(3.0)
                continue
            async with _lock(account):
                if baseline is None:
                    baseline = {r["id"]: r["last"] for r in await page.evaluate(_JS_CHAT_LIST, _SEL)}
                else:
                    await _collect_new(account, page, baseline, inject)
        elif state == "login":
            loading_since = time.monotonic()
            status.connected = False
            if browser.is_headless(label) and status.linked and not _lost_notified[account]:
                status.linked = False
                _lost_notified[account] = True
                _marker(account).unlink(missing_ok=True)
                log_event("plugin:messenger", "session_lost", account=account)
                inject(
                    f'[Messenger ("{account}", {_ACCOUNT_LABEL[account]} account) has signed out: Facebook ended the '
                    f'session. Tell the user once, and ask them to run messenger_login(account="{account}") and log in again.]'
                )
        else:
            status.connected = False
            if time.monotonic() - loading_since > LOADING_GIVE_UP_S:
                raise MessengerError("messenger.com did not finish loading")
        await asyncio.sleep(POLL_INTERVAL_S)


def start_messenger_channel(workspace_dir: str, inject_proactive: Callable[[str], None]) -> list[asyncio.Task[None]]:
    """One supervised loop per account: with a kept sign-in (or after messenger_login) the profile
    is opened and watched; otherwise it waits, cheaply, until one of those happens."""
    def _make_loop(account: str) -> Callable[[Callable[[], None]], Any]:
        async def _loop(touch: Callable[[], None]) -> None:
            while True:
                if not (_login_requested[account] or _has_prior_session(workspace_dir, account)):
                    touch()
                    await asyncio.sleep(30.0)
                    continue
                try:
                    await _watch(workspace_dir, account, inject_proactive, touch)
                except MessengerError as exc:
                    _status[account].last_error = str(exc)
                    _status[account].connected = False
                    log_event("plugin:messenger", "watch_unavailable", account=account, error=str(exc))
                    touch()
                    await asyncio.sleep(60.0)
                    continue
                except Exception as exc:
                    _status[account].last_error = str(exc)
                    _status[account].connected = False
                    log_event("plugin:messenger", "watch_failed", account=account, error=str(exc)[:300])
                touch()
                await asyncio.sleep(5.0)
        return _loop

    return [
        supervise(f"messenger_channel_{account}", _make_loop(account), heartbeat_timeout_s=CHANNEL_HEARTBEAT_TIMEOUT_S)
        for account in ACCOUNTS
    ]
