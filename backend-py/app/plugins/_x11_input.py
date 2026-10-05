"""Linux port (2026-10-04): XTest-based mouse/keyboard primitives, the
direct analog of native_exe.py's Windows SendInput-backed mouse.exe/
keyboard.exe -- in-process via python-xlib instead of a spawned native
exe, per the Linux Port Plan's own stated approach ("skip the subprocess
layer entirely: a small app/plugins/_x11_input.py... using python-xlib
directly, in-process").

A single shared Xlib.display.Display connection is kept open for the
process's lifetime (opening one per call is wasteful and python-xlib
connections are not meant to be short-lived); callers must run on a
thread/loop where a DISPLAY is actually reachable (same constraint
native Windows automation already has against the interactive desktop
session).

Tested live against a real X server (WSL2 + WSLg, 2026-10-04): XTEST
extension present, pointer warp + button press/release delivered and
read back correctly, keysym-based key press/release delivered. One
WSLg-specific quirk found and NOT present on a real X.Org desktop: its
window manager reports a sentinel (-32768, -32768) geometry for
reparented top-level frame windows, which breaks naive
"sum parent geometry up to root" absolute-position math for a specific
window's screen rect -- _x11_window.py's get_window_rect degrades
gracefully (returns None rect fields) rather than returning that
garbage when it detects the sentinel, but still works fine for
everything that doesn't depend on window-relative screen coordinates
(pointer move/click/scroll by absolute coordinate, all keyboard
primitives, window enumeration/info by name/class/pid).
"""

from __future__ import annotations

from typing import Any

import Xlib.X
import Xlib.XK
import Xlib.display
import Xlib.ext.xtest
import Xlib.protocol.event

_display: Xlib.display.Display | None = None


def _d() -> Xlib.display.Display:
    global _display
    if _display is None:
        _display = Xlib.display.Display()
    return _display


def get_mouse_position() -> dict[str, int]:
    root = _d().screen().root
    pointer = root.query_pointer()
    return {"x": pointer.root_x, "y": pointer.root_y}


def move_mouse(x: int, y: int) -> None:
    d = _d()
    Xlib.ext.xtest.fake_input(d, Xlib.X.MotionNotify, x=int(x), y=int(y))
    d.sync()


# X11 button numbers (XTest ButtonPress/Release take these directly, not a
# name) -- 1/2/3 are the standard left/middle/right, 4/5 vertical scroll,
# 6/7 horizontal scroll (same convention xdotool/libinput use).
_BUTTON_NUMBERS = {"left": 1, "middle": 2, "right": 3}


def _resolve_button(name: str | None) -> int:
    return _BUTTON_NUMBERS.get((name or "left").strip().lower(), 1)


def click_mouse(x: int, y: int, button: str | None = None) -> None:
    d = _d()
    move_mouse(x, y)
    btn = _resolve_button(button)
    Xlib.ext.xtest.fake_input(d, Xlib.X.ButtonPress, btn)
    Xlib.ext.xtest.fake_input(d, Xlib.X.ButtonRelease, btn)
    d.sync()


def mouse_button(action: str, button: str | None = None) -> None:
    """action: "down" or "up" -- the held-button analog of click_mouse, for
    drag sequences (mirrors mouse_plugin.py's Windows mouse_button tool)."""
    d = _d()
    btn = _resolve_button(button)
    event = Xlib.X.ButtonPress if action == "down" else Xlib.X.ButtonRelease
    Xlib.ext.xtest.fake_input(d, event, btn)
    d.sync()


def scroll_mouse(amount: int, horizontal: bool = False) -> None:
    """Positive amount scrolls up/right, negative scrolls down/left --
    matches mouse_plugin.py's existing Windows wheel-delta sign convention.
    One XTest button click per notch (no continuous-delta wheel event in
    core X11); amount is the notch count."""
    d = _d()
    if horizontal:
        btn = 7 if amount > 0 else 6
    else:
        btn = 4 if amount > 0 else 5
    for _ in range(abs(int(amount))):
        Xlib.ext.xtest.fake_input(d, Xlib.X.ButtonPress, btn)
        Xlib.ext.xtest.fake_input(d, Xlib.X.ButtonRelease, btn)
    d.sync()


# --- key-name -> X11 keysym table -------------------------------------------
# Mirrors native_exe.py's _NAMED_KEYS table key-for-key (same names Caroline
# already uses in tool calls) so chain/keyboard/window-keyboard steps work
# unmodified across both platforms -- only the resolved value's TYPE differs
# (an X11 keysym here, a Win32 virtual-key code there); each plugin's Linux
# branch calls resolve_keysym instead of native_exe.resolve_vk.
_NAMED_KEYS: dict[str, int] = {
    "enter": Xlib.XK.XK_Return, "return": Xlib.XK.XK_Return,
    "escape": Xlib.XK.XK_Escape, "esc": Xlib.XK.XK_Escape, "tab": Xlib.XK.XK_Tab,
    "backspace": Xlib.XK.XK_BackSpace, "space": Xlib.XK.XK_space, "spacebar": Xlib.XK.XK_space,
    "capslock": Xlib.XK.XK_Caps_Lock,
    "left": Xlib.XK.XK_Left, "up": Xlib.XK.XK_Up, "right": Xlib.XK.XK_Right, "down": Xlib.XK.XK_Down,
    "home": Xlib.XK.XK_Home, "end": Xlib.XK.XK_End, "pageup": Xlib.XK.XK_Page_Up, "pagedown": Xlib.XK.XK_Page_Down,
    "insert": Xlib.XK.XK_Insert, "delete": Xlib.XK.XK_Delete, "del": Xlib.XK.XK_Delete,
    "printscreen": Xlib.XK.XK_Print, "scrolllock": Xlib.XK.XK_Scroll_Lock,
    "pause": Xlib.XK.XK_Pause, "numlock": Xlib.XK.XK_Num_Lock,
    "ctrl": Xlib.XK.XK_Control_L, "control": Xlib.XK.XK_Control_L,
    "lctrl": Xlib.XK.XK_Control_L, "rctrl": Xlib.XK.XK_Control_R,
    "shift": Xlib.XK.XK_Shift_L, "lshift": Xlib.XK.XK_Shift_L, "rshift": Xlib.XK.XK_Shift_R,
    "alt": Xlib.XK.XK_Alt_L, "menu": Xlib.XK.XK_Menu, "lalt": Xlib.XK.XK_Alt_L, "ralt": Xlib.XK.XK_Alt_R,
    "win": Xlib.XK.XK_Super_L, "windows": Xlib.XK.XK_Super_L,
    "lwin": Xlib.XK.XK_Super_L, "rwin": Xlib.XK.XK_Super_R,
    "numpad0": Xlib.XK.XK_KP_0, "numpad1": Xlib.XK.XK_KP_1, "numpad2": Xlib.XK.XK_KP_2,
    "numpad3": Xlib.XK.XK_KP_3, "numpad4": Xlib.XK.XK_KP_4, "numpad5": Xlib.XK.XK_KP_5,
    "numpad6": Xlib.XK.XK_KP_6, "numpad7": Xlib.XK.XK_KP_7, "numpad8": Xlib.XK.XK_KP_8, "numpad9": Xlib.XK.XK_KP_9,
    "multiply": Xlib.XK.XK_KP_Multiply, "add": Xlib.XK.XK_KP_Add, "subtract": Xlib.XK.XK_KP_Subtract,
    "decimal": Xlib.XK.XK_KP_Decimal, "divide": Xlib.XK.XK_KP_Divide,
    "semicolon": Xlib.XK.XK_semicolon, "equals": Xlib.XK.XK_equal, "comma": Xlib.XK.XK_comma,
    "minus": Xlib.XK.XK_minus, "period": Xlib.XK.XK_period, "slash": Xlib.XK.XK_slash,
    "backtick": Xlib.XK.XK_grave, "grave": Xlib.XK.XK_grave,
    "openbracket": Xlib.XK.XK_bracketleft, "backslash": Xlib.XK.XK_backslash,
    "closebracket": Xlib.XK.XK_bracketright, "quote": Xlib.XK.XK_apostrophe,
}
for _i in range(1, 25):
    _NAMED_KEYS[f"f{_i}"] = getattr(Xlib.XK, f"XK_F{_i}")


def resolve_keysym(key: str) -> int:
    normalized = key.strip().lower()
    if normalized in _NAMED_KEYS:
        return _NAMED_KEYS[normalized]
    if len(normalized) == 1:
        ks = Xlib.XK.string_to_keysym(normalized)
        if ks != 0:
            return ks
    raise ValueError(f'Unknown key name: "{key}"')


# A keysym with no keycode in the CURRENT keyboard layout (confirmed live,
# 2026-10-04: this test machine's X server layout is Russian -- Latin
# letter keysyms like XK_a have no keycode at all, only the Cyrillic ones
# at the same physical keys do) can't be delivered via XTest at all, since
# FakeInput KeyPress only takes a keycode, never a keysym directly. The fix
# every real X11 automation tool uses (xdotool included): temporarily bind
# one unused keycode to whatever keysym is needed via
# ChangeKeyboardMapping, then synthesize that keycode -- same effect as a
# layout switch, scoped to a single spare key. _SCRATCH_KEYCODE is resolved
# once (first keycode in the map with no keysym bound at all) and reused/
# rebound on demand rather than restored after each use, since nothing
# else on the system depends on that keycode being empty.
_scratch_keycode: int | None = None
_scratch_bound_keysym: int | None = None


def _find_free_keycode(d: Xlib.display.Display) -> int:
    min_kc, max_kc = d.display.info.min_keycode, d.display.info.max_keycode
    mapping = d.get_keyboard_mapping(min_kc, max_kc - min_kc + 1)
    for i, syms in enumerate(mapping):
        if all(s == 0 for s in syms):
            return min_kc + i
    raise RuntimeError("No free X11 keycode available to remap for synthetic input")


def _keysym_to_keycode(keysym: int) -> int:
    global _scratch_keycode, _scratch_bound_keysym
    d = _d()
    keycode = d.keysym_to_keycode(keysym)
    if keycode != 0:
        return keycode
    if _scratch_keycode is None:
        _scratch_keycode = _find_free_keycode(d)
    if _scratch_bound_keysym != keysym:
        d.change_keyboard_mapping(_scratch_keycode, [(keysym, 0, keysym, 0)])
        d.sync()
        _scratch_bound_keysym = keysym
    return _scratch_keycode


def _tap_keycode(keycode: int, press: bool) -> None:
    d = _d()
    event = Xlib.X.KeyPress if press else Xlib.X.KeyRelease
    Xlib.ext.xtest.fake_input(d, event, keycode)


def key_down(key: str) -> None:
    _tap_keycode(_keysym_to_keycode(resolve_keysym(key)), press=True)
    _d().sync()


def key_up(key: str) -> None:
    _tap_keycode(_keysym_to_keycode(resolve_keysym(key)), press=False)
    _d().sync()


def press_key(key: str, modifiers: list[str] | None = None) -> None:
    mod_codes = [_keysym_to_keycode(resolve_keysym(m)) for m in (modifiers or [])]
    main_code = _keysym_to_keycode(resolve_keysym(key))
    for mc in mod_codes:
        _tap_keycode(mc, press=True)
    _tap_keycode(main_code, press=True)
    _tap_keycode(main_code, press=False)
    for mc in reversed(mod_codes):
        _tap_keycode(mc, press=False)
    _d().sync()


def type_text(text: str) -> None:
    """One-shot-per-character press+release -- matches keyboard_plugin.py's
    Windows type_text semantics (plain text, no modifier combos). Routes
    every character through _keysym_to_keycode, so a character missing
    from the CURRENT keyboard layout (confirmed live, 2026-10-04: this
    machine's X server layout is Russian, so e.g. plain Latin "a" has no
    keycode at all) still gets typed via the scratch-keycode remap, not
    silently dropped. A character with no keysym at all (resolved via
    Xlib's own Unicode keysym space, 0x01000000 + codepoint, per the X11
    "Unicode keysym" extension most modern X servers honor) uses that."""
    for ch in text:
        ks = Xlib.XK.string_to_keysym(ch)
        if ks == 0:
            ks = 0x01000000 + ord(ch)
        keycode = _keysym_to_keycode(ks)
        _tap_keycode(keycode, press=True)
        _tap_keycode(keycode, press=False)
    _d().sync()


# --- posted/synthetic (non-focus-stealing) window-targeted input -----------
# The X11 analog of window_mouse_plugin.py/window_keyboard_plugin.py's
# Windows PostMessage-based click_window/type_window/press_window_key:
# XSendEvent delivers a synthetic event directly to one window without
# moving the real pointer, changing input focus, or raising/activating it.
# Confirmed live (WSL2 + WSLg, 2026-10-04): a plain X11 window listening
# for ButtonPressMask receives it correctly. Same documented caveat as the
# Windows posted-message tools already carry for GPU-rendered custom
# controls (Chromium/Electron/games): many modern toolkits deliberately
# ignore input events carrying the send_event=true flag (a deliberate
# X11 trust boundary, not a bug) and need real XTest input instead -- this
# is the direct Linux counterpart of that same fidelity gap, not a new one.


def send_window_button(window: Any, x: int, y: int, root_x: int, root_y: int, press: bool, button: str | None = None) -> None:
    d = _d()
    root = d.screen().root
    btn = _resolve_button(button)
    cls = Xlib.protocol.event.ButtonPress if press else Xlib.protocol.event.ButtonRelease
    event = cls(
        time=Xlib.X.CurrentTime, root=root, window=window, child=Xlib.X.NONE,
        root_x=root_x, root_y=root_y, event_x=x, event_y=y,
        state=0, detail=btn, same_screen=1,
    )
    window.send_event(event, event_mask=Xlib.X.ButtonPressMask if press else Xlib.X.ButtonReleaseMask, propagate=False)
    d.sync()


def send_window_click(window: Any, x: int, y: int, root_x: int, root_y: int, button: str | None = None) -> None:
    send_window_button(window, x, y, root_x, root_y, press=True, button=button)
    send_window_button(window, x, y, root_x, root_y, press=False, button=button)


def send_window_key(window: Any, keycode: int, press: bool, modifier_state: int = 0) -> None:
    d = _d()
    root = d.screen().root
    cls = Xlib.protocol.event.KeyPress if press else Xlib.protocol.event.KeyRelease
    event = cls(
        time=Xlib.X.CurrentTime, root=root, window=window, child=Xlib.X.NONE,
        root_x=0, root_y=0, event_x=0, event_y=0,
        state=modifier_state, detail=keycode, same_screen=1,
    )
    window.send_event(event, event_mask=Xlib.X.KeyPressMask if press else Xlib.X.KeyReleaseMask, propagate=False)
    d.sync()


def send_window_text(window: Any, text: str) -> None:
    for ch in text:
        ks = Xlib.XK.string_to_keysym(ch)
        if ks == 0:
            ks = 0x01000000 + ord(ch)
        keycode = _keysym_to_keycode(ks)
        send_window_key(window, keycode, press=True)
        send_window_key(window, keycode, press=False)


def send_window_combo(window: Any, key: str, modifiers: list[str] | None = None) -> None:
    mod_codes = [_keysym_to_keycode(resolve_keysym(m)) for m in (modifiers or [])]
    main_code = _keysym_to_keycode(resolve_keysym(key))
    for mc in mod_codes:
        send_window_key(window, mc, press=True)
    send_window_key(window, main_code, press=True)
    send_window_key(window, main_code, press=False)
    for mc in reversed(mod_codes):
        send_window_key(window, mc, press=False)
