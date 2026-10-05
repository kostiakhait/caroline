"""Linux port (2026-10-04), Phase 3 of docs/LINUX_PORT_PLAN.md: real,
separate, per-label persistent Chromium windows replacing
Caroline.NativeHost.exe/WebView2 entirely on this platform -- no separate
host process at all, launched directly from backend-py via Playwright.

app_browser_cdp.py (snapshot/find/click/type/press_key/evaluate) is
ALREADY fully platform-agnostic: it only ever takes a CDP port number and
calls playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}") --
it doesn't care whether that port belongs to WebView2 (Windows) or a
launched Chromium (here), so it needs NO changes for this port. This
module only replaces the OTHER half of app_browser_plugin.py: window
LIFECYCLE (open/navigate/close/list) and the ops that went straight
through AppBrowserHost's HTTP bridge instead of CDP (screenshot/scroll/
click-by-coords/is_visible_on_top/fill_file_dialog/the "real" OS-level
escalation for click/type/press_key).

Each label's Chromium is launched with `--app=<url>` (removes the
address bar/tabs/toolbar entirely) -- the direct visual analog of
WebView2 being embedded with no browser chrome shown around it at all on
Windows. This also makes the "real OS-level click" escalation's math
exact rather than approximate: with zero chrome, the window's absolute
screen rect IS the page viewport, offset (0, 0) -- no browser-toolbar-
height estimate needed.

A real, separate Chromium process per label is heavier than WebView2's
shared-runtime model, but matches what the plan doc already decided
("the only thing WebView2 added was *embedding* the window inside
Caroline's own app chrome... a real, separate, per-label persistent
Chromium window gives the same... behavior with far less new code").
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page
from playwright.async_api import async_playwright

from app.plugins import _x11_input as x11
from app.plugins import _x11_window as x11win
from app.plugins import app_browser_cdp as cdp
from app.workspace_dir import WORKSPACE_DIR

PROFILES_DIR = Path(WORKSPACE_DIR) / "app_browser_profiles"


class _Window:
    __slots__ = ("label", "context", "page", "port")

    def __init__(self, label: str, context: BrowserContext, page: Page, port: int) -> None:
        self.label = label
        self.context = context
        self.page = page
        self.port = port


_windows: dict[str, _Window] = {}
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


async def _open_window(label: str, url: str | None) -> _Window:
    existing = _windows.get(label)
    if existing is not None and not existing.page.is_closed():
        if url:
            await existing.page.goto(url)
        return existing

    pw = await _get_playwright()
    profile_dir = PROFILES_DIR / label
    profile_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    target_url = url or "about:blank"
    context = await pw.chromium.launch_persistent_context(
        str(profile_dir),
        headless=False,
        # --disable-gpu: confirmed live (WSL2 + WSLg, 2026-10-04) that
        # page.screenshot() fails outright ("Unable to capture
        # screenshot") against this environment's GPU compositing path;
        # forcing software rendering fixed it immediately. Keeping this
        # on for every Linux desktop, not just WSLg, trades a bit of
        # rendering performance for not depending on whatever GPU driver
        # state happens to exist on an arbitrary end-user machine this
        # installer can't control -- the same "design for a stranger's
        # clean machine" posture the rest of this product follows.
        args=[f"--remote-debugging-port={port}", f"--app={target_url}", "--disable-gpu"],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    window = _Window(label, context, page, port)
    _windows[label] = window
    return window


async def open_window(label: str, url: str | None) -> dict[str, Any]:
    async with _open_lock:
        window = await _open_window(label, url)
    return {"label": label, "cdpPort": window.port, "url": window.page.url}


async def navigate(label: str, url: str) -> dict[str, Any]:
    async with _open_lock:
        window = await _open_window(label, url)
    return {"label": label, "url": window.page.url}


async def close_window(label: str) -> dict[str, Any]:
    window = _windows.pop(label, None)
    if window is not None:
        try:
            await window.context.close()
        except Exception:
            pass
    return {"label": label, "closed": window is not None}


async def list_windows() -> list[dict[str, Any]]:
    result = []
    for label, window in list(_windows.items()):
        if window.page.is_closed():
            _windows.pop(label, None)
            continue
        result.append({"label": label, "url": window.page.url})
    return result


async def get_port(label: str) -> int | None:
    window = _windows.get(label)
    return window.port if window is not None else None


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
    await asyncio.to_thread(x11.click_mouse, cx, cy, "left")
    return {"label": label, "clicked_os_level": [cx, cy]}


async def _matching_os_window(window: _Window) -> dict[str, Any] | None:
    title = await window.page.title()
    if not title:
        return None
    found = await asyncio.to_thread(x11win.list_windows, title, None, None, False)
    return found[0] if found else None


async def is_visible_on_top(label: str) -> dict[str, Any]:
    """Simplification, documented rather than silently assumed: this
    checks that the window is mapped/viewable, NOT that it's actually
    the unobscured topmost window at its own position -- a true
    stacking-order occlusion test needs _NET_CLIENT_LIST_STACKING, which
    this WM doesn't set (confirmed live, same gap _x11_window.py's
    module docstring already flags for enumeration), and the manual
    XQueryTree-sibling-order fallback that would replace it wasn't
    built for this first pass. A coordinate-based click guarded by this
    check could still land on a covering window as a result -- a real,
    not-yet-closed fidelity gap vs the Windows version's actual
    occlusion check."""
    window = _windows.get(label)
    if window is None or window.page.is_closed():
        return {"label": label, "visible": False, "reason": "not open"}
    found = await _matching_os_window(window)
    return {"label": label, "visible": found is not None}


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
