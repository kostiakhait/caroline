"""A single window's own content on X11, covered or not (2026-10-24, explicit
instruction: "можно ли в линуксе получать содержимое отдельных окон без того,
что их перекрывает? Это важно") -- the Linux analog of PrintWindow.

Core X11 keeps no pixels for the covered part of a window, so a screen crop
captures whatever lies on top. The Composite extension does: a window
redirected to off-screen storage (XCompositeRedirectWindow, automatic mode --
the server still draws it on screen as before) has its whole content in a
pixmap (XCompositeNameWindowPixmap), read here with GetImage. Under a
compositing window manager (GNOME, KDE, Xfce with compositing) every window
already is in off-screen storage, and the first capture is complete at once.

Without a compositor this module redirects the window itself, the first time
it is captured, and keeps it redirected. The parts that were covered at that
moment get their pixels only once the application repaints them (the server
sends it Expose for them), so a FIRST capture waits for the content to settle:
a capture every settle_poll_ms, until two in a row are identical, at most
settle_max_ms -- both set by the model per call (settlePollMs/settleMaxMs on
the tools), defaulting to SETTLE_POLL_MS_DEFAULT/SETTLE_MAX_MS_DEFAULT.

Not possible here: a minimized (unmapped) window -- it has no content, the same
as PrintWindow on Windows -- and a server without the Composite extension
(CompositeUnavailable; callers fall back to a screen crop and say so). Wayland
does not let one client read another's windows at all; XWayland applications
work through this same X11 path.

Untested live (the user's instruction, 2026-10-24: "Делать, не проверять").
"""

from __future__ import annotations

import time
from typing import Any

import Xlib.X

from app.logging_setup import log_event
from app.plugins import _x11_window as x11win

_REDIRECT_AUTOMATIC = 0  # Xlib.ext.composite.RedirectAutomatic
# The first-capture wait's defaults (see the module docstring); the model can
# set both per call (2026-10-24, the user: "Цифры сделай параметрами задаваемыми
# моделью. Дефолты оставь свои").
SETTLE_POLL_MS_DEFAULT = 50
SETTLE_MAX_MS_DEFAULT = 1000

_redirected: set[int] = set()


class CompositeUnavailable(RuntimeError):
    """The X server has no Composite extension: only a screen crop is possible."""


def _display() -> Any:
    return x11win._d()


def available() -> bool:
    try:
        return bool(_display().has_extension("Composite"))
    except Exception:  # noqa: BLE001 -- no display at all: not available
        return False


def _to_image(window: Any) -> Any:
    """The window's content (without its border) as an RGB Pillow image."""
    from PIL import Image

    geom = window.get_geometry()
    width, height, border = geom.width, geom.height, geom.border_width
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Window has non-positive size ({width}x{height}).")
    pixmap = window.composite_name_window_pixmap()
    try:
        reply = pixmap.get_image(border, border, width, height, Xlib.X.ZPixmap, 0xFFFFFFFF)
    finally:
        pixmap.free()
    depth = reply.depth
    data = bytes(reply.data)
    if depth not in (24, 32) or len(data) < width * height * 4:
        raise RuntimeError(f"Unsupported window pixel format (depth {depth}, {len(data)} bytes for {width}x{height}).")
    byte_order = _display().display.info.image_byte_order  # 0 = LSBFirst
    raw_mode = "BGRX" if byte_order == 0 else "XRGB"
    return Image.frombuffer("RGB", (width, height), data, "raw", raw_mode, width * 4, 1)


def capture_window(hwnd: str, settle_poll_ms: int | None = None, settle_max_ms: int | None = None) -> Any:
    """The window's whole content, covered parts included, as an RGB Pillow
    image. Raises CompositeUnavailable without the extension, RuntimeError
    for an unmapped (minimized/hidden) window. settle_poll_ms/settle_max_ms:
    the first-capture wait (None = the defaults)."""
    poll_s = (SETTLE_POLL_MS_DEFAULT if settle_poll_ms is None else max(0, int(settle_poll_ms))) / 1000
    max_s = (SETTLE_MAX_MS_DEFAULT if settle_max_ms is None else max(0, int(settle_max_ms))) / 1000
    d = _display()
    if not d.has_extension("Composite"):
        raise CompositeUnavailable("This X server has no Composite extension.")
    window = x11win.window_for(hwnd)
    if window.get_attributes().map_state != Xlib.X.IsViewable:
        raise RuntimeError(f"Window {hwnd} is not mapped (minimized or hidden) -- it has no content to capture.")
    fresh = window.id not in _redirected
    if fresh:
        window.composite_redirect_window(_REDIRECT_AUTOMATIC)
        d.sync()
        _redirected.add(window.id)
        log_event("plugin:x11", "window_redirected_offscreen", hwnd=hwnd)
    image = _to_image(window)
    if not fresh:
        return image
    deadline = time.monotonic() + max_s
    previous = image.tobytes()
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        image = _to_image(window)
        current = image.tobytes()
        if current == previous:
            return image
        previous = current
    log_event("plugin:x11", "window_capture_not_settled", hwnd=hwnd, waited_ms=round(max_s * 1000))
    return image
