"""app/whatsapp_channel.py -- WhatsApp through Caroline's own browser (web.whatsapp.com).

Replaces the Baileys Node sidecar (owner's decision, 2026-11-24: for WhatsApp and Messenger the
other ways of access are removed and this one takes their place). The person signs in ONCE, in
a visible window of Caroline's own Chromium (the QR scan); from then on the same profile is
opened HEADLESS (_chromium_app_browser.py: one profile folder, two modes) and Caroline reads,
watches and answers through the page like a person at the keyboard would.

TWO accounts as before: "self" is Caroline's own WhatsApp (her own profile, label
"whatsapp_self"); "owner" is the person's (label "whatsapp_owner"), sent from only when they ask
for that specific message. The tools keep their names (whatsapp_*) and this module keeps the
functions whatsapp_plugin.py calls.

How a chat is told apart: the chat list gives titles, unread counts and last messages, no jids.
A message row carries a data-id ("true|false_<chat jid>_<message id>[_<sender>]"), so jids are
learned from the messages that are read and remembered (chats.json). A chat whose jid is not
known yet is addressed as "<title>@chat.name": whatsapp_list_chats returns those, and the same
string works as `to`. A phone number opens its chat through WhatsApp's own deep link.

All selectors and the in-page scripts are in the table _SEL and the _JS_* constants below: when
WhatsApp changes its page, that is the only place to change.

Reading new messages opens the chat that has unread messages, so the messages are marked read
(on the phone too) -- the cost of reading in a browser, as it is for a person.
"""

from __future__ import annotations

import asyncio
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

WHATSAPP_URL = "https://web.whatsapp.com/"
ACTION_TIMEOUT_S = 30.0
DOWNLOAD_TIMEOUT_S = 60.0
POLL_INTERVAL_S = 8.0
LOADING_GIVE_UP_S = 180.0
CHANNEL_HEARTBEAT_TIMEOUT_S = 300.0
SEEN_CAP = 2000
NAME_JID_SUFFIX = "@chat.name"

WHATSAPP_DOWNLOADS_DIR = Path(WORKSPACE_DIR) / "whatsapp_downloads"

# Where things are on the page. Roles and aria-labels first (they change least), WhatsApp's own
# data-* attributes second.
_SEL = {
    "pane": "#pane-side",
    "qr": 'canvas[aria-label*="QR" i], div[data-ref] canvas, div[data-ref]',
    "rows": '#pane-side [role="row"], #pane-side [role="listitem"]',
    "search": '#side [contenteditable="true"][role="textbox"], #side [contenteditable="true"][data-tab="3"]',
    "main": "#main",
    "composer": '#main footer [contenteditable="true"], #main [contenteditable="true"][data-tab="10"]',
    "messages": "#main [data-id]",
    "attach": '#main footer [data-icon="plus-rounded"], #main footer [data-icon="attach-menu-plus"], #main footer [data-icon="clip"]',
    "file_input": 'input[type="file"]',
    "send": '#main footer [data-icon="send"], #main footer button[aria-label*="Send" i]',
    "header": "#main header",
}

# Words of the page's own buttons (English and Russian interface), used where only text names them.
_TXT = {
    "new_chat": ["New chat", "Новый чат"],
    "new_group": ["New group", "Новая группа"],
    "block": ["Block", "Заблокировать"],
    "unblock": ["Unblock", "Разблокировать"],
    "confirm": ["Block", "Заблокировать", "Unblock", "Разблокировать", "Leave group", "Выйти из группы", "Exit group",
                "Remove", "Удалить", "Add", "Добавить", "OK", "ОК"],
    "exit_group": ["Exit group", "Leave group", "Выйти из группы"],
    "add_participant": ["Add member", "Add participants", "Добавить участника", "Добавить участников"],
    "make_admin": ["Make group admin", "Make admin", "Назначить администратором"],
    "dismiss_admin": ["Dismiss as admin", "Remove admin", "Снять с должности администратора"],
    "remove_member": ["Remove", "Удалить"],
    "create": ["Create group", "Create", "Создать группу", "Создать"],
    "invalid_number": ["invalid", "not on WhatsApp", "недействителен", "не зарегистрирован"],
}

# -- scripts run inside the page ----------------------------------------------------------------

_JS_STATE = """(sel) => {
  if (document.querySelector(sel.pane)) return 'ready';
  if (document.querySelector(sel.qr)) return 'qr';
  return 'loading';
}"""

_JS_CHAT_LIST = """(sel) => {
  const out = [];
  for (const row of document.querySelectorAll(sel.rows)) {
    const titleEl = row.querySelector('span[title]');
    const title = titleEl ? (titleEl.getAttribute('title') || titleEl.textContent || '').trim() : '';
    if (!title) continue;
    const badge = row.querySelector('span[aria-label*="unread" i], span[aria-label*="непрочит" i]');
    let unread = 0;
    if (badge) { const n = parseInt((badge.textContent || '').trim(), 10); unread = Number.isFinite(n) ? n : 1; }
    const spans = Array.from(row.querySelectorAll('span[dir]')).map(s => (s.textContent || '').trim()).filter(Boolean);
    out.push({ title, unread, last: spans.length > 1 ? spans[spans.length - 1] : '' });
  }
  return out;
}"""

_JS_MESSAGES = """(sel) => {
  const out = [];
  for (const el of document.querySelectorAll(sel.messages)) {
    const id = el.getAttribute('data-id') || '';
    if (!/^(true|false)_/.test(id)) continue;
    const parts = id.split('_');
    const copyable = el.querySelector('[data-pre-plain-text]');
    const pre = copyable ? copyable.getAttribute('data-pre-plain-text') : '';
    const textEl = el.querySelector('span.selectable-text, [data-testid="selectable-text"]');
    const media = el.querySelector('img[src^="blob:"], video, [data-icon*="download"], [data-icon*="audio"], [data-icon*="document"]');
    let kind = '';
    if (el.querySelector('video')) kind = 'video';
    else if (el.querySelector('img[src^="blob:"]')) kind = 'image';
    else if (el.querySelector('[data-icon*="audio"]')) kind = 'audio';
    else if (el.querySelector('[data-icon*="document"], [data-icon*="download"]')) kind = 'file';
    const nameMatch = /\\]\\s*(.*?):\\s*$/.exec(pre || '');
    out.push({
      id, fromMe: parts[0] === 'true', chat: parts[1] || '', sender: parts.length > 3 ? parts.slice(3).join('_') : (parts[0] === 'true' ? '' : parts[1]),
      senderName: nameMatch ? nameMatch[1] : '', time: pre ? pre.replace(/\\].*$/, '').replace('[', '') : '',
      text: textEl ? textEl.innerText : '', media: media ? kind || 'media' : '',
    });
  }
  return out;
}"""

_JS_HEADER = """(sel) => {
  const h = document.querySelector(sel.header);
  if (!h) return null;
  const t = h.querySelector('span[title]');
  return { title: t ? (t.getAttribute('title') || '').trim() : '', text: h.innerText || '' };
}"""

_JS_CLICK_TEXT = """(args) => {
  const want = args.texts.map(s => s.toLowerCase());
  const nodes = Array.from(document.querySelectorAll('[role="button"], button, [role="menuitem"], li, div[role="listitem"] span, span'));
  for (const n of nodes) {
    const label = ((n.getAttribute('aria-label') || '') + ' ' + (n.innerText || n.textContent || '')).trim().toLowerCase();
    if (!label || label.length > 60) continue;
    if (want.some(w => label === w || label.startsWith(w + '\\n'))) {
      if (n.offsetParent === null) continue;
      n.click(); return true;
    }
  }
  return false;
}"""

_JS_DRAWER_TEXT = """() => {
  const sections = Array.from(document.querySelectorAll('#app section, #app [role="dialog"]'));
  const el = sections.length ? sections[sections.length - 1] : null;
  return el ? el.innerText : '';
}"""


class WhatsAppError(Exception):
    pass


@dataclass
class WhatsAppChannelStatus:
    account: str = ""
    linked: bool = False
    connected: bool = False
    own_jid: str | None = None
    last_event_at_iso: str | None = None
    last_error: str | None = None


_status: dict[str, WhatsAppChannelStatus] = {a: WhatsAppChannelStatus(account=a) for a in ACCOUNTS}
_login_requested: dict[str, bool] = {a: False for a in ACCOUNTS}
_locks: dict[str, asyncio.Lock] = {}
_names: dict[str, dict[str, str]] = {}      # account -> {jid: chat title}
_lost_notified: dict[str, bool] = {a: False for a in ACCOUNTS}


def is_valid_account(account: str) -> bool:
    return account in ACCOUNTS


def _check_account(account: str) -> str:
    if account not in ACCOUNTS:
        raise WhatsAppError(f'"account" must be one of {", ".join(ACCOUNTS)}, not {account!r}.')
    return account


def _label(account: str) -> str:
    return f"whatsapp_{account}"


def _state_dir() -> Path:
    d = messenger_secrets_dir(WORKSPACE_DIR, "whatsapp")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _marker(account: str) -> Path:
    return _state_dir() / f"browser_linked_{account}"


def _has_prior_session(workspace_dir: str, account: str) -> bool:
    return _marker(account).exists()


def _names_file(account: str) -> Path:
    return _state_dir() / f"chats_{account}.json"


def _load_names(account: str) -> dict[str, str]:
    if account not in _names:
        try:
            _names[account] = json.loads(_names_file(account).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _names[account] = {}
    return _names[account]


def _remember(account: str, jid: str, title: str) -> None:
    if not jid or not title:
        return
    names = _load_names(account)
    if names.get(jid) != title:
        names[jid] = title
        try:
            _names_file(account).write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


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


def get_whatsapp_channel_status() -> dict[str, dict[str, Any]]:
    return {account: asdict(status) for account, status in _status.items()}


def is_connected(account: str) -> bool:
    return _status[account].connected and browser.get_page(_label(account)) is not None


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
    """The account's page, in its chat view, or an error saying what to do."""
    _check_account(account)
    page = browser.get_page(_label(account))
    if page is None:
        raise WhatsAppError(f'WhatsApp ("{account}") isn\'t connected yet -- use whatsapp_login(account="{account}") first.')
    state = await _page_state(page)
    if state != "ready":
        raise WhatsAppError(
            f'WhatsApp ("{account}") is not signed in right now ({state}) -- use whatsapp_login(account="{account}") '
            "and scan the code in the window that opens."
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


def _digits_of(jid: str) -> str:
    return "".join(c for c in jid.split("@")[0] if c.isdigit())


def _chat_list_id(account: str, title: str) -> str:
    for jid, name in _load_names(account).items():
        if name == title:
            return jid
    return f"{title}{NAME_JID_SUFFIX}"


async def _open_chat(account: str, page: Any, to: str) -> str:
    """Open the chat for a jid, a phone jid or "<title>@chat.name"; the chat's title."""
    if to.endswith("@s.whatsapp.net") and not _load_names(account).get(to):
        digits = _digits_of(to)
        await page.goto(f"{WHATSAPP_URL}send?phone={digits}", wait_until="domcontentloaded")
        if not await _wait_for(page, _SEL["composer"], 25.0):
            raise WhatsAppError(f"{digits} could not be opened as a WhatsApp chat (not on WhatsApp?).")
        head = await page.evaluate(_JS_HEADER, _SEL)
        return (head or {}).get("title") or digits
    title = to[: -len(NAME_JID_SUFFIX)] if to.endswith(NAME_JID_SUFFIX) else _load_names(account).get(to)
    if not title:
        raise WhatsAppError(f"The chat {to} is not known yet: run whatsapp_list_chats first, then use the id it gives.")
    box = page.locator(_SEL["search"]).first
    await box.click()
    await page.keyboard.press("Control+A")
    await page.keyboard.insert_text(title)
    await page.wait_for_timeout(1500)
    rows = await page.evaluate(_JS_CHAT_LIST, _SEL)
    exact = [r for r in rows if r["title"] == title]
    if not rows:
        raise WhatsAppError(f'No chat named "{title}" found.')
    target = (exact or rows)[0]["title"]
    await page.locator(_SEL["rows"]).filter(has=page.locator(f'span[title="{target}"]')).first.click()
    await page.keyboard.press("Escape")
    if not await _wait_for(page, _SEL["composer"], 10.0):
        raise WhatsAppError(f'The chat "{target}" did not open.')
    return target


async def _type_into_composer(page: Any, text: str) -> None:
    composer = page.locator(_SEL["composer"]).first
    await composer.click()
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line:
            await page.keyboard.insert_text(line)
        if i < len(lines) - 1:
            await page.keyboard.press("Shift+Enter")


async def _read_messages(page: Any) -> list[dict[str, Any]]:
    return await page.evaluate(_JS_MESSAGES, _SEL)


# -- the functions whatsapp_plugin.py calls --------------------------------------------------------

async def send_message(account: str, to: str, text: str) -> None:
    async with _lock(account):
        page = await _ready_page(account)
        await _open_chat(account, page, to)
        before = {m["id"] for m in await _read_messages(page) if m["fromMe"]}
        await _type_into_composer(page, text)
        await page.keyboard.press("Enter")
        deadline = time.monotonic() + ACTION_TIMEOUT_S
        while time.monotonic() < deadline:
            await page.wait_for_timeout(500)
            if {m["id"] for m in await _read_messages(page) if m["fromMe"]} - before:
                return
        raise WhatsAppError("The message was typed but no new sent message appeared on the page within 30 s.")


async def list_chats(account: str) -> list[dict[str, Any]]:
    async with _lock(account):
        page = await _ready_page(account)
        rows = await page.evaluate(_JS_CHAT_LIST, _SEL)
    return [{"id": _chat_list_id(account, r["title"]), "name": r["title"], "unread": r["unread"], "lastMessage": r["last"]} for r in rows]


async def check_number(account: str, phone: str) -> tuple[bool, str | None]:
    """Whether a number has WhatsApp: its deep link opens a chat, or an "invalid" dialog."""
    async with _lock(account):
        page = await _ready_page(account)
        await page.goto(f"{WHATSAPP_URL}send?phone={phone}", wait_until="domcontentloaded")
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            if await page.locator(_SEL["composer"]).count():
                return True, f"{phone}@s.whatsapp.net"
            dialog = await page.evaluate("() => { const d = document.querySelector('[role=\"dialog\"], [data-animate-modal-popup]'); return d ? d.innerText : ''; }")
            if dialog and any(w.lower() in dialog.lower() for w in _TXT["invalid_number"]):
                return False, None
            await page.wait_for_timeout(500)
    raise WhatsAppError("WhatsApp did not answer whether the number exists within 25 s.")


async def _open_info(page: Any) -> str:
    await page.locator(_SEL["header"]).first.click()
    await page.wait_for_timeout(1200)
    return await page.evaluate(_JS_DRAWER_TEXT)


async def get_info(account: str, jid: str) -> dict[str, Any]:
    """What the chat's info panel shows: for a person their name, number and "about" text, for a
    group its subject, description and members (the panel's own text, parsed no further)."""
    async with _lock(account):
        page = await _ready_page(account)
        title = await _open_chat(account, page, jid)
        panel = await _open_info(page)
        await page.keyboard.press("Escape")
    kind = "group" if jid.endswith("@g.us") or re.search(r"\b(participants|участник)", panel, re.I) else "person"
    info: dict[str, Any] = {"kind": kind, "name": title, "id": jid, "panelText": panel}
    if kind == "group":
        members = [ln.strip() for ln in panel.splitlines() if ln.strip()]
        info["participants"] = [{"id": m} for m in members]
    return info


async def _block_flow(account: str, jid: str, block: bool) -> None:
    async with _lock(account):
        page = await _ready_page(account)
        await _open_chat(account, page, jid)
        await _open_info(page)
        key = "block" if block else "unblock"
        if not await _click_text(page, key):
            raise WhatsAppError(f"The {key} button was not found in the chat's info panel (already {'blocked' if block else 'unblocked'}?).")
        await page.wait_for_timeout(800)
        await _click_text(page, "confirm")
        await page.wait_for_timeout(800)
        await page.keyboard.press("Escape")


async def set_block(account: str, jid: str, block: bool) -> None:
    await _block_flow(account, jid, block)


async def download_media(account: str, ref: str, save_path: str | None = None) -> str:
    """ref is a message id (data-id) from a notification: the chat is opened, the message
    found, and its download started; the file is saved to save_path."""
    if not save_path:
        WHATSAPP_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        save_path = str(WHATSAPP_DOWNLOADS_DIR / re.sub(r"[^A-Za-z0-9_.-]", "_", ref))
    chat = ref.split("_")[1] if "_" in ref else ""
    async with _lock(account):
        page = await _ready_page(account)
        if chat:
            await _open_chat(account, page, chat if "@" in chat else f"{chat}@s.whatsapp.net")
        message = page.locator(f'#main [data-id="{ref}"]').first
        if not await message.count():
            raise WhatsAppError("That message is not on the page any more (scroll history is not kept): ask for the file to be sent again.")
        await message.scroll_into_view_if_needed()
        target = Path(save_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        icon = message.locator('[data-icon*="download"]').first
        try:
            async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_S * 1000) as info:
                if await icon.count():
                    await icon.click()
                else:
                    await message.locator('img[src^="blob:"], video').first.click()
                    await page.locator('[data-icon="download"]').first.click()
            download = await info.value
            await download.save_as(str(target))
        except Exception as exc:
            raise WhatsAppError(f"The attachment could not be downloaded: {exc}") from exc
        finally:
            await page.keyboard.press("Escape")
    return str(target)


async def send_file(account: str, to: str, path: str, caption: str | None = None) -> None:
    if not Path(path).is_file():
        raise WhatsAppError(f"No such local file: {path}")
    async with _lock(account):
        page = await _ready_page(account)
        await _open_chat(account, page, to)
        await page.locator(_SEL["attach"]).first.click()
        await page.wait_for_timeout(500)
        inputs = page.locator(_SEL["file_input"])
        count = await inputs.count()
        if not count:
            raise WhatsAppError("The attach menu did not offer a file input.")
        # The document input accepts anything; photos/videos use the one that accepts image/*.
        chosen = inputs.first
        for i in range(count):
            accept = await inputs.nth(i).get_attribute("accept") or ""
            if accept in ("*", ""):
                chosen = inputs.nth(i)
                break
        await chosen.set_input_files(path)
        await page.wait_for_timeout(1500)
        if caption:
            await page.keyboard.insert_text(caption)
        if await page.locator(_SEL["send"]).count():
            await page.locator(_SEL["send"]).first.click()
        else:
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(1500)


async def create_group(account: str, subject: str, participants: list[str]) -> str:
    async with _lock(account):
        page = await _ready_page(account)
        names = _load_names(account)
        if not await _click_text(page, "new_chat"):
            await page.locator('[data-icon="new-chat-outline"], [data-icon="chat"]').first.click()
        await page.wait_for_timeout(800)
        if not await _click_text(page, "new_group"):
            raise WhatsAppError("The New group entry was not found.")
        await page.wait_for_timeout(800)
        for who in participants:
            label = names.get(who) or (who[: -len(NAME_JID_SUFFIX)] if who.endswith(NAME_JID_SUFFIX) else _digits_of(who))
            await page.keyboard.insert_text(label)
            await page.wait_for_timeout(1000)
            await page.keyboard.press("Enter")
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
        await page.locator('[data-icon="arrow-forward"], [data-icon="forward"]').first.click()
        await page.wait_for_timeout(600)
        await page.keyboard.insert_text(subject)
        if not await _click_text(page, "create"):
            await page.locator('[data-icon="checkmark-medium"], [data-icon="checkmark"]').first.click()
        await page.wait_for_timeout(2500)
        await page.keyboard.press("Escape")
        rows = await page.evaluate(_JS_CHAT_LIST, _SEL)
        if not any(r["title"] == subject for r in rows):
            raise WhatsAppError(f'The group "{subject}" did not appear in the chat list.')
    return f"{subject}{NAME_JID_SUFFIX}"


async def update_group_participants(account: str, jid: str, participants: list[str], action: str) -> None:
    async with _lock(account):
        page = await _ready_page(account)
        names = _load_names(account)
        await _open_chat(account, page, jid)
        await _open_info(page)
        for who in participants:
            label = names.get(who) or (who[: -len(NAME_JID_SUFFIX)] if who.endswith(NAME_JID_SUFFIX) else _digits_of(who))
            if action == "add":
                if not await _click_text(page, "add_participant"):
                    raise WhatsAppError("The Add member entry was not found (are you an admin of this group?).")
                await page.wait_for_timeout(600)
                await page.keyboard.insert_text(label)
                await page.wait_for_timeout(1000)
                await page.keyboard.press("Enter")
                await _click_text(page, "confirm")
                await page.wait_for_timeout(800)
                continue
            row = page.locator('section [role="listitem"], section [role="button"]').filter(has_text=label).first
            if not await row.count():
                raise WhatsAppError(f"{label} was not found among the group's members.")
            await row.hover()
            await row.click(button="right")
            await page.wait_for_timeout(500)
            key = {"remove": "remove_member", "promote": "make_admin", "demote": "dismiss_admin"}[action]
            if not await _click_text(page, key):
                raise WhatsAppError(f"The '{action}' entry was not offered for {label} (are you an admin?).")
            await page.wait_for_timeout(600)
            await _click_text(page, "confirm")
            await page.wait_for_timeout(800)
        await page.keyboard.press("Escape")


async def leave_group(account: str, jid: str) -> None:
    async with _lock(account):
        page = await _ready_page(account)
        await _open_chat(account, page, jid)
        await _open_info(page)
        if not await _click_text(page, "exit_group"):
            raise WhatsAppError("The Exit group entry was not found.")
        await page.wait_for_timeout(600)
        await _click_text(page, "confirm")
        await page.wait_for_timeout(800)
        await page.keyboard.press("Escape")


async def delete_group(account: str, jid: str) -> None:
    """WhatsApp has no real "delete this group": every other member is removed, then she leaves."""
    info = await get_info(account, jid)
    own = {"you", "вы"}
    others = [p["id"] for p in info.get("participants", []) if p["id"].strip().lower() not in own]
    if others:
        await update_group_participants(account, jid, [_names_key(account, o) for o in others[:256]], "remove")
    await leave_group(account, jid)


def _names_key(account: str, label: str) -> str:
    return f"{label}{NAME_JID_SUFFIX}"


# -- sign-in and the watch ---------------------------------------------------------------------

async def start_login(workspace_dir: str, account: str) -> str:
    """Open the account's profile in a VISIBLE window at web.whatsapp.com for the person to scan
    the QR; the watch loop notices the chat list and then moves the profile to headless."""
    _check_account(account)
    if is_connected(account):
        return f'Already connected (account="{account}").'
    _login_requested[account] = True
    _lost_notified[account] = False
    await browser.open_window(_label(account), WHATSAPP_URL, headless=False)
    return (
        f'A WhatsApp window is open for the "{account}" account. Scan its QR code with WhatsApp on the phone for that '
        "account (Settings > Linked devices > Link a device). Once the chats show, the window can be left alone: it "
        "is closed and the account carries on quietly in the background -- you sign in only this once."
    )


def _note_for(account: str, msg: dict[str, Any], chat_title: str) -> str:
    frm = msg.get("chat") or _chat_list_id(account, chat_title)
    name = third_party_label(chat_title or frm)
    sender = msg.get("sender") or frm
    who = f"sender={third_party_label(sender)}"
    if msg.get("senderName"):
        who += f', senderName="{third_party_label(msg["senderName"])}"'
    text = (msg.get("text") or "").strip()
    note = (
        f'[New WhatsApp message in {_ACCOUNT_LABEL[account]} WhatsApp account, from "{name}" '
        f'(jid={frm}, {who}, account="{account}"):\n'
        f'{third_party_text(text, name) if text else "(no caption)"}'
    )
    if msg.get("media"):
        note += (
            f'\nAttached: {msg["media"]}. Use whatsapp_download_attachment(ref="{msg["id"]}", '
            f'account="{account}") to save it locally first.'
        )
    if account == "self":
        note += (
            "\nThis is Caroline's own separate WhatsApp account, not the user's -- replying here is "
            "Caroline's own correspondence, not something done on the user's behalf, and needs no extra "
            f'permission. Reply with whatsapp_send_message(to="{frm}", text=..., account="self") if it needs a response.]'
        )
    else:
        note += (
            "\nThis is the OWNER'S OWN correspondence, not yours -- read it and tell the owner about it "
            "if it matters, but do not reply as them unless they have explicitly asked you to send this "
            f'specific reply (whatsapp_send_message(to="{frm}", text=..., account="owner")).]'
        )
    return note


async def _collect_new(account: str, page: Any, baseline: dict[str, int], inject: Callable[[str], None]) -> None:
    """Open every chat whose unread count rose, read its new incoming messages, make notes."""
    rows = await page.evaluate(_JS_CHAT_LIST, _SEL)
    seen = _load_seen(account)
    seen_set = set(seen)
    for row in rows:
        title, unread = row["title"], int(row["unread"])
        before = baseline.get(title, 0)
        baseline[title] = unread
        if unread <= before:
            continue
        try:
            await page.locator(_SEL["rows"]).filter(has=page.locator(f'span[title="{title}"]')).first.click()
            if not await _wait_for(page, _SEL["messages"], 8.0):
                continue
            await page.wait_for_timeout(600)
            messages = await _read_messages(page)
        except Exception as exc:
            log_event("plugin:whatsapp", "read_chat_failed", account=account, error=str(exc)[:200])
            continue
        incoming = [m for m in messages if not m["fromMe"]]
        for m in incoming:
            _remember(account, m["chat"], title)
        fresh = [m for m in incoming if m["id"] not in seen_set][-(unread - before):]
        for m in fresh:
            if not (m.get("text") or "").strip() and not m.get("media"):
                continue
            seen_set.add(m["id"])
            seen.append(m["id"])
            _status[account].last_event_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            log_event("plugin:whatsapp", "message_received", account=account, frm=m["chat"])
            inject(_note_for(account, m, title))
        baseline[title] = 0
    _save_seen(account, seen)


async def _watch(workspace_dir: str, account: str, inject: Callable[[str], None], touch: Callable[[], None]) -> None:
    label = _label(account)
    status = _status[account]
    headless = not _login_requested[account]
    await browser.open_window(label, WHATSAPP_URL, headless=headless)
    loading_since = time.monotonic()
    baseline: dict[str, int] | None = None
    while True:
        touch()
        page = browser.get_page(label)
        if page is None:
            _login_requested[account] = False   # the person closed the sign-in window: it is not reopened by itself
            raise WhatsAppError("the WhatsApp browser window was closed")
        state = await _page_state(page)
        if state == "ready":
            loading_since = time.monotonic()
            _lost_notified[account] = False
            if not status.linked:
                _marker(account).write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
                log_event("plugin:whatsapp", "connected", account=account)
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
                    baseline = {r["title"]: int(r["unread"]) for r in await page.evaluate(_JS_CHAT_LIST, _SEL)}
                else:
                    await _collect_new(account, page, baseline, inject)
        elif state == "qr":
            loading_since = time.monotonic()
            status.connected = False
            if browser.is_headless(label) and status.linked and not _lost_notified[account]:
                status.linked = False
                _lost_notified[account] = True
                _marker(account).unlink(missing_ok=True)
                log_event("plugin:whatsapp", "session_lost", account=account)
                inject(
                    f'[WhatsApp ("{account}", {_ACCOUNT_LABEL[account]} account) has signed out: the phone unlinked it or '
                    f'the session expired. Tell the user once, and ask them to run whatsapp_login(account="{account}") '
                    "and scan the code again.]"
                )
        else:
            status.connected = False
            if time.monotonic() - loading_since > LOADING_GIVE_UP_S:
                raise WhatsAppError("web.whatsapp.com did not finish loading")
        await asyncio.sleep(POLL_INTERVAL_S)


def start_whatsapp_channel(workspace_dir: str, inject_proactive: Callable[[str], None]) -> list[asyncio.Task[None]]:
    """One supervised loop per account: with a kept sign-in (or after whatsapp_login) the profile
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
                except WhatsAppError as exc:
                    _status[account].last_error = str(exc)
                    _status[account].connected = False
                    log_event("plugin:whatsapp", "watch_unavailable", account=account, error=str(exc))
                    touch()
                    await asyncio.sleep(60.0)
                    continue
                except Exception as exc:
                    _status[account].last_error = str(exc)
                    _status[account].connected = False
                    log_event("plugin:whatsapp", "watch_failed", account=account, error=str(exc)[:300])
                touch()
                await asyncio.sleep(5.0)
        return _loop

    return [
        supervise(f"whatsapp_channel_{account}", _make_loop(account), heartbeat_timeout_s=CHANNEL_HEARTBEAT_TIMEOUT_S)
        for account in ACCOUNTS
    ]
