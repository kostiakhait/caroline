"""Windows desktop automation in-process, via ctypes (2026-10-24, explicit
instruction: every tool lives in the Python backend, no native utilities --
"Мы давно ушли от exe и node-based MCP"). The direct analog of
_x11_input.py/_x11_window.py on Linux, and a line-by-line port of the C#
helpers it replaces (backend/mcp-servers-src/{mouse,keyboard,window-mouse,
window-keyboard,screenshot,window-screenshot,inspect}/native/Program.cs):
same Win32 calls, same coordinates, same output shapes.

Every call is synchronous (the plugins run it via asyncio.to_thread) and
runs system-DPI-aware for its own thread (_dpi_aware) -- the exes each
called SetProcessDPIAware, without which cursor positions, window rects and
screen pixels come back in a scaled, DPI-virtualized space. Thread-scoped
here rather than process-wide, so nothing else in the backend changes.

Images go through Pillow (already in the bundled runtime): screen and window
pixels are read with GDI (BitBlt / PrintWindow) into a 32-bit DIB.
"""

from __future__ import annotations

import ctypes
import time
from contextlib import contextmanager
from ctypes import wintypes
from typing import Any, Callable, Iterator

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LPARAM = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
ULONG_PTR = ctypes.c_size_t

# ---------------------------------------------------------------- DPI --------

_DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = ctypes.c_void_p(-2)
try:
    _SetThreadDpiAwarenessContext = user32.SetThreadDpiAwarenessContext
    _SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    _SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
except AttributeError:  # older than Windows 10 1607
    _SetThreadDpiAwarenessContext = None


@contextmanager
def _dpi_aware() -> Iterator[None]:
    """What SetProcessDPIAware gave each exe, for the calling thread only."""
    previous = _SetThreadDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_SYSTEM_AWARE) if _SetThreadDpiAwarenessContext else None
    try:
        yield
    finally:
        if previous:
            _SetThreadDpiAwarenessContext(previous)


def dpi_aware(fn: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _dpi_aware():
            return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ---------------------------------------------------------------- structs ----

class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    # All three members, so the union -- and SendInput's cbSize -- is the real
    # Win32 size (the C# helpers had to force Size=32 for the same reason).
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


# ---------------------------------------------------------------- signatures -

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), LPARAM)


def _sig(fn: Any, restype: Any, *argtypes: Any) -> Any:
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_sig(user32.SetCursorPos, wintypes.BOOL, ctypes.c_int, ctypes.c_int)
_sig(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(POINT))
_sig(user32.mouse_event, None, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ULONG_PTR)
_sig(user32.SendInput, wintypes.UINT, wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_sig(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)
_sig(user32.EnumWindows, wintypes.BOOL, WNDENUMPROC, LPARAM)
_sig(user32.EnumChildWindows, wintypes.BOOL, wintypes.HWND, WNDENUMPROC, LPARAM)
_sig(user32.GetWindowRect, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(RECT))
_sig(user32.GetClientRect, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(RECT))
_sig(user32.GetClassNameW, ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_sig(user32.GetWindowTextW, ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_sig(user32.GetWindowTextLengthW, ctypes.c_int, wintypes.HWND)
_sig(user32.IsWindowVisible, wintypes.BOOL, wintypes.HWND)
_sig(user32.IsWindowEnabled, wintypes.BOOL, wintypes.HWND)
_sig(user32.IsWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.GetParent, wintypes.HWND, wintypes.HWND)
_sig(user32.GetWindowThreadProcessId, wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_sig(user32.GetWindowLongPtrW, ctypes.c_ssize_t, wintypes.HWND, ctypes.c_int)
_sig(user32.ClientToScreen, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(POINT))
_sig(user32.PrintWindow, wintypes.BOOL, wintypes.HWND, wintypes.HDC, wintypes.UINT)
_sig(user32.GetDC, wintypes.HDC, wintypes.HWND)
_sig(user32.ReleaseDC, ctypes.c_int, wintypes.HWND, wintypes.HDC)
_sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
_sig(user32.EnumDisplayMonitors, wintypes.BOOL, wintypes.HDC, ctypes.POINTER(RECT), MONITORENUMPROC, LPARAM)
_sig(user32.GetMonitorInfoW, wintypes.BOOL, wintypes.HMONITOR, ctypes.POINTER(MONITORINFO))
_sig(gdi32.CreateCompatibleDC, wintypes.HDC, wintypes.HDC)
_sig(gdi32.CreateCompatibleBitmap, wintypes.HBITMAP, wintypes.HDC, ctypes.c_int, ctypes.c_int)
_sig(gdi32.SelectObject, wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
_sig(gdi32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
_sig(gdi32.DeleteDC, wintypes.BOOL, wintypes.HDC)
_sig(gdi32.BitBlt, wintypes.BOOL, wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD)
_sig(gdi32.GetDIBits, ctypes.c_int, wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
     ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT)
_sig(gdi32.GetPixel, wintypes.COLORREF, wintypes.HDC, ctypes.c_int, ctypes.c_int)
_sig(kernel32.GetProcessId, wintypes.DWORD, wintypes.HANDLE)
_sig(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)

# ---------------------------------------------------------------- constants --

MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL = 0x0800
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
MK_LBUTTON, MK_RBUTTON, MK_MBUTTON = 0x0001, 0x0002, 0x0010
PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
GWLP_ID = -12
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79

# ---------------------------------------------------------------- keys -------
# Key name -> Windows virtual-key code. Moved here, unchanged, from the old
# native_exe.py (originally mcp-servers-src/keyboard/src/keys.ts); shared by
# keyboard, window-keyboard and chain.

_NAMED_KEYS: dict[str, int] = {
    "enter": 0x0D, "return": 0x0D, "escape": 0x1B, "esc": 0x1B, "tab": 0x09,
    "backspace": 0x08, "space": 0x20, "spacebar": 0x20, "capslock": 0x14,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "printscreen": 0x2C, "scrolllock": 0x91, "pause": 0x13, "numlock": 0x90,
    "ctrl": 0x11, "control": 0x11, "lctrl": 0xA2, "rctrl": 0xA3,
    "shift": 0x10, "lshift": 0xA0, "rshift": 0xA1,
    "alt": 0x12, "menu": 0x12, "lalt": 0xA4, "ralt": 0xA5,
    "win": 0x5B, "windows": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62, "numpad3": 0x63, "numpad4": 0x64,
    "numpad5": 0x65, "numpad6": 0x66, "numpad7": 0x67, "numpad8": 0x68, "numpad9": 0x69,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "semicolon": 0xBA, "equals": 0xBB, "comma": 0xBC, "minus": 0xBD,
    "period": 0xBE, "slash": 0xBF, "backtick": 0xC0, "grave": 0xC0,
    "openbracket": 0xDB, "backslash": 0xDC, "closebracket": 0xDD, "quote": 0xDE,
}
for _i in range(1, 25):
    _NAMED_KEYS[f"f{_i}"] = 0x6F + _i


def resolve_vk(key: str) -> int:
    normalized = key.strip().lower()
    if normalized in _NAMED_KEYS:
        return _NAMED_KEYS[normalized]
    if len(normalized) == 1:
        if "a" <= normalized <= "z":
            return ord(normalized.upper())
        if "0" <= normalized <= "9":
            return ord(normalized)
    raise ValueError(f'Unknown key name: "{key}"')


# ---------------------------------------------------------------- handles ----

def parse_hwnd(text: str) -> int:
    """A "0x..." hex handle (as window_list returns it) or bare hex."""
    s = str(text).strip()
    return int(s[2:] if s.lower().startswith("0x") else s, 16)


def hwnd_to_hex(hwnd: int) -> str:
    return "0x" + format(hwnd, "08X")


def _lparam(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


# ---------------------------------------------------------------- mouse ------

def _global_button_flags(button: str) -> tuple[int, int]:
    flags = {"Left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
             "Right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
             "Middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)}.get(button)
    if flags is None:
        raise ValueError(f"Unknown button: {button}")
    return flags


def _posted_button_messages(button: str) -> tuple[int, int, int]:
    messages = {"Left": (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
                "Right": (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON),
                "Middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON)}.get(button)
    if messages is None:
        raise ValueError(f"Unknown button: {button}")
    return messages


def _cursor() -> dict[str, int]:
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return {"x": pt.x, "y": pt.y}


def _set_cursor(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))


def _mouse_event(flags: int, data: int = 0) -> None:
    user32.mouse_event(flags, 0, 0, data & 0xFFFFFFFF, 0)


@dpi_aware
def get_mouse_position() -> dict[str, int]:
    return _cursor()


@dpi_aware
def move_mouse(x: int, y: int) -> dict[str, int]:
    _set_cursor(x, y)
    return _cursor()


@dpi_aware
def click_mouse(button: str, clicks: int, x: int | None = None, y: int | None = None) -> dict[str, int]:
    if x is not None and y is not None:
        _set_cursor(x, y)
    down, up = _global_button_flags(button)
    for i in range(clicks):
        _mouse_event(down)
        _mouse_event(up)
        if i < clicks - 1:
            time.sleep(0.05)
    return _cursor()


@dpi_aware
def mouse_button(action: str, button: str, x: int | None = None, y: int | None = None) -> dict[str, int]:
    if x is not None and y is not None:
        _set_cursor(x, y)
    down, up = _global_button_flags(button)
    _mouse_event(down if action == "down" else up)
    return _cursor()


@dpi_aware
def scroll_mouse(delta: int) -> dict[str, int]:
    _mouse_event(MOUSEEVENTF_WHEEL, int(delta) * 120)
    return _cursor()


def post_click(hwnd: int, x: int, y: int, button: str = "Left") -> None:
    """Posted to the window's queue: no cursor move, no focus change."""
    down, up, wparam_down = _posted_button_messages(button)
    lparam = _lparam(x, y)
    user32.PostMessageW(hwnd, down, wparam_down, lparam)
    time.sleep(0.02)
    user32.PostMessageW(hwnd, up, 0, lparam)


# ---------------------------------------------------------------- keyboard ---

def _send(inputs: list[INPUT]) -> None:
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput inserted {sent}/{len(inputs)} events, GetLastError={ctypes.get_last_error()}")


def _key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    item = INPUT(type=INPUT_KEYBOARD)
    item.u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return item


def _utf16_units(text: str) -> list[int]:
    """UTF-16 code units, as the C# helpers iterated a string (a character
    outside the BMP goes as its two surrogates)."""
    data = text.encode("utf-16-le")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


def type_text(text: str, delay_ms: int = 10) -> None:
    for unit in _utf16_units(text):
        _send([_key_input(scan=unit, flags=KEYEVENTF_UNICODE), _key_input(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)


def press_combo(modifier_vks: list[int], vk: int) -> None:
    inputs = [_key_input(vk=m) for m in modifier_vks]
    inputs += [_key_input(vk=vk), _key_input(vk=vk, flags=KEYEVENTF_KEYUP)]
    inputs += [_key_input(vk=m, flags=KEYEVENTF_KEYUP) for m in reversed(modifier_vks)]
    _send(inputs)


def key_down(vk: int) -> None:
    _send([_key_input(vk=vk)])


def key_up(vk: int) -> None:
    _send([_key_input(vk=vk, flags=KEYEVENTF_KEYUP)])


def post_text(hwnd: int, text: str, delay_ms: int = 10) -> None:
    for unit in _utf16_units(text):
        user32.PostMessageW(hwnd, WM_CHAR, unit, 0)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)


def post_combo(hwnd: int, modifier_vks: list[int], vk: int) -> None:
    for m in modifier_vks:
        user32.PostMessageW(hwnd, WM_KEYDOWN, m, 0)
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, 0)
    for m in reversed(modifier_vks):
        user32.PostMessageW(hwnd, WM_KEYUP, m, 0)


# ---------------------------------------------------------------- windows ----

def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _rect_dto(r: RECT) -> dict[str, int]:
    return {"x": r.left, "y": r.top, "width": r.right - r.left, "height": r.bottom - r.top}


def _process_name(pid: int) -> str | None:
    try:
        import psutil
        name = psutil.Process(pid).name()
    except Exception:  # noqa: BLE001 -- exited or inaccessible: null, as inspect.exe did
        return None
    return name[:-4] if name.lower().endswith(".exe") else name


def describe(hwnd: int) -> dict[str, Any]:
    """The same record inspect.exe returned (camelCase fields)."""
    rect, client = RECT(), RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    user32.GetClientRect(hwnd, ctypes.byref(client))
    pid = window_pid(hwnd)
    parent = user32.GetParent(hwnd)
    return {
        "hwnd": hwnd_to_hex(hwnd), "title": _window_text(hwnd), "className": _class_name(hwnd),
        "pid": pid, "processName": _process_name(pid), "rect": _rect_dto(rect), "clientRect": _rect_dto(client),
        "visible": bool(user32.IsWindowVisible(hwnd)), "enabled": bool(user32.IsWindowEnabled(hwnd)),
        "controlId": int(user32.GetWindowLongPtrW(hwnd, GWLP_ID)),
        "parentHwnd": hwnd_to_hex(parent) if parent else None,
    }


def _enum(parent: int | None) -> list[int]:
    handles: list[int] = []

    @WNDENUMPROC
    def callback(hwnd: int, _lp: int) -> bool:
        handles.append(hwnd)
        return True

    if parent is None:
        user32.EnumWindows(callback, 0)
    else:
        user32.EnumChildWindows(parent, callback, 0)
    return handles


def _matches(w: dict[str, Any], title: str | None, class_name: str | None, pid: int | None, include_invisible: bool) -> bool:
    if not include_invisible and not w["visible"]:
        return False
    if title is not None and title.lower() not in w["title"].lower():
        return False
    if class_name is not None and class_name.lower() not in w["className"].lower():
        return False
    if pid is not None and w["pid"] != int(pid):
        return False
    return True


@dpi_aware
def list_windows(title: str | None = None, class_name: str | None = None, pid: int | None = None,
                 include_invisible: bool = False) -> list[dict[str, Any]]:
    records = (describe(h) for h in _enum(None))
    return [w for w in records if _matches(w, title, class_name, pid, include_invisible)]


@dpi_aware
def window_children(hwnd: int, title: str | None = None, class_name: str | None = None, pid: int | None = None,
                    include_invisible: bool = False) -> list[dict[str, Any]]:
    records = (describe(h) for h in _enum(hwnd))
    return [w for w in records if _matches(w, title, class_name, pid, include_invisible)]


@dpi_aware
def window_info(hwnd: int) -> dict[str, Any]:
    return describe(hwnd)


def find_window(title: str | None, class_name: str | None, pid: int | None) -> int | None:
    """The first visible top-level window matching all given filters."""
    for hwnd in _enum(None):
        if not user32.IsWindowVisible(hwnd):
            continue
        if title is not None and title.lower() not in _window_text(hwnd).lower():
            continue
        if class_name is not None and class_name.lower() not in _class_name(hwnd).lower():
            continue
        if pid is not None and window_pid(hwnd) != int(pid):
            continue
        return hwnd
    return None


def client_to_screen(hwnd: int, x: int, y: int) -> tuple[int, int]:
    pt = POINT(int(x), int(y))
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


# ---------------------------------------------------------------- capture ----

def _dc_to_image(src_dc: int, width: int, height: int, draw: Callable[[int], None]) -> Any:
    """Runs `draw(memory_dc)` onto a fresh width x height bitmap and returns it
    as an RGB Pillow image."""
    from PIL import Image

    mem_dc = gdi32.CreateCompatibleDC(src_dc)
    bitmap = gdi32.CreateCompatibleBitmap(src_dc, width, height)
    old = gdi32.SelectObject(mem_dc, bitmap)
    try:
        draw(mem_dc)
        header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=width, biHeight=-height,
                                  biPlanes=1, biBitCount=32, biCompression=0)
        buffer = ctypes.create_string_buffer(width * height * 4)
        gdi32.SelectObject(mem_dc, old)
        if gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0) == 0:
            raise OSError(f"GetDIBits failed, GetLastError={ctypes.get_last_error()}")
        return Image.frombuffer("RGB", (width, height), buffer.raw, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)


def _capture_screen_region(x: int, y: int, width: int, height: int) -> Any:
    screen_dc = user32.GetDC(0)
    try:
        return _dc_to_image(screen_dc, width, height,
                            lambda mem: gdi32.BitBlt(mem, 0, 0, width, height, screen_dc, x, y, SRCCOPY))
    finally:
        user32.ReleaseDC(0, screen_dc)


def _monitors() -> list[dict[str, int]]:
    """Monitor rects in EnumDisplayMonitors order (what Screen.AllScreens used)."""
    found: list[dict[str, int]] = []

    @MONITORENUMPROC
    def callback(hmon: int, _hdc: int, _rect: Any, _lp: int) -> bool:
        info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        found.append(_rect_dto(info.rcMonitor))
        return True

    user32.EnumDisplayMonitors(0, None, callback, 0)
    return found


def _virtual_screen() -> dict[str, int]:
    return {"x": user32.GetSystemMetrics(SM_XVIRTUALSCREEN), "y": user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            "width": user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), "height": user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)}


def _capture_window(hwnd: int) -> Any:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError(f"GetWindowRect failed for handle {hwnd_to_hex(hwnd)} - is it a valid window?")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise OSError(f"Window has non-positive size ({width}x{height}) - it may be minimized.")
    screen_dc = user32.GetDC(0)
    try:
        # PrintWindow renders the window's own content, so a covered or
        # background window is captured as it is, not what lies on top of it.
        return _dc_to_image(screen_dc, width, height, lambda mem: user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT))
    finally:
        user32.ReleaseDC(0, screen_dc)


from app.plugins._image_util import crop_and_downscale as _crop_and_downscale, to_png  # noqa: E402,F401 -- shared with Linux


@dpi_aware
def screenshot(monitor: int | None = None, crop: tuple[int, int, int, int] | None = None,
               max_width: int | None = None) -> Any:
    """The whole virtual screen, or one monitor by zero-based index -- then the
    optional crop (relative to that capture) and downscale, as capture.exe."""
    if monitor is not None:
        monitors = _monitors()
        if monitor < 0 or monitor >= len(monitors):
            raise ValueError(f"Monitor index {monitor} out of range (0..{len(monitors) - 1})")
        bounds = monitors[monitor]
    else:
        bounds = _virtual_screen()
    image = _capture_screen_region(bounds["x"], bounds["y"], bounds["width"], bounds["height"])
    return _crop_and_downscale(image, crop, max_width)


@dpi_aware
def capture_window(hwnd: int, crop: tuple[int, int, int, int] | None = None, max_width: int | None = None) -> Any:
    return _crop_and_downscale(_capture_window(hwnd), crop, max_width)


@dpi_aware
def capture_window_raw(hwnd: int) -> Any:
    return _capture_window(hwnd)


@dpi_aware
def capture_screen_region(x: int, y: int, width: int, height: int) -> Any:
    return _capture_screen_region(x, y, width, height)


@dpi_aware
def capture_full_screen() -> Any:
    v = _virtual_screen()
    return _capture_screen_region(v["x"], v["y"], v["width"], v["height"])


def screen_pixel(x: int, y: int) -> tuple[int, int, int]:
    screen_dc = user32.GetDC(0)
    try:
        raw = gdi32.GetPixel(screen_dc, int(x), int(y))
    finally:
        user32.ReleaseDC(0, screen_dc)
    return raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF
