"""Caroline's embedded browser on every platform: one persistent, labeled
window per site ("whatsapp", "facebook", ...), each a separate process of the
bundled Chromium (the build of web_browser.py, dist/browsers), launched through
Playwright with its own profile folder.

It is the single kind of browser Caroline has (owner, 2026-11-24): the Windows
WebView2 windows of Caroline.NativeHost.exe were replaced by this, the way
Linux had it since 2026-10-04. The same label's profile serves two modes:
  * visible -- a window (`--app=<url>`: no address bar or tabs, so the
    window's client area IS the page viewport); the person signs in here once;
  * headless -- the same Chromium without a window, on the SAME profile folder,
    so the sign-in, cookies and the site's storage carry over as they are.
A profile folder can be open in one Chromium process only, so a label is in
one mode at a time; set_mode() closes the one and opens the other on the same
profile and page address.

app_browser_cdp.py (snapshot/find/click/type/press_key/evaluate) takes only a
CDP port number, so it works on any window of this module unchanged.
"""

from __future__ import annotations

import asyncio
import ctypes
import socket
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page
from playwright.async_api import async_playwright

from app.plugins import app_browser_cdp as cdp
from app.web_browser import browser_user_agent, bundled_chromium_path
from app.workspace_dir import WORKSPACE_DIR

if sys.platform == "win32":
    from app.plugins import _win32 as win32
else:
    from app.plugins import _x11_input as x11
    from app.plugins import _x11_window as x11win

PROFILES_DIR = Path(WORKSPACE_DIR) / "app_browser_profiles"


class _Window:
    __slots__ = ("label", "context", "page", "port", "headless")

    def __init__(self, label: str, context: BrowserContext, page: Page, port: int, headless: bool) -> None:
        self.label = label
        self.context = context
        self.page = page
        self.port = port
        self.headless = headless


_windows: dict[str, _Window] = {}
_modes: dict[str, bool] = {}   # label -> headless; a label keeps the mode it was last put in
_open_lock = asyncio.Lock()
_playwright_instance: Any = None
_playwright_lock = asyncio.Lock()


async def _get_playwright() -> Any:
    global _playwright_instance
    async with _playwright_lock:
        if _playwright_instance is None:
            _playwright_instance = await async_playwright().start()
        return _playwright_instance


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _launch(label: str, url: str | None, headless: bool) -> _Window:
    pw = await _get_playwright()
    profile_dir = PROFILES_DIR / label
    profile_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    target_url = url or "about:blank"
    args = [f"--remote-debugging-port={port}", "--disable-blink-features=AutomationControlled"]
    options: dict[str, Any] = {"headless": headless, "user_agent": browser_user_agent()}
    if headless:
        options["viewport"] = {"width": 1366, "height": 900}
    else:
        args.append(f"--app={target_url}")
        options["no_viewport"] = True
    # --disable-gpu: confirmed live (WSL2 + WSLg, 2026-10-04) that page.screenshot() fails against
    # that GPU compositing path; software rendering fixed it. Kept for every Linux desktop so as
    # not to depend on a stranger's GPU driver. Windows keeps the GPU.
    if sys.platform != "win32":
        args.append("--disable-gpu")
    chromium = bundled_chromium_path()
    if chromium is not None:
        options["executable_path"] = str(chromium)   # a dev tree without it uses Playwright's own lookup
    context = await pw.chromium.launch_persistent_context(str(profile_dir), args=args, **options)
    page = context.pages[0] if context.pages else await context.new_page()
    if headless and url:
        await page.goto(url)
    return _Window(label, context, page, port, headless)


async def _close_window_process(window: _Window) -> None:
    """Close the label's Chromium and wait until it lets go of the profile folder. A plain
    context.close() hangs on a window opened with --app (measured 2026-11-25: 30 s and no end),
    so it gets a few seconds, then the browser is asked to quit over CDP (Browser.close)."""
    try:
        await asyncio.wait_for(window.context.close(), 6)
        return
    except Exception:
        pass
    try:
        pw = await _get_playwright()
        remote = await asyncio.wait_for(pw.chromium.connect_over_cdp(f"http://127.0.0.1:{window.port}"), 10)
        session = await remote.new_browser_cdp_session()
        try:
            await asyncio.wait_for(session.send("Browser.close"), 10)
        except Exception:
            pass   # the browser quits before it can answer
    except Exception:
        pass
    await asyncio.sleep(1.0)


async def _open_window(label: str, url: str | None, headless: bool | None = None) -> _Window:
    want = _modes.get(label, False) if headless is None else headless
    existing = _windows.get(label)
    if existing is not None and existing.page.is_closed():
        _windows.pop(label, None)
        existing = None
    if existing is not None and existing.headless == want:
        if url:
            await existing.page.goto(url)
        return existing
    if existing is not None:
        # The other mode: the profile folder is released first, then reopened at the same page.
        url = url or existing.page.url
        _windows.pop(label, None)
        await _close_window_process(existing)
    window = await _launch(label, url, want)
    _windows[label] = window
    _modes[label] = want
    return window


async def open_window(label: str, url: str | None, headless: bool | None = None) -> dict[str, Any]:
    async with _open_lock:
        window = await _open_window(label, url, headless)
    return {"label": label, "cdpPort": window.port, "url": window.page.url, "headless": window.headless}


async def set_mode(label: str, headless: bool) -> dict[str, Any]:
    """Move a label between a visible window and the headless browser, on the same profile."""
    async with _open_lock:
        window = await _open_window(label, None, headless)
    return {"label": label, "cdpPort": window.port, "url": window.page.url, "headless": window.headless}


async def navigate(label: str, url: str) -> dict[str, Any]:
    async with _open_lock:
        window = await _open_window(label, url)
    return {"label": label, "url": window.page.url}


async def close_window(label: str) -> dict[str, Any]:
    window = _windows.pop(label, None)
    _modes.pop(label, None)
    if window is not None:
        await _close_window_process(window)
    return {"label": label, "closed": window is not None}


async def list_windows() -> list[dict[str, Any]]:
    result = []
    for label, window in list(_windows.items()):
        if window.page.is_closed():
            _windows.pop(label, None)
            continue
        result.append({"label": label, "url": window.page.url, "headless": window.headless})
    return result


async def get_port(label: str) -> int | None:
    window = _windows.get(label)
    return window.port if window is not None else None


async def get_cookies(label: str, url: str) -> list[dict[str, Any]]:
    """Added 2026-10-21 (Linux parity pass, messenger-integrations testing
    plan): messenger_channel.py's Facebook login flow polls this to detect
    when the user finishes logging in by hand. On Windows this goes through
    AppBrowserHost.cs's own /get_cookies (WebView2's native CookieManager,
    returning {"key": ..., "value": ...} per cookie) -- Playwright's own
    `BrowserContext.cookies()` is the direct equivalent here, just reshaped
    to that same {"key", "value"} field naming so the caller needs no
    platform branch of its own for the shape, only for which function to call."""
    window = _windows.get(label)
    if window is None:
        return []
    cookies = await window.context.cookies([url])
    return [{"key": c["name"], "value": c["value"]} for c in cookies]


async def screenshot(label: str, x: int | None, y: int | None, width: int | None, height: int | None, max_width: int | None) -> dict[str, Any]:
    """Playwright's own page.screenshot() (a real CDP Page.captureScreenshot
    call) in place of the Windows HTTP bridge's PrintWindow-based capture --
    strictly BETTER fidelity here, since it renders the page's own content
    directly rather than depending on screen occlusion state at all."""
    import base64

    window = await _open_window(label, None)
    clip = None
    if all(v is not None for v in (x, y, width, height)):
        clip = {"x": x, "y": y, "width": width, "height": height}

    # maxWidth without a crop: shrink the viewport itself before capturing,
    # so Chromium renders (and CDP captures) at the smaller size directly --
    # no post-capture image resizing/new dependency needed. Combined with a
    # crop at the same time, honor the crop exactly and skip the downscale
    # instead of rescaling clip coordinates against a resized viewport too
    # (a real, documented simplification, not silently dropped).
    if clip is None and max_width is not None:
        original_viewport = window.page.viewport_size
        if original_viewport and original_viewport["width"] > max_width:
            new_height = max(1, round(original_viewport["height"] * max_width / original_viewport["width"]))
            await window.page.set_viewport_size({"width": max_width, "height": new_height})
            try:
                png_bytes = await window.page.screenshot(type="png")
            finally:
                await window.page.set_viewport_size(original_viewport)
            return {"imageBase64": base64.b64encode(png_bytes).decode("ascii")}

    png_bytes = await window.page.screenshot(clip=clip, type="png")
    return {"imageBase64": base64.b64encode(png_bytes).decode("ascii")}


async def scroll(label: str, ref: str | None, selector: str | None, x: int | None, y: int | None, clicks: int | None) -> dict[str, Any]:
    window = await _open_window(label, None)
    delta_y = -(clicks if clicks is not None else -3) * 100
    if ref or selector:
        locator = cdp._resolve_target(window.page, ref, selector)
        await locator.scroll_into_view_if_needed()
        box = await locator.bounding_box()
        if box:
            await window.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    elif x is not None and y is not None:
        await window.page.mouse.move(x, y)
    await window.page.mouse.wheel(0, delta_y)
    return {"label": label, "scrolled": delta_y}


async def click_coords(label: str, x: int, y: int, button: str | None) -> dict[str, Any]:
    """Raw viewport-coordinate click -- Playwright's page.mouse.click is a
    real CDP Input.dispatchMouseEvent, trusted by the page the same way a
    genuine OS click is (unlike a JS-dispatched synthetic DOM event), so
    this covers the Windows x,y path's "always does a real... click"
    contract without needing the OS-level escalation below."""
    window = await _open_window(label, None)
    await window.page.mouse.click(x, y, button=(button or "left").lower())
    return {"label": label, "clicked": [x, y]}


async def real_os_click(label: str, ref: str | None, selector: str | None) -> dict[str, Any]:
    """OS-level escalation (the Linux analog of the Windows real:true
    path): finds the element's viewport box via CDP, then the OS window
    whose title matches the page's current document.title (via
    _x11_window -- X11 window properties don't expose which Chromium
    instance/CDP port a window belongs to directly, but each window's
    title tracks its page's title, which IS something CDP can read
    precisely), and fires a genuine XTest click at their sum. Works
    exactly (no chrome-height guess) because --app mode leaves zero
    browser chrome, so the OS window's client rect IS the page viewport
    at offset (0, 0). Falls back to the CDP-level click if the window
    can't be matched or its screen position can't be resolved (e.g. the
    WSLg sentinel-geometry race _x11_window.py documents) rather than
    failing outright."""
    window = await _open_window(label, None)
    locator = cdp._resolve_target(window.page, ref, selector)
    await locator.scroll_into_view_if_needed()
    box = await locator.bounding_box()
    if not box:
        return await click_coords(label, 0, 0, None)  # best-effort, shouldn't normally happen
    cx_viewport, cy_viewport = int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)

    target_window = await _matching_os_window(window)
    if target_window is None or target_window["x"] is None:
        return await click_coords(label, cx_viewport, cy_viewport, None)

    cx, cy = target_window["x"] + cx_viewport, target_window["y"] + cy_viewport
    if sys.platform == "win32":
        await asyncio.to_thread(win32.click_mouse, "Left", 1, cx, cy)
    else:
        await asyncio.to_thread(x11.click_mouse, cx, cy, "left")
    return {"label": label, "clicked_os_level": [cx, cy]}


def _win_client_origin(hwnd_hex: str) -> tuple[int, int] | None:
    from ctypes import wintypes
    point = wintypes.POINT(0, 0)
    if not ctypes.windll.user32.ClientToScreen(win32.parse_hwnd(hwnd_hex), ctypes.byref(point)):
        return None
    return point.x, point.y


async def _matching_os_window(window: _Window) -> dict[str, Any] | None:
    """The OS window of this label: the one whose title matches its page's title (a window of
    --app mode carries no browser chrome, so its client area is the page viewport at offset 0,0).
    {"x", "y"} = the client area's screen position, or None when no window matches."""
    if window.headless:
        return None
    title = await window.page.title()
    if not title:
        return None
    if sys.platform == "win32":
        found = await asyncio.to_thread(win32.list_windows, title, "Chrome_WidgetWin", None, False)
        if not found:
            return None
        origin = _win_client_origin(found[0]["hwnd"])
        return {"x": origin[0], "y": origin[1], "hwnd": found[0]["hwnd"]} if origin else None
    found = await asyncio.to_thread(x11win.list_windows, title, None, None, False)
    return found[0] if found else None


async def is_visible_on_top(label: str) -> dict[str, Any]:
    """Visible and not covered at its centre. Windows: the window at the centre point of the
    client area must be this window. Linux: only that the window is mapped (no stacking order
    there; a documented gap)."""
    window = _windows.get(label)
    if window is None or window.page.is_closed():
        return {"label": label, "visible": False, "reason": "not open"}
    if window.headless:
        return {"label": label, "visible": False, "reason": "headless: no window"}
    found = await _matching_os_window(window)
    if found is None:
        return {"label": label, "visible": False}
    if sys.platform == "win32":
        from ctypes import wintypes
        size = await window.page.evaluate("[innerWidth, innerHeight]")
        point = wintypes.POINT(found["x"] + size[0] // 2, found["y"] + size[1] // 2)
        hit = ctypes.windll.user32.WindowFromPoint(point)
        root = ctypes.windll.user32.GetAncestor(hit, 2) if hit else 0   # GA_ROOT
        return {"label": label, "visible": root == win32.parse_hwnd(found["hwnd"])}
    return {"label": label, "visible": True}


async def fill_file_dialog(paths: list[str], _timeout_ms: int | None) -> dict[str, Any]:
    """Playwright's own filechooser event handles this far more directly
    than the Windows path's native-dialog-detection HTTP bridge call
    does: no OS dialog ever needs to appear at all, since Playwright can
    set the file input's files programmatically the instant a page
    triggers one. Attached per-page the moment a window opens would be
    cleaner, but this plugin's call site (app_browser_fill_file_dialog)
    isn't told WHICH label's dialog it's for -- matching the Windows
    tool's own "not tied to a specific labeled window" contract -- so
    this waits for a filechooser on whichever tracked page raises one
    first."""
    pending = [
        asyncio.ensure_future(w.page.wait_for_event("filechooser", timeout=(_timeout_ms or 15000)))
        for w in _windows.values() if not w.page.is_closed()
    ]
    if not pending:
        return {"filled": False, "reason": "no open app-browser windows"}
    done, pending_tasks = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    for t in pending_tasks:
        t.cancel()
    for d in done:
        try:
            chooser = d.result()
        except Exception:
            continue
        await chooser.set_files(paths)
        return {"filled": True, "paths": paths}
    return {"filled": False, "reason": "no filechooser event observed"}


async def download(label: str, ref: str | None, selector: str | None, save_path: str) -> dict[str, Any]:
    """Click an element that starts a download and save the file to save_path."""
    window = await _open_window(label, None)
    locator = cdp._resolve_target(window.page, ref, selector)
    async with window.page.expect_download(timeout=60000) as info:
        await locator.click()
    download_ = await info.value
    target = Path(save_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    await download_.save_as(str(target))
    return {"label": label, "saved": str(target), "suggestedName": download_.suggested_filename}


def list_profiles() -> list[dict[str, Any]]:
    """The labels that have a profile folder (a sign-in kept), open or not."""
    if not PROFILES_DIR.is_dir():
        return []
    return [{"label": d.name, "open": d.name in _windows, "headless": _modes.get(d.name)}
            for d in sorted(PROFILES_DIR.iterdir()) if d.is_dir()]


async def forget_profile(label: str) -> dict[str, Any]:
    """Close the label and delete its profile folder: the sign-in, cookies and site storage go."""
    import re
    import shutil
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", label) or label in (".", ".."):
        return {"label": label, "deleted": False, "reason": "not a valid label"}
    await close_window(label)
    folder = PROFILES_DIR / label
    if not folder.is_dir():
        return {"label": label, "deleted": False, "reason": "no such profile"}
    await asyncio.sleep(0.5)   # the browser process lets go of its files
    shutil.rmtree(folder, ignore_errors=True)
    return {"label": label, "deleted": not folder.exists()}
