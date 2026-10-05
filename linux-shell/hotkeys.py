"""Port of Windows/Caroline/Interop/GlobalHotkeyService.cs -- a system-wide
hotkey independent of window focus. RegisterHotKey/WM_HOTKEY's direct X11
analog is XGrabKey on the root window + a KeyPress event loop; unlike
Windows, X11 has no single "this hotkey is already taken" signal from the
grab call itself (BadAccess arrives asynchronously as an error event), so
failure is detected via an X error handler instead of a return value.

Runs its own Xlib connection on a background thread (python-xlib's event
loop is blocking) and marshals callbacks onto the GTK main loop via
GLib.idle_add, since GTK widgets must only be touched from the main thread.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

import Xlib.X
import Xlib.XK
import Xlib.display
import Xlib.error
from gi.repository import GLib

# Same defaults as Models/AppSettings.cs's own HotkeyModifiers/HotkeyVirtualKey
# (Ctrl+Alt) and MainWindow.xaml.cs's hardcoded Ctrl+Shift for voice record --
# no settings persistence here yet (see caroline_shell.py's own docstring), so
# these aren't user-configurable on Linux yet either.
MOD_CONTROL = Xlib.X.ControlMask
MOD_ALT = Xlib.X.Mod1Mask
MOD_SHIFT = Xlib.X.ShiftMask

# X11 grabs care about the exact modifier state, including lock/numlock
# toggles -- grabbing only the bare combo would silently stop firing the
# moment Caps Lock or Num Lock is on. Mirrors what every real X11 hotkey
# daemon (e.g. sxhkd) does: grab the combo under every lock-key combination
# too, matching Win32's RegisterHotKey which ignores lock-key state entirely.
_IGNORED_LOCKS = (0, Xlib.X.LockMask, Xlib.X.Mod2Mask, Xlib.X.LockMask | Xlib.X.Mod2Mask)


@dataclass
class _Binding:
    keycode: int
    modifiers: int
    callback: Callable[[], None]


class GlobalHotkeyListener:
    def __init__(self) -> None:
        self._display = Xlib.display.Display()
        self._root = self._display.screen().root
        self._bindings: list[_Binding] = []
        self._thread: threading.Thread | None = None
        self._stopping = False

    def register(self, modifiers: int, key_name: str, callback: Callable[[], None]) -> bool:
        """key_name: a single character (e.g. "c") or an Xlib.XK name suffix.
        Returns False (logged by the caller) if this layout has no keycode
        for the requested key at all -- same graceful-failure contract as
        GlobalHotkeyService.Register's bool return."""
        keysym = Xlib.XK.string_to_keysym(key_name)
        if keysym == 0:
            keysym = getattr(Xlib.XK, f"XK_{key_name}", 0)
        if keysym == 0:
            return False
        keycode = self._display.keysym_to_keycode(keysym)
        if keycode == 0:
            return False
        for lock in _IGNORED_LOCKS:
            self._root.grab_key(keycode, modifiers | lock, True, Xlib.X.GrabModeAsync, Xlib.X.GrabModeAsync)
        self._display.sync()
        self._bindings.append(_Binding(keycode, modifiers, callback))
        return True

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stopping:
            event = self._display.next_event()
            if event.type != Xlib.X.KeyPress:
                continue
            # Mask off the lock-key bits we deliberately grabbed under too,
            # so e.g. Ctrl+Alt+C with Caps Lock on still matches the plain
            # Ctrl+Alt+C binding.
            state = event.state & ~(Xlib.X.LockMask | Xlib.X.Mod2Mask)
            for binding in self._bindings:
                if binding.keycode == event.detail and binding.modifiers == state:
                    GLib.idle_add(binding.callback)
                    break

    def stop(self) -> None:
        """Ungrabs every key. Doesn't try to wake the listener thread's
        blocking next_event() call -- it's a daemon thread (start()), so it
        dies with the process regardless; nothing currently calls stop()
        outside of process shutdown anyway."""
        self._stopping = True
        for binding in self._bindings:
            for lock in _IGNORED_LOCKS:
                try:
                    self._root.ungrab_key(binding.keycode, binding.modifiers | lock)
                except Xlib.error.XError:
                    pass
        self._display.sync()
