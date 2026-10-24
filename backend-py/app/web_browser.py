"""web_browser -- Caroline's own headless Chromium for searching and reading
the web (agentic web search, decided with the user 2026-10-23; see
app/web_research.py for the loop and app/plugins/web_research_plugin.py for
the tools).

A SEPARATE browser of the backend: not Caroline.NativeHost's WebView2, not
the Linux embedded-browser windows (_linux_app_browser.py), not anything of
the user's -- its own headless Chromium, its own empty profile, no window.
The Chromium build is bundled with Caroline (dist/browsers, put there by the
Makefile on Windows and by linux-shell/provision_runtime.sh on Linux), the
headless shell of the revision this Playwright version expects; it is
launched by its explicit path, so nothing in the machine's own Playwright
caches is ever used or needed. A dev tree without dist/browsers falls back to
Playwright's own default lookup.

Searches the user's chosen engines -- Google, Bing, DuckDuckGo, Yandex; the
model picks which -- by opening the ordinary results page and reading it
like a person would. An engine that answers with nothing parseable is
reported as failed, and the caller moves on to another engine.

At most MAX_TABS pages are open at once (the user's number); everything
beyond waits for a free tab. Every search, every results page, every page
opened is logged -- titles and addresses, never page text.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.logging_setup import log_event
from app.workspace_dir import WORKSPACE_DIR

MAX_TABS = 5   # the user's limit (2026-10-23): Chromium tabs open at once, results pages and reading together
# The engines offered to the model. Bing and Yandex are deferred (the user, 2026-10-24): from
# this browser Bing answered with unrelated pages and Yandex with a captcha. Their results
# parsers stay below, unused, for when they are taken up again.
ENGINES = ("google", "duckduckgo")
DEFERRED_ENGINES = ("bing", "yandex")

# The bundled build: <app>/browsers, a sibling of backend-py/ in an install.
_BROWSERS_DIR = Path(os.environ.get("CAROLINE_BROWSERS_DIR") or Path(__file__).resolve().parents[2] / "browsers")
# The browser's own profile, kept between runs -- see _Browser.
PROFILE_DIR = Path(WORKSPACE_DIR) / "web_browser_profile"
_SHELL_NAME = "chrome-headless-shell.exe" if sys.platform == "win32" else "chrome-headless-shell"
_CHROME_NAME = "chrome.exe" if sys.platform == "win32" else "chrome"
# The headless shell (Chromium's old, separate headless build) is told apart from a person's
# browser at once: seen live 2026-10-24 from the user's own machine (a T-Mobile address, not a
# data centre), Google and Yandex answered with a captcha, DuckDuckGo with 403, Bing with
# unrelated pages. The full Chromium runs the "new" headless mode -- the real browser, only
# without a window -- and is used when it is there; the automation flag is taken off as well.
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

# A headless shell says "HeadlessChrome" in its user agent; search engines
# serve such a client a different (or empty) page. The browser presents
# itself as the ordinary desktop Chrome of the same platform.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    if sys.platform == "win32" else
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


class WebBrowserError(Exception):
    """The browser could not be started, or a page could not be opened."""


def _require_time(timeout_s: float) -> None:
    """Playwright reads a timeout of 0 as "wait forever": a caller whose time
    has run out must get an error, never an endless wait."""
    if timeout_s <= 0:
        raise WebBrowserError("no time left for this page")


def bundled_shell_path() -> Path | None:
    """The bundled browser -- the full Chromium when there, else the headless
    shell -- or None (a dev tree, or a build without it)."""
    if not _BROWSERS_DIR.is_dir():
        return None
    for pattern in (f"chromium-*/*/{_CHROME_NAME}", f"chromium_headless_shell-*/*/{_SHELL_NAME}"):
        found = sorted(_BROWSERS_DIR.glob(pattern))
        if found:
            return found[-1]
    return None


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    engine: str
    query: str


@dataclass
class Page:
    url: str              # where the browser ended up (after redirects)
    title: str
    text: str
    links: list[dict[str, str]] = field(default_factory=list)   # [{"text", "url"}] in page order


class _Browser:
    """One headless Chromium for the whole process, started on first use and
    started again if it died. Pages are opened through open(), which holds one
    of MAX_TABS slots for as long as the page is open.

    Its profile is a folder of its own in the workspace (PROFILE_DIR), kept
    between runs (the user, 2026-10-24): a browser that comes back with its
    cookies looks to search engines like a person's, not like a fresh robot.
    Google gets one query at a time (google_turn): two at once got the second
    one a captcha, seen live 2026-10-24 -- everything else stays parallel."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(MAX_TABS)
        self.google_turn = asyncio.Lock()
        self._playwright: Any = None
        self._context: Any = None

    def _closed(self, *_args: Any) -> None:
        log_event("plugin:web", "browser_closed")
        self._context = None

    async def _ensure(self) -> Any:
        async with self._lock:
            if self._context is not None:
                return self._context
            from playwright.async_api import async_playwright   # late: a backend without playwright still starts
            started = time.monotonic()
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            shell = bundled_shell_path()
            options: dict[str, Any] = {
                "headless": True, "args": list(_LAUNCH_ARGS),
                "user_agent": _USER_AGENT, "viewport": {"width": 1366, "height": 900},
            }
            if shell is not None:
                options["executable_path"] = str(shell)
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(str(PROFILE_DIR), **options)
            except Exception as exc:
                log_event("plugin:web", "browser_launch_failed", shell=str(shell) if shell else None, error=str(exc)[:500])
                raise WebBrowserError(
                    f"Caroline's own web browser could not be started ({exc}). "
                    + ("The bundled Chromium is missing from this install." if shell is None else "")
                ) from exc
            self._context.on("close", self._closed)
            log_event("plugin:web", "browser_started", shell=str(shell) if shell else "playwright-default",
                      profile=str(PROFILE_DIR), ms=int((time.monotonic() - started) * 1000))
            return self._context

    async def open(self, url: str, timeout_s: float) -> tuple[Any, Any]:
        """(page, response) for `url`, the page holding a tab slot until it is
        closed by the caller (close())."""
        _require_time(timeout_s)
        await self._slots.acquire()
        try:
            context = await self._ensure()
            page = await context.new_page()
        except BaseException:
            self._slots.release()
            raise
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        except BaseException:
            await self.close(page)
            raise
        return page, response

    async def close(self, page: Any) -> None:
        try:
            await page.close()
        except Exception:  # noqa: BLE001 -- the browser may already be gone
            pass
        finally:
            self._slots.release()

    async def fetch_bytes(self, url: str, timeout_s: float) -> tuple[bytes, str]:
        """A document the browser would download rather than show (a PDF):
        its bytes and content type, fetched with the browser's own session."""
        _require_time(timeout_s)
        context = await self._ensure()
        response = await context.request.get(url, timeout=timeout_s * 1000)
        return await response.body(), response.headers.get("content-type", "")


_browser = _Browser()


# --- results pages -------------------------------------------------------------

def search_url(engine: str, query: str) -> str:
    q = urllib.parse.quote_plus(query)
    return {
        "google": f"https://www.google.com/search?q={q}",
        "bing": f"https://www.bing.com/search?q={q}",
        # The plain-HTML version: the same results, no script needed to render them.
        "duckduckgo": f"https://html.duckduckgo.com/html/?q={q}",
        "yandex": f"https://yandex.ru/search/?text={q}",
    }[engine]


# Run inside the results page: each engine's own result blocks, then -- when
# none matched (the engine changed its page) -- every heading link that leads
# off the engine's own site. Returns [{title, url, snippet}] in page order.
_RESULTS_JS = r"""
(engine) => {
  const out = [];
  // textContent when innerText is empty: Bing renders its result blocks with visibility:hidden
  // (seen live 2026-10-23), which leaves innerText empty though the text is there.
  const text = (el) => (el ? el.innerText || el.textContent || '' : '').replace(/\s+/g, ' ').trim();
  const push = (title, url, snippet) => { if (title && url) out.push({ title, url, snippet }); };
  if (engine === 'google') {
    for (const h3 of document.querySelectorAll('#search a h3, #rso a h3')) {
      const a = h3.closest('a');
      const block = h3.closest('div[data-hveid], div.g, div.MjjYud') || a.parentElement;
      const snippetEl = block && block.querySelector('div[data-sncf], div.VwiC3b, span.aCOpRe');
      push(text(h3), a.href, snippetEl ? text(snippetEl) : text(block).replace(text(h3), '').slice(0, 400));
    }
  } else if (engine === 'bing') {
    for (const li of document.querySelectorAll('li.b_algo')) {
      const a = li.querySelector('h2 a');
      push(text(a), a && a.href, text(li.querySelector('.b_caption p, p.b_lineclamp2, p.b_lineclamp3, p.b_lineclamp4, .b_caption')));
    }
  } else if (engine === 'duckduckgo') {
    for (const r of document.querySelectorAll('.result')) {
      if (r.classList.contains('result--ad')) continue;
      const a = r.querySelector('a.result__a');
      push(text(a), a && a.href, text(r.querySelector('.result__snippet')));
    }
  } else if (engine === 'yandex') {
    for (const li of document.querySelectorAll('li.serp-item')) {
      if (li.querySelector('.label_theme_direct, [data-fast-name="direct"]') || /реклама/i.test(text(li.querySelector('.Label')))) continue;
      const a = li.querySelector('a.OrganicTitle-Link, h2 a, a.Link_theme_outer');
      push(text(li.querySelector('.OrganicTitle-LinkText, h2')) || text(a), a && a.href,
           text(li.querySelector('.OrganicTextContentSpan, .TextContainer, .Organic-ContentWrapper .text-container')));
    }
  }
  if (!out.length) {
    const own = location.hostname.split('.').slice(-2).join('.');
    for (const a of document.querySelectorAll('a[href^="http"]')) {
      const h = a.querySelector('h2, h3');
      if (!h) continue;
      let host = '';
      try { host = new URL(a.href).hostname; } catch (e) { continue; }
      if (host.endsWith(own)) continue;
      push(text(h), a.href, '');
    }
  }
  return out;
}
"""


# True once a results page can be read: its results are there, it has fully
# loaded (so whatever is there is all there is), or it is a captcha page.
_RESULTS_READY_JS = r"""
(engine) => {
  const selector = {google: '#search a h3, #rso a h3', bing: 'li.b_algo', duckduckgo: '.result, .no-results',
                    yandex: 'li.serp-item'}[engine];
  if (selector && document.querySelector(selector)) return true;
  if (/\/sorry\/|showcaptcha/.test(location.href)) return true;
  return document.readyState === 'complete';
}
"""


def _real_url(engine: str, href: str) -> str:
    """The destination of a result link: engines wrap some of them in their
    own redirects (Bing's /ck/a?u=a1<base64>, DuckDuckGo's /l/?uddg=,
    Google's /url?q=)."""
    try:
        parsed = urllib.parse.urlparse(href)
        query = urllib.parse.parse_qs(parsed.query)
        if engine == "bing" and parsed.path.startswith("/ck/a") and query.get("u"):
            encoded = query["u"][0]
            if encoded.startswith("a1"):
                encoded = encoded[2:]
            return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8", "replace")
        if engine == "duckduckgo" and "uddg" in query:
            return query["uddg"][0]
        if engine == "google" and parsed.path == "/url" and query.get("q"):
            return query["q"][0]
    except Exception:  # noqa: BLE001 -- an undecodable link is kept as it is
        pass
    return href


async def _unwrap_google(rows: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
    """Google's result links (2026-10-24) are google.com/goto?url=<encrypted>: the destination
    can't be read off the link, only off the redirect Google answers it with. Each one is asked
    for its redirect (no tab, no page load), all at once; a link that will not say stays as it is
    -- the browser still follows it when the page is read."""
    async def one(row: dict[str, Any]) -> dict[str, Any]:
        href = str(row.get("url") or "")
        if "/goto?" not in href or timeout_s <= 0:
            return row
        try:
            context = await _browser._ensure()
            response = await context.request.get(href, max_redirects=0, timeout=timeout_s * 1000)
            location = response.headers.get("location", "")
            if location.startswith("http"):
                return {**row, "url": location}
        except Exception as exc:  # noqa: BLE001
            log_event("plugin:web", "google_link_unresolved", error=str(exc)[:200])
        return row

    return list(await asyncio.gather(*(one(row) for row in rows)))


async def _results_page(engine: str, query: str, url: str, timeout_s: float) -> tuple[list[dict[str, Any]], Any, str]:
    """The results page's raw rows, its HTTP status and where the browser landed."""
    opened = time.monotonic()
    try:
        page, response = await _browser.open(url, timeout_s)
    except WebBrowserError:
        raise
    except Exception as exc:
        log_event("plugin:web", "search_failed", engine=engine, query=query[:200], error=str(exc)[:300])
        raise WebBrowserError(f"{engine} could not be opened: {exc}") from exc
    try:
        # Google streams its results in after the page itself (an AI overview "Thinking" first):
        # parsing at domcontentloaded found none, seen live 2026-10-24. Wait until results are
        # there, or the page has fully loaded, or it is a captcha page -- within the caller's time.
        remaining_s = timeout_s - (time.monotonic() - opened)
        if remaining_s > 0:
            try:
                await page.wait_for_function(_RESULTS_READY_JS, arg=engine, timeout=remaining_s * 1000)
            except Exception:  # noqa: BLE001 -- parsed as it stands
                pass
        rows = await page.evaluate(_RESULTS_JS, engine)
        remaining_s = timeout_s - (time.monotonic() - opened)
        if not rows and remaining_s > 0 and not re.search(r"/sorry/|showcaptcha", page.url):
            # Results added by scripts after the load: let the page go quiet, then read it again.
            try:
                await page.wait_for_load_state("networkidle", timeout=remaining_s * 1000)
            except Exception:  # noqa: BLE001
                pass
            rows = await page.evaluate(_RESULTS_JS, engine)
            log_event("plugin:web", "results_reparsed", engine=engine, query=query[:200], count=len(rows or []))
        return rows or [], (response.status if response is not None else None), page.url
    finally:
        await _browser.close(page)


async def search(engine: str, query: str, timeout_s: float) -> list[SearchResult]:
    """The organic results of one engine for one query. Raises WebBrowserError
    when the page could not be opened or held no results (the engine refused,
    or changed its page so that nothing could be read)."""
    if engine not in ENGINES:
        deferred = " (deferred for now)" if engine in DEFERRED_ENGINES else ""
        raise WebBrowserError(f'search engine "{engine}" is not available{deferred}; one of {", ".join(ENGINES)}')
    started = time.monotonic()
    url = search_url(engine, query)
    if engine == "google":
        waited = time.monotonic()
        async with _browser.google_turn:   # one Google query at a time, see _Browser
            log_event("plugin:web", "google_turn", query=query[:200], waited_ms=int((time.monotonic() - waited) * 1000))
            rows, status, landed = await _results_page(engine, query, url, timeout_s - (time.monotonic() - started))
    else:
        rows, status, landed = await _results_page(engine, query, url, timeout_s)
    if engine == "google":
        rows = await _unwrap_google(rows or [], timeout_s - (time.monotonic() - started))
    results, seen = [], set()
    for row in rows or []:
        real = _real_url(engine, str(row.get("url") or ""))
        if not real.startswith("http") or real in seen:
            continue
        seen.add(real)
        results.append(SearchResult(title=str(row.get("title") or "").strip(), url=real,
                                    snippet=str(row.get("snippet") or "").strip(), engine=engine, query=query))
    log_event("plugin:web", "search_done", engine=engine, query=query[:200], status=status, landed=landed[:200],
              count=len(results), urls=[r.url for r in results], ms=int((time.monotonic() - started) * 1000))
    if not results:
        raise WebBrowserError(f"{engine} gave no results that could be read (HTTP {status}, page {landed})")
    return results


# --- reading a page ------------------------------------------------------------

# The page's main text: <article>/<main> when one holds most of the text,
# else the body; menus, headers, footers, forms and scripts left out. Plus
# every link, in page order.
_READ_JS = r"""
() => {
  const drop = 'script, style, noscript, template, svg, nav, header, footer, aside, form, iframe, [role="navigation"], [role="banner"], [role="contentinfo"], [aria-hidden="true"]';
  const clean = (root) => { const c = root.cloneNode(true); c.querySelectorAll(drop).forEach((e) => e.remove()); return c; };
  const textOf = (el) => (el.innerText || el.textContent || '').replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n\n').trim();
  const body = document.body ? clean(document.body) : null;
  document.body && document.body.appendChild(body);   // innerText needs a rendered element
  body && (body.style.display = 'block');
  let text = body ? textOf(body) : '';
  for (const sel of ['article', 'main', '[role="main"]']) {
    const el = body && body.querySelector(sel);
    if (el) { const t = textOf(el); if (t.length > text.length / 2) { text = t; break; } }
  }
  body && body.remove();
  const links = [];
  for (const a of document.querySelectorAll('a[href^="http"]')) {
    const t = (a.innerText || '').replace(/\s+/g, ' ').trim();
    if (t) links.push({ text: t.slice(0, 200), url: a.href });
  }
  return { title: document.title || '', text, links };
}
"""


async def read(url: str, timeout_s: float) -> Page:
    """The text of one page as a person would read it (PDFs included)."""
    started = time.monotonic()
    try:
        page, response = await _browser.open(url, timeout_s)
    except WebBrowserError:
        raise
    except Exception as exc:
        message = str(exc)
        if "Download is starting" in message or "net::ERR_ABORTED" in message:
            return await _read_document(url, timeout_s, started)
        log_event("plugin:web", "read_failed", url=url[:300], error=message[:300])
        raise WebBrowserError(f"could not open {url}: {exc}") from exc
    try:
        content_type = (response.headers.get("content-type", "") if response is not None else "")
        status = response.status if response is not None else None
        if "pdf" in content_type.lower():
            await _browser.close(page)
            page = None
            return await _read_document(url, timeout_s, started)
        settle_s = timeout_s - (time.monotonic() - started)
        if settle_s > 0:
            try:   # pages that build their text with scripts: let them settle, within the caller's own time
                await page.wait_for_load_state("networkidle", timeout=settle_s * 1000)
            except Exception:  # noqa: BLE001 -- a page that never goes quiet is read as it stands
                pass
        data = await page.evaluate(_READ_JS)
        landed = page.url
    finally:
        if page is not None:
            await _browser.close(page)
    result = Page(url=landed, title=str(data.get("title") or ""), text=str(data.get("text") or ""), links=list(data.get("links") or []))
    log_event("plugin:web", "read_done", url=url[:300], landed=landed[:300], status=status, title=result.title[:200],
              chars=len(result.text), links=len(result.links), ms=int((time.monotonic() - started) * 1000))
    return result


async def _read_document(url: str, timeout_s: float, started: float) -> Page:
    data, content_type = await _browser.fetch_bytes(url, timeout_s)
    if "pdf" not in content_type.lower() and not data.startswith(b"%PDF"):
        log_event("plugin:web", "read_failed", url=url[:300], error=f"download of {content_type or 'unknown type'}")
        raise WebBrowserError(f"{url} is a download ({content_type or 'unknown type'}), not a page that can be read")
    import fitz   # pymupdf, bundled
    with fitz.open(stream=data, filetype="pdf") as document:
        text = "\n\n".join(p.get_text() for p in document)
        title = (document.metadata or {}).get("title") or ""
    text = re.sub(r"[ \t]+", " ", text).strip()
    log_event("plugin:web", "read_done", url=url[:300], kind="pdf", chars=len(text), ms=int((time.monotonic() - started) * 1000))
    return Page(url=url, title=title, text=text)
