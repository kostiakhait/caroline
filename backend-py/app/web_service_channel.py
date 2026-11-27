"""app/web_service_channel.py -- Facebook and LinkedIn through Caroline's own browser.

The same pattern as whatsapp_channel.py and messenger_channel.py (docs/MESSENGERS.md): the person
signs in ONCE in a visible window of Caroline's own Chromium, then the same profile is opened
headless and watched. A service here is a ServiceSpec: where it lives, how to tell "signed in"
from "login page", and one function that reads what is new (notifications, unread messages,
invitations) and returns notes for her. The tools (facebook_*, linkedin_*) work in the same
profile through the helpers below.

TWO accounts per service, like the messengers: "self" (her own) and "owner" (the person's; acting
as them only on request for that specific thing). Labels in the browser: "<service>_<account>".

Selectors and in-page scripts are in the specs at the bottom of this file: when a site changes its
page, that is the place to change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.logging_setup import log_event
from app.plugins import _chromium_app_browser as browser
from app.secret_store import messenger_secrets_dir
from app.task_supervisor import supervise
from app.untrusted_text import third_party_label, third_party_text
from app.workspace_dir import WORKSPACE_DIR

ACCOUNTS = ("self", "owner")
DEFAULT_ACCOUNT = "owner"
_ACCOUNT_LABEL = {"self": "your own", "owner": "the owner's"}
LOADING_GIVE_UP_S = 180.0
CHANNEL_HEARTBEAT_TIMEOUT_S = 300.0
SEEN_CAP = 2000


class ServiceError(Exception):
    pass


@dataclass
class ServiceSpec:
    key: str                      # "facebook", "linkedin"
    title: str                    # "Facebook", "LinkedIn"
    url: str                      # where the signed-in view lives
    state_js: str                 # (page) -> 'ready' | 'login' | 'loading'
    poll_s: float                 # how often the watch looks for news
    collect: Callable[[Any], Awaitable[list[dict[str, Any]]]]   # page -> [{key, kind, text, who, url}]
    pages: dict[str, str] = field(default_factory=dict)


@dataclass
class ServiceStatus:
    account: str = ""
    linked: bool = False
    connected: bool = False
    last_event_at_iso: str | None = None
    last_error: str | None = None


_status: dict[tuple[str, str], ServiceStatus] = {}
_login_requested: dict[tuple[str, str], bool] = {}
_lost_notified: dict[tuple[str, str], bool] = {}
_locks: dict[tuple[str, str], asyncio.Lock] = {}
SPECS: dict[str, ServiceSpec] = {}


def register(spec: ServiceSpec) -> None:
    SPECS[spec.key] = spec
    for a in ACCOUNTS:
        _status[(spec.key, a)] = ServiceStatus(account=a)
        _login_requested[(spec.key, a)] = False
        _lost_notified[(spec.key, a)] = False


def is_valid_account(account: str) -> bool:
    return account in ACCOUNTS


def label_of(service: str, account: str) -> str:
    return f"{service}_{account}"


def get_status(service: str) -> dict[str, dict[str, Any]]:
    return {a: vars(_status[(service, a)]).copy() for a in ACCOUNTS}


def _state_dir(service: str) -> Path:
    d = messenger_secrets_dir(WORKSPACE_DIR, service)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _marker(service: str, account: str) -> Path:
    return _state_dir(service) / f"browser_linked_{account}"


def _seen_file(service: str, account: str) -> Path:
    return _state_dir(service) / f"seen_{account}.json"


def _load_seen(service: str, account: str) -> list[str]:
    try:
        return list(json.loads(_seen_file(service, account).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def _save_seen(service: str, account: str, seen: list[str]) -> None:
    try:
        _seen_file(service, account).write_text(json.dumps(seen[-SEEN_CAP:]), encoding="utf-8")
    except OSError:
        pass


def lock_of(service: str, account: str) -> asyncio.Lock:
    key = (service, account)
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


def is_connected(service: str, account: str) -> bool:
    return _status[(service, account)].connected and browser.get_page(label_of(service, account)) is not None


async def page_state(spec: ServiceSpec, page: Any) -> str:
    try:
        return await page.evaluate(spec.state_js)
    except Exception:
        return "loading"


async def ready_page(service: str, account: str) -> Any:
    """The account's page, signed in, or an error saying what to do."""
    spec = SPECS[service]
    if account not in ACCOUNTS:
        raise ServiceError(f'"account" must be one of {", ".join(ACCOUNTS)}, not {account!r}.')
    page = browser.get_page(label_of(service, account))
    if page is None:
        raise ServiceError(f'{spec.title} ("{account}") isn\'t connected yet -- use {service}_login(account="{account}") first.')
    state = await page_state(spec, page)
    if state != "ready":
        raise ServiceError(
            f'{spec.title} ("{account}") is not signed in right now ({state}) -- use {service}_login(account="{account}") '
            "and log in in the window that opens."
        )
    return page


async def goto_ready(service: str, account: str, url: str, wait_selector: str | None = None, timeout_s: float = 20.0) -> Any:
    page = await ready_page(service, account)
    if page.url.split("#")[0].rstrip("/") != url.rstrip("/"):
        await page.goto(url, wait_until="domcontentloaded")
    if wait_selector:
        try:
            await page.wait_for_selector(wait_selector, timeout=timeout_s * 1000)
        except Exception:
            pass
    await page.wait_for_timeout(800)
    return page


async def click_text(page: Any, texts: list[str], within: str | None = None) -> bool:
    return bool(await page.evaluate(
        """(a) => {
          const want = a.texts.map(s => s.toLowerCase());
          const root = a.within ? document.querySelector(a.within) : document;
          if (!root) return false;
          for (const n of root.querySelectorAll('[role="button"], button, [role="menuitem"], a, span')) {
            const label = ((n.getAttribute('aria-label') || '') + ' ' + (n.innerText || n.textContent || '')).trim().toLowerCase();
            if (!label || label.length > 60) continue;
            if (want.some(w => label === w || label.startsWith(w + '\\n'))) { if (n.offsetParent === null) continue; n.click(); return true; }
          }
          return false;
        }""",
        {"texts": texts, "within": within},
    ))


async def start_login(service: str, account: str) -> str:
    spec = SPECS[service]
    if account not in ACCOUNTS:
        raise ServiceError(f'"account" must be one of {", ".join(ACCOUNTS)}, not {account!r}.')
    if is_connected(service, account):
        return f'Already connected (account="{account}").'
    _login_requested[(service, account)] = True
    _lost_notified[(service, account)] = False
    await browser.open_window(label_of(service, account), spec.url, headless=False)
    return (
        f'A {spec.title} window is open for the "{account}" account -- log in there yourself (including any 2FA code or '
        "check the site asks for). No need to tell me anything back: once you are in, the window goes to the background "
        "and I carry on quietly -- you sign in only this once."
    )


def _note(spec: ServiceSpec, account: str, item: dict[str, Any]) -> str:
    who = third_party_label(item.get("who") or spec.title)
    text = (item.get("text") or "").strip()
    note = (
        f'[New {spec.title} {item.get("kind", "item")} in {_ACCOUNT_LABEL[account]} {spec.title} account'
        + (f', from "{who}"' if item.get("who") else "")
        + (f', url={item["url"]}' if item.get("url") else "")
        + f', account="{account}"):\n{third_party_text(text, who)}'
    )
    if account == "self":
        note += f"\nThis is Caroline's own separate {spec.title} account, not the user's.]"
    else:
        note += (
            f"\nThis is the OWNER'S OWN {spec.title} account, not yours -- read it and tell the owner about it if it "
            "matters, but do not post, reply or accept anything as them unless they have explicitly asked for that "
            "specific action.]"
        )
    return note


async def _watch(spec: ServiceSpec, account: str, inject: Callable[[str], None], touch: Callable[[], None]) -> None:
    service, label = spec.key, label_of(spec.key, account)
    ident = (service, account)
    status = _status[ident]
    await browser.open_window(label, spec.url, headless=not _login_requested[ident])
    loading_since = time.monotonic()
    first = True
    last_collect = 0.0
    while True:
        touch()
        page = browser.get_page(label)
        if page is None:
            _login_requested[ident] = False   # the sign-in window was closed: not reopened by itself
            raise ServiceError(f"the {spec.title} browser window was closed")
        state = await page_state(spec, page)
        if state == "ready":
            loading_since = time.monotonic()
            _lost_notified[ident] = False
            if not status.linked:
                _marker(service, account).write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
                log_event("plugin:webservice", "connected", service=service, account=account)
            status.linked = status.connected = True
            status.last_error = None
            if browser.is_headless(label) is False and _login_requested[ident]:
                _login_requested[ident] = False
                await browser.set_mode(label, True)
                first = True
                await asyncio.sleep(3.0)
                continue
            if time.monotonic() - last_collect >= spec.poll_s:
                async with lock_of(service, account):
                    items = await spec.collect(page)
                last_collect = time.monotonic()
                seen = _load_seen(service, account)
                seen_set = set(seen)
                fresh = [i for i in items if i["key"] not in seen_set]
                for i in fresh:
                    seen.append(i["key"])
                    seen_set.add(i["key"])
                if first:
                    first = False          # what was already there at the start is not news
                else:
                    for i in fresh:
                        status.last_event_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                        log_event("plugin:webservice", "item_received", service=service, account=account, kind=i.get("kind"))
                        inject(_note(spec, account, i))
                _save_seen(service, account, seen)
        elif state == "login":
            loading_since = time.monotonic()
            status.connected = False
            if browser.is_headless(label) and status.linked and not _lost_notified[ident]:
                status.linked = False
                _lost_notified[ident] = True
                _marker(service, account).unlink(missing_ok=True)
                log_event("plugin:webservice", "session_lost", service=service, account=account)
                inject(
                    f'[{spec.title} ("{account}", {_ACCOUNT_LABEL[account]} account) has signed out: the site ended the '
                    f'session. Tell the user once, and ask them to run {service}_login(account="{account}") and log in again.]'
                )
        else:
            status.connected = False
            if time.monotonic() - loading_since > LOADING_GIVE_UP_S:
                raise ServiceError(f"{spec.title} did not finish loading")
        await asyncio.sleep(8.0)


def start_service_channels(workspace_dir: str, inject_proactive: Callable[[str], None]) -> list[asyncio.Task[None]]:
    """One supervised loop per service and account: with a kept sign-in (or after <service>_login)
    the profile is opened and watched; otherwise it waits, cheaply."""
    def _make_loop(spec: ServiceSpec, account: str) -> Callable[[Callable[[], None]], Any]:
        ident = (spec.key, account)

        async def _loop(touch: Callable[[], None]) -> None:
            while True:
                if not (_login_requested[ident] or _marker(spec.key, account).exists()):
                    touch()
                    await asyncio.sleep(30.0)
                    continue
                try:
                    await _watch(spec, account, inject_proactive, touch)
                except ServiceError as exc:
                    _status[ident].last_error = str(exc)
                    _status[ident].connected = False
                    log_event("plugin:webservice", "watch_unavailable", service=spec.key, account=account, error=str(exc))
                    touch()
                    await asyncio.sleep(60.0)
                    continue
                except Exception as exc:
                    _status[ident].last_error = str(exc)
                    _status[ident].connected = False
                    log_event("plugin:webservice", "watch_failed", service=spec.key, account=account, error=str(exc)[:300])
                touch()
                await asyncio.sleep(5.0)
        return _loop

    return [
        supervise(f"{spec.key}_channel_{account}", _make_loop(spec, account), heartbeat_timeout_s=CHANNEL_HEARTBEAT_TIMEOUT_S)
        for spec in SPECS.values() for account in ACCOUNTS
    ]


def digest(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


# -- Facebook ----------------------------------------------------------------------------------------

FACEBOOK_STATE_JS = """() => {
  if (document.querySelector('input[name="email"], input[name="pass"], form[action*="login"]')) return 'login';
  if (document.querySelector('[role="banner"] [aria-label*="Notifications" i], [role="banner"] [aria-label*="Уведомлени" i], [aria-label="Facebook"][role="navigation"], [role="navigation"][aria-label*="Facebook"], [role="banner"]')) return 'ready';
  return 'loading';
}"""

NOTIFICATIONS_JS = """() => {
  const out = [], seen = new Set();
  const root = document.querySelector('[role="main"]') || document.body;
  const nodes = root.querySelectorAll('[role="listitem"], [role="article"], a[href*="notif"], a[role="link"]');
  for (const n of nodes) {
    const text = (n.innerText || '').trim().replace(/\\s*\\n\\s*/g, ' · ');
    if (text.length < 12 || seen.has(text)) continue;
    seen.add(text);
    const a = n.tagName === 'A' ? n : n.querySelector('a[href]');
    out.push({ text: text.slice(0, 600), url: a ? a.href : '' });
    if (out.length >= 40) break;
  }
  return out;
}"""


async def facebook_notifications(account: str) -> list[dict[str, Any]]:
    async with lock_of("facebook", account):
        page = await goto_ready("facebook", account, "https://www.facebook.com/notifications", '[role="main"]')
        await page.wait_for_timeout(1500)
        return await page.evaluate(NOTIFICATIONS_JS)


async def _facebook_collect(page: Any) -> list[dict[str, Any]]:
    await page.goto("https://www.facebook.com/notifications", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector('[role="main"]', timeout=15000)
    except Exception:
        return []
    await page.wait_for_timeout(2000)
    rows = await page.evaluate(NOTIFICATIONS_JS)
    return [{"key": digest("fb", r["text"]), "kind": "notification", "text": r["text"], "url": r["url"], "who": ""} for r in rows]


async def facebook_read_page(account: str, url: str) -> str:
    if not url.startswith("https://www.facebook.com/") and not url.startswith("https://facebook.com/"):
        raise ServiceError("facebook_read_page reads facebook.com addresses only.")
    async with lock_of("facebook", account):
        page = await goto_ready("facebook", account, url, '[role="main"]')
        await page.wait_for_timeout(1500)
        return await page.evaluate("() => { const m = document.querySelector('[role=\"main\"]') || document.body; return (m.innerText || '').slice(0, 20000); }")


async def facebook_post(account: str, text: str) -> None:
    async with lock_of("facebook", account):
        page = await goto_ready("facebook", account, "https://www.facebook.com/", '[role="main"]')
        if not await click_text(page, ["What's on your mind?", "Что у вас нового?", "О чем вы думаете?", "Create post", "Создать публикацию"]):
            opener = page.locator('[role="main"] [role="button"]').filter(has_text="mind").first
            if not await opener.count():
                raise ServiceError("The 'What's on your mind' box was not found on the page.")
            await opener.click()
        await page.wait_for_selector('[role="dialog"] [contenteditable="true"]', timeout=15000)
        await page.locator('[role="dialog"] [contenteditable="true"]').first.click()
        await page.keyboard.insert_text(text)
        await page.wait_for_timeout(800)
        if not await click_text(page, ["Post", "Опубликовать"], '[role="dialog"]'):
            raise ServiceError("The Post button was not found.")
        await page.wait_for_timeout(4000)
        if await page.locator('[role="dialog"] [contenteditable="true"]').count():
            raise ServiceError("The post dialog is still open: Facebook did not publish it.")


async def facebook_comment(account: str, post_url: str, text: str) -> None:
    async with lock_of("facebook", account):
        page = await goto_ready("facebook", account, post_url, '[role="main"]')
        box = page.locator('[contenteditable="true"][aria-label*="comment" i], [contenteditable="true"][aria-label*="комментар" i]').first
        if not await box.count():
            raise ServiceError("The comment box was not found on that post.")
        await box.click()
        await page.keyboard.insert_text(text)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(2500)


# -- LinkedIn ----------------------------------------------------------------------------------------

LINKEDIN_STATE_JS = """() => {
  if (document.querySelector('input#username, form.login__form, input[name="session_key"], a[href*="/login"][data-tracking-control-name*="sign-in"]') && !document.querySelector('#global-nav, .global-nav')) return 'login';
  if (document.querySelector('#global-nav, nav.global-nav, .global-nav__me')) return 'ready';
  if (location.pathname.startsWith('/login') || location.pathname.startsWith('/uas/') || location.pathname.startsWith('/checkpoint')) return 'login';
  return 'loading';
}"""

LINKEDIN_NOTIFICATIONS_JS = """() => {
  const out = [], seen = new Set();
  const root = document.querySelector('main') || document.body;
  for (const n of root.querySelectorAll('.nt-card, article, [data-finite-scroll-hotkey-item], li')) {
    const text = (n.innerText || '').trim().replace(/\\s*\\n\\s*/g, ' · ');
    if (text.length < 12 || seen.has(text)) continue;
    seen.add(text);
    const a = n.querySelector('a[href]');
    out.push({ text: text.slice(0, 600), url: a ? a.href : '' });
    if (out.length >= 40) break;
  }
  return out;
}"""

LINKEDIN_CONVERSATIONS_JS = """() => {
  const out = [];
  for (const li of document.querySelectorAll('.msg-conversation-listitem, li.msg-conversation-card, [class*="msg-conversation-listitem"]')) {
    const a = li.querySelector('a[href*="/messaging/thread/"]');
    const m = a ? /\\/thread\\/([^\\/?]+)/.exec(a.getAttribute('href') || '') : null;
    if (!m) continue;
    const name = (li.querySelector('.msg-conversation-listitem__participant-names, .msg-conversation-card__participant-names, h3') || {}).innerText || '';
    const preview = (li.querySelector('.msg-conversation-card__message-snippet, .msg-conversation-listitem__message-snippet, p') || {}).innerText || '';
    const unread = !!li.querySelector('.notification-badge--show, .msg-conversation-card__unread-count, [class*="unread"]');
    out.push({ id: m[1], name: name.trim(), preview: preview.trim(), unread });
  }
  return out;
}"""

LINKEDIN_THREAD_JS = """() => {
  const out = [];
  for (const e of document.querySelectorAll('.msg-s-event-listitem')) {
    const body = (e.querySelector('.msg-s-event-listitem__body') || {}).innerText || '';
    const who = (e.querySelector('.msg-s-message-group__name, .msg-s-event-listitem__name') || {}).innerText || '';
    const t = (e.querySelector('time, .msg-s-message-group__timestamp') || {}).innerText || '';
    if (body.trim()) out.push({ who: who.trim(), time: t.trim(), text: body.trim(), mine: !!e.closest('.msg-s-message-list__event--mine, [class*="--self"]') });
  }
  return out;
}"""

LINKEDIN_INVITATIONS_JS = """() => {
  const out = [];
  for (const c of document.querySelectorAll('[componentkey*="invitation"], li.invitation-card, [class*="invitation-card"], section li')) {
    const text = (c.innerText || '').trim().replace(/\\s*\\n\\s*/g, ' · ');
    if (text.length < 8 || !/accept|принять/i.test(c.innerHTML + text)) continue;
    const a = c.querySelector('a[href*="/in/"]');
    out.push({ name: ((a && a.innerText) || text.split(' · ')[0]).trim(), text: text.slice(0, 400), url: a ? a.href : '' });
  }
  return out;
}"""


async def linkedin_notifications(account: str) -> list[dict[str, Any]]:
    async with lock_of("linkedin", account):
        page = await goto_ready("linkedin", account, "https://www.linkedin.com/notifications/", "main")
        await page.wait_for_timeout(1500)
        return await page.evaluate(LINKEDIN_NOTIFICATIONS_JS)


async def linkedin_conversations(account: str) -> list[dict[str, Any]]:
    async with lock_of("linkedin", account):
        page = await goto_ready("linkedin", account, "https://www.linkedin.com/messaging/", ".msg-conversations-container, main")
        await page.wait_for_timeout(1500)
        return await page.evaluate(LINKEDIN_CONVERSATIONS_JS)


async def linkedin_read_thread(account: str, thread_id: str, amount: int) -> list[dict[str, Any]]:
    async with lock_of("linkedin", account):
        page = await goto_ready("linkedin", account, f"https://www.linkedin.com/messaging/thread/{thread_id}/", ".msg-s-message-list")
        await page.wait_for_timeout(1500)
        messages = await page.evaluate(LINKEDIN_THREAD_JS)
        for _ in range(30):
            if len(messages) >= amount:
                break
            await page.evaluate("() => { const l = document.querySelector('.msg-s-message-list'); if (l) l.scrollTop = 0; }")
            await page.wait_for_timeout(1000)
            more = await page.evaluate(LINKEDIN_THREAD_JS)
            if len(more) <= len(messages):
                break
            messages = more
    return messages[-amount:]


async def linkedin_send_message(account: str, thread_id: str, text: str) -> None:
    async with lock_of("linkedin", account):
        page = await goto_ready("linkedin", account, f"https://www.linkedin.com/messaging/thread/{thread_id}/", ".msg-form__contenteditable")
        before = len(await page.evaluate(LINKEDIN_THREAD_JS))
        box = page.locator(".msg-form__contenteditable").first
        if not await box.count():
            raise ServiceError("The message box was not found in that conversation.")
        await box.click()
        await page.keyboard.insert_text(text)
        await page.wait_for_timeout(500)
        send = page.locator(".msg-form__send-button, button[type='submit'].msg-form__send-btn").first
        if await send.count():
            await send.click()
        else:
            await page.keyboard.press("Enter")
        for _ in range(40):
            await page.wait_for_timeout(500)
            if len(await page.evaluate(LINKEDIN_THREAD_JS)) > before:
                return
        raise ServiceError("The message was typed but did not appear in the conversation within 20 s.")


async def linkedin_invitations(account: str) -> list[dict[str, Any]]:
    async with lock_of("linkedin", account):
        page = await goto_ready("linkedin", account, "https://www.linkedin.com/mynetwork/invitation-manager/", "main")
        await page.wait_for_timeout(1500)
        return await page.evaluate(LINKEDIN_INVITATIONS_JS)


async def linkedin_answer_invitation(account: str, name: str, accept: bool) -> None:
    async with lock_of("linkedin", account):
        page = await goto_ready("linkedin", account, "https://www.linkedin.com/mynetwork/invitation-manager/", "main")
        await page.wait_for_timeout(1500)
        card = page.locator('[componentkey*="invitation"], li.invitation-card, [class*="invitation-card"], section li').filter(has_text=name).first
        if not await card.count():
            raise ServiceError(f'No pending invitation from "{name}" was found.')
        words = ["Accept", "Принять"] if accept else ["Ignore", "Игнорировать"]
        handle = await card.element_handle()
        clicked = await page.evaluate(
            """([el, words]) => { for (const b of el.querySelectorAll('button, [role="button"]')) {
                 const l = ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).toLowerCase();
                 if (words.some(w => l.includes(w.toLowerCase()))) { b.click(); return true; } } return false; }""",
            [handle, words],
        )
        if not clicked:
            raise ServiceError(f"The {'Accept' if accept else 'Ignore'} button was not found for {name}.")
        await page.wait_for_timeout(1500)


async def _linkedin_collect(page: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    await page.goto("https://www.linkedin.com/messaging/", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector(".msg-conversations-container, main", timeout=15000)
        await page.wait_for_timeout(2000)
        for c in await page.evaluate(LINKEDIN_CONVERSATIONS_JS):
            if c["unread"]:
                items.append({"key": digest("lim", c["id"], c["preview"]), "kind": "message", "who": c["name"],
                              "text": c["preview"], "url": f"https://www.linkedin.com/messaging/thread/{c['id']}/" if c["id"] else ""})
    except Exception as exc:
        log_event("plugin:webservice", "collect_failed", service="linkedin", part="messages", error=str(exc)[:200])
    await page.goto("https://www.linkedin.com/notifications/", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector("main", timeout=15000)
        await page.wait_for_timeout(2000)
        for n in await page.evaluate(LINKEDIN_NOTIFICATIONS_JS):
            items.append({"key": digest("lin", n["text"]), "kind": "notification", "who": "", "text": n["text"], "url": n["url"]})
    except Exception as exc:
        log_event("plugin:webservice", "collect_failed", service="linkedin", part="notifications", error=str(exc)[:200])
    await page.goto("https://www.linkedin.com/mynetwork/invitation-manager/", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector("main", timeout=15000)
        await page.wait_for_timeout(2000)
        for inv in await page.evaluate(LINKEDIN_INVITATIONS_JS):
            items.append({"key": digest("liv", inv["name"], inv["text"]), "kind": "connection invitation", "who": inv["name"],
                          "text": inv["text"], "url": inv["url"]})
    except Exception as exc:
        log_event("plugin:webservice", "collect_failed", service="linkedin", part="invitations", error=str(exc)[:200])
    return items


register(ServiceSpec("facebook", "Facebook", "https://www.facebook.com/", FACEBOOK_STATE_JS, 90.0, _facebook_collect))
register(ServiceSpec("linkedin", "LinkedIn", "https://www.linkedin.com/feed/", LINKEDIN_STATE_JS, 120.0, _linkedin_collect))
