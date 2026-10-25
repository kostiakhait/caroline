"""app/plugins/_x11_capture.py with a fake Xlib and fake windows (no X server):
pixel conversion, redirect-once, the first-capture settle wait, and the
no-Composite / unmapped-window errors."""

import sys
import types

import pytest


@pytest.fixture
def cap(monkeypatch):
    xlib = types.ModuleType("Xlib")
    x = types.ModuleType("Xlib.X")
    x.ZPixmap, x.IsViewable = 2, 2
    xlib.X = x
    for name in ("Xlib.Xatom", "Xlib.display"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "Xlib", xlib)
    monkeypatch.setitem(sys.modules, "Xlib.X", x)
    sys.modules.pop("app.plugins._x11_window", None)
    sys.modules.pop("app.plugins._x11_capture", None)
    import app.plugins._x11_capture as module
    monkeypatch.setattr(module, "SETTLE_POLL_MS_DEFAULT", 0)
    yield module
    sys.modules.pop("app.plugins._x11_window", None)
    sys.modules.pop("app.plugins._x11_capture", None)


class FakeDisplay:
    def __init__(self, composite=True):
        self.composite = composite
        self.display = types.SimpleNamespace(info=types.SimpleNamespace(image_byte_order=0))
        self.synced = 0

    def has_extension(self, name):
        return self.composite and name == "Composite"

    def sync(self):
        self.synced += 1


class FakeWindow:
    """2x1 window; frames[i] is the BGRX content of the i-th capture."""

    def __init__(self, frames, mapped=True):
        self.id, self.frames, self.captures, self.redirects = 0x400001, frames, 0, 0
        self.mapped = mapped

    def get_attributes(self):
        return types.SimpleNamespace(map_state=2 if self.mapped else 0)

    def get_geometry(self):
        return types.SimpleNamespace(width=2, height=1, border_width=0)

    def composite_redirect_window(self, update):
        self.redirects += 1

    def composite_name_window_pixmap(self):
        window = self

        class Pixmap:
            def get_image(self, x, y, w, h, fmt, mask):
                frame = window.frames[min(window.captures, len(window.frames) - 1)]
                window.captures += 1
                return types.SimpleNamespace(depth=24, data=frame)

            def free(self):
                pass
        return Pixmap()


def _wire(cap, monkeypatch, display, window):
    monkeypatch.setattr(cap.x11win, "_d", lambda: display)
    monkeypatch.setattr(cap.x11win, "window_for", lambda hwnd: window)
    cap._redirected.clear()


RED_BLUE = bytes([0, 0, 255, 0, 255, 0, 0, 0])  # BGRX: red, blue
GREEN = bytes([0, 255, 0, 0, 0, 255, 0, 0])


def test_pixels_come_out_as_rgb(cap, monkeypatch):
    window = FakeWindow([RED_BLUE])
    _wire(cap, monkeypatch, FakeDisplay(), window)
    image = cap.capture_window("0x400001")
    assert image.size == (2, 1)
    assert image.getpixel((0, 0)) == (255, 0, 0) and image.getpixel((1, 0)) == (0, 0, 255)


def test_redirected_once_and_the_first_capture_waits_until_it_settles(cap, monkeypatch):
    window = FakeWindow([GREEN, RED_BLUE, RED_BLUE])   # the app repaints after the redirect
    _wire(cap, monkeypatch, FakeDisplay(), window)
    assert cap.capture_window("0x400001").getpixel((0, 0)) == (255, 0, 0)
    assert window.redirects == 1
    before = window.captures
    cap.capture_window("0x400001")
    assert window.redirects == 1 and window.captures == before + 1   # later captures: one read, no wait


def test_no_composite_extension(cap, monkeypatch):
    _wire(cap, monkeypatch, FakeDisplay(composite=False), FakeWindow([RED_BLUE]))
    with pytest.raises(cap.CompositeUnavailable):
        cap.capture_window("0x400001")
    assert cap.available() is False


def test_an_unmapped_window_has_nothing_to_capture(cap, monkeypatch):
    _wire(cap, monkeypatch, FakeDisplay(), FakeWindow([RED_BLUE], mapped=False))
    with pytest.raises(RuntimeError, match="not mapped"):
        cap.capture_window("0x400001")


def test_the_model_sets_the_settle_wait(cap, monkeypatch):
    window = FakeWindow([GREEN, RED_BLUE, RED_BLUE])
    _wire(cap, monkeypatch, FakeDisplay(), window)
    image = cap.capture_window("0x400001", settle_poll_ms=0, settle_max_ms=0)   # 0: no wait
    assert image.getpixel((0, 0)) == (0, 255, 0) and window.captures == 1
