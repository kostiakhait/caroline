"""Ports backend/src/appBrowserCdp.ts -- a real CDP client (via Python
Playwright's connect_over_cdp, never a launched local browser) for
AppBrowserWindow's page-content operations (snapshot/find/click/type/
press_key/evaluate). Connects directly to WebView2's own remote-debugging
port (see AppBrowserWindow.xaml.cs's CdpPort) instead of routing through
AppBrowserHost's HTTP bridge + ExecuteScriptAsync -- CDP's Runtime.evaluate
isn't subject to a page's own CSP the way script-tag injection is
(confirmed live in the original TS port: some sites, e.g. ChatGPT and
Facebook, silently blocked ALL ExecuteScriptAsync-injected script).

The tagging/click/type approach mirrors MCP/browser/src/index.ts's own
technique (data-mcp-ref attributes, JS event-dispatch fallback for
React-controlled inputs) -- same idea, reached over a real CDP connection
to an embedded WebView2 instead of a standalone spawned Chromium process.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Download, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

# Passed to page.evaluate() as a JS source string -- Python's Playwright,
# unlike Node's, always takes JS as a string/expression, never a native
# function reference. Ported verbatim from tagVisibleElements().
_TAG_VISIBLE_ELEMENTS_JS = """
() => {
  document.querySelectorAll("[data-mcp-ref]").forEach((el) => el.removeAttribute("data-mcp-ref"));
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none" && +style.opacity !== 0;
  };
  const roleOf = (el) => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "input") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (["button", "submit", "reset"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      return "textbox";
    }
    if (tag === "textarea") return "textbox";
    if (tag === "select") return "combobox";
    if (el.isContentEditable) return "textbox";
    return null;
  };
  const nameOf = (el) => {
    return (
      el.getAttribute("aria-label") ||
      el.getAttribute("placeholder") ||
      el.getAttribute("alt") ||
      el.getAttribute("title") ||
      el.value ||
      el.textContent ||
      ""
    ).trim().replace(/\\s+/g, " ").slice(0, 160);
  };
  const candidates = document.querySelectorAll(
    'a,button,input,textarea,select,[role],[contenteditable="true"],[onclick],summary'
  );
  const results = [];
  let i = 0;
  candidates.forEach((el) => {
    if (!isVisible(el)) return;
    const role = roleOf(el);
    if (!role) return;
    const ref = "e" + ++i;
    el.setAttribute("data-mcp-ref", ref);
    results.push({ ref, role, name: nameOf(el), tag: el.tagName.toLowerCase() });
  });
  return results;
}
"""

_CLICK_JS_DISPATCH = """
(el) => {
  el.scrollIntoView({ block: "center" });
  const r = el.getBoundingClientRect();
  const opts = { bubbles: true, cancelable: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 };
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
    el.dispatchEvent(new MouseEvent(type, opts));
  }
}
"""


def _ref_selector(ref: str) -> str:
    return f'[data-mcp-ref="{ref}"]'


def _resolve_target(page: Page, ref: str | None = None, selector: str | None = None):
    if ref:
        return page.locator(_ref_selector(ref)).first
    if selector:
        return page.locator(selector).first
    raise ValueError("Provide either ref (from app_browser_snapshot/app_browser_find) or selector.")


async def _click_with_fallback(locator: Any) -> str:
    try:
        await locator.click(timeout=6000)
        return "clicked"
    except Exception:
        await locator.evaluate(_CLICK_JS_DISPATCH)
        return "clicked (JS-dispatch fallback; native click() timed out)"


async def _type_with_fallback(page: Page, locator: Any, text: str) -> str:
    try:
        await locator.fill(text, timeout=6000)
        return "typed (fill)"
    except Exception:
        pass
    await locator.evaluate("(el) => el.focus()")
    try:
        await page.keyboard.press("Control+A")
    except Exception:
        pass
    try:
        await page.keyboard.press("Delete")
    except Exception:
        pass
    await page.keyboard.insert_text(text)
    return "typed (focus + keyboard.insertText fallback)"


# --- connection management ---------------------------------------------
# One cached Browser (+ its first Page) per CDP port, reused across calls --
# reconnecting fresh every call would be needlessly slow.

class _Cached:
    __slots__ = ("browser", "page")

    def __init__(self, browser: Browser, page: Page) -> None:
        self.browser = browser
        self.page = page


_cache: dict[int, _Cached] = {}
_playwright_instance: Any = None
_playwright_lock = asyncio.Lock()


async def _get_playwright() -> Any:
    global _playwright_instance
    async with _playwright_lock:
        if _playwright_instance is None:
            _playwright_instance = await async_playwright().start()
        return _playwright_instance


# --- downloads -----------------------------------------------------------
# Confirmed in the original TS port: a CDP connection makes Playwright
# silently intercept every browser download and stage it under a temp
# dir unless something explicitly awaits the "download" event and saves
# it -- attached once, persistently, per page (see _get_page), so every
# trigger (click, navigate-to-a-file-URL, a real OS-level click routed
# through AppBrowserHost.cs) is covered uniformly, not just clicks.
DOWNLOADS_DIR = Path.home() / "Downloads"
_download_save_tasks: dict[Download, "asyncio.Task[str]"] = {}


def _unique_download_path(filename: str) -> Path:
    """Windows Explorer's own "name (1).ext" convention, so a page
    downloaded multiple times doesn't silently overwrite its own earlier
    copy."""
    p = DOWNLOADS_DIR / filename
    stem, suffix = p.stem, p.suffix
    n = 1
    while p.exists():
        p = DOWNLOADS_DIR / f"{stem} ({n}){suffix}"
        n += 1
    return p


async def _save_download(download: Download) -> str:
    target = _unique_download_path(download.suggested_filename or "download")
    await download.save_as(str(target))
    return str(target)


def _on_download(download: Download) -> None:
    _download_save_tasks[download] = asyncio.ensure_future(_save_download(download))


async def _get_page(cdp_port: int) -> Page:
    existing = _cache.get(cdp_port)
    if existing and existing.browser.is_connected() and not existing.page.is_closed():
        return existing.page

    pw = await _get_playwright()
    # Brief retry -- the WebView2 instance's CDP listener may not be up
    # yet the very first moment after EnsureInitializedAsync starts (cold
    # start).
    deadline = time.monotonic() + 15.0
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}", timeout=5000)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = next((p for p in context.pages if not p.is_closed()), None) or await context.new_page()
            page.on("download", _on_download)
            _cache[cdp_port] = _Cached(browser, page)
            return page
        except Exception as exc:
            last_err = exc
            await asyncio.sleep(0.5)
    raise RuntimeError(f"Could not connect to WebView2 CDP port {cdp_port} after 15s: {last_err}")


async def snapshot(cdp_port: int) -> list[dict[str, Any]]:
    page = await _get_page(cdp_port)
    return await page.evaluate(_TAG_VISIBLE_ELEMENTS_JS)


async def find(cdp_port: int, text: str) -> list[dict[str, Any]]:
    tagged = await snapshot(cdp_port)
    needle = text.lower()
    return [t for t in tagged if needle in (t.get("name") or "").lower()]


async def click(cdp_port: int, ref: str | None = None, selector: str | None = None) -> str:
    page = await _get_page(cdp_port)
    locator = _resolve_target(page, ref, selector)

    # Started BEFORE the click (Playwright's standard pattern) so it can't
    # miss a download that fires the instant the click resolves.
    download_wait = asyncio.ensure_future(page.wait_for_event("download", timeout=2000))
    result = await _click_with_fallback(locator)
    download: Download | None = None
    try:
        download = await download_wait
    except (PlaywrightTimeoutError, Exception):
        download = None

    final_result = result
    if download is not None:
        save_task = _download_save_tasks.get(download)
        if save_task is not None:
            try:
                target = await save_task
                final_result = f"{result}; downloaded to {target}"
            except Exception as exc:
                final_result = f"{result}; download started but could not be saved: {exc}"
    return final_result


async def type_text(cdp_port: int, text: str, ref: str | None = None, selector: str | None = None) -> str:
    page = await _get_page(cdp_port)
    locator = _resolve_target(page, ref, selector)
    return await _type_with_fallback(page, locator, text)


async def press_key(cdp_port: int, key: str) -> str:
    page = await _get_page(cdp_port)
    await page.keyboard.press(key)
    return f"pressed {key}"


async def evaluate(cdp_port: int, fn: str, ref: str | None = None) -> Any:
    page = await _get_page(cdp_port)
    if ref:
        result = await page.locator(_ref_selector(ref)).first.evaluate(fn)
    else:
        result = await page.evaluate(fn)
    return result if result is not None else None
