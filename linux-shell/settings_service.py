"""Port of Windows/Caroline/Services/SettingsService.cs + Models/AppSettings.cs
-- persists window geometry, always-on-top, the toggle-visibility global
hotkey, and open-tab-ids/tab-names, so a relaunch restores the same layout
instead of GTK/hardcoded defaults every time (see caroline_shell.py's own
module docstring, "no SettingsService equivalent here yet").

Same directory convention caroline_shell.py's own LOG_PATH already uses
(~/.local/share/Caroline), not a fresh XDG_DATA_HOME-aware path -- no
reason for settings.json to live somewhere shell.log doesn't.

Window geometry uses None (not NaN) as the "never customized yet" sentinel
-- the whole reason Windows' own JsonOptions needs AllowNamedFloatingPointLiterals
is that System.Text.Json throws on NaN by default; plain JSON null has no
such problem and needs no special-casing on either read or write.

The hotkey is stored in the SAME terms hotkeys.py's GlobalHotkeyListener.
register() already takes (an X11 modifier-mask int, a key name string) --
inventing a cross-platform encoding shared with Windows' raw Win32 VK
codes would be translation work nobody asked for, since neither shell
exposes a settings UI to EDIT this today; it only needs to round-trip
through its own native representation if a user hand-edits the file."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SETTINGS_DIR = Path.home() / ".local" / "share" / "Caroline"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"
BACKUP_PATH = SETTINGS_DIR / "settings.json.bak"

# Same defaults as hotkeys.py's own hardcoded registration call and
# Models/AppSettings.cs's HotkeyModifiers/HotkeyVirtualKey (Ctrl+Alt+C).
DEFAULT_HOTKEY_MODIFIERS = 0x0002 | 0x0001  # Xlib.X.ControlMask | Xlib.X.Mod1Mask, avoiding an Xlib import here
DEFAULT_HOTKEY_KEY = "c"


@dataclass
class AppSettings:
    always_on_top: bool = True
    hotkey_modifiers: int = DEFAULT_HOTKEY_MODIFIERS
    hotkey_key: str = DEFAULT_HOTKEY_KEY
    # None means "never customized/saved yet" for all four -- caroline_shell.py
    # falls back to its own hardcoded default geometry in that case, same
    # shape as Windows' NaN sentinel (see module docstring for why None
    # instead of NaN here).
    window_left: float | None = None
    window_top: float | None = None
    window_width: float | None = None
    window_height: float | None = None
    open_tab_ids: list[str] = field(default_factory=lambda: ["1"])
    tab_names: dict[str, str] = field(default_factory=dict)


class SettingsService:
    def __init__(self, log: Any = None) -> None:
        self._log = log or (lambda _line: None)
        # Set when settings.json existed but could not be read -- saving
        # over it then would replace the user's real settings with
        # defaults, so save() refuses, same as SettingsService.cs.
        self._load_failed = False

    def load(self) -> AppSettings:
        if SETTINGS_PATH.exists():
            try:
                raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self._load_failed = True
                self._log(f"[SettingsService] load: {SETTINGS_PATH} unreadable/corrupt, using defaults and refusing to overwrite it: {exc}")
                return AppSettings()
            settings = AppSettings(**{k: v for k, v in raw.items() if k in AppSettings.__dataclass_fields__})
            self._log(f"[SettingsService] load: open_tab_ids={settings.open_tab_ids} tab_names={len(settings.tab_names)}")
            return settings
        return AppSettings()

    def save(self, settings: AppSettings, caller: str = "") -> None:
        if self._load_failed:
            self._log(f"[SettingsService] save from {caller} refused: settings.json could not be read at startup; not overwriting it")
            return
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        self._log(f"[SettingsService] save from {caller}: open_tab_ids={settings.open_tab_ids} tab_names={len(settings.tab_names)}")
        if SETTINGS_PATH.exists():
            BACKUP_PATH.write_text(SETTINGS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        temp_path = SETTINGS_PATH.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
        temp_path.replace(SETTINGS_PATH)
