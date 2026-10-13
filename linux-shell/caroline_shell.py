"""Caroline's Linux desktop shell: a GTK window hosting the existing chat page
(Windows/Caroline/wwwroot) in WebKit2, plus the supervisor/backend lifecycle.

Replaces the WPF shell's job on Linux (see docs/LINUX_PORT_PLAN.md, Phase 4).
The page talks to the native host through window.chrome.webview.postMessage
(a WebView2 API), so a small document-start shim maps that call onto a WebKit
script message handler. Messages the page sends are handled here; anything not
handled yet is logged, so gaps show up in the log instead of failing silently.

Tab strip mirrors Windows/Caroline/MainWindow.xaml.cs as closely as GTK
allows: each tab is its own WebKit2.WebView loaded at chat.html?port=...
&tab=<id>&alwaysOnTop=...&assetsVersion=..., same query contract chat.js
already expects, with a name button plus a hamburger menu (Mode submenu /
Clear / Close). Window position/size, always-on-top, open-tab-ids/
tab-names and the toggle-visibility hotkey now persist too, via
settings_service.py (port of Services/SettingsService.cs). Still not
ported from that file: the tray icon (so there is no "minimize and keep
running" on Linux yet -- closing the window quits the app outright), the
update-available banner, and the OnlyOffice document editor.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, WebKit2  # noqa: E402

from hotkeys import MOD_ALT, MOD_CONTROL, MOD_SHIFT, GlobalHotkeyListener  # noqa: E402
from settings_service import AppSettings, SettingsService  # noqa: E402
from viewer_window import ViewerResult, ViewerWindow  # noqa: E402

HERE = Path(__file__).resolve().parent
# pystray is vendored here (see vendor/pystray/NOTICE.md for why: PEP 668
# blocks pip into the system python3 this shell runs under, and pystray
# isn't a Debian/Ubuntu apt package the way python3-xlib is) -- importing
# it (done lazily, in _build_tray_icon) needs this on sys.path first.
sys.path.insert(0, str(HERE / "vendor"))
APP_ROOT = Path(os.environ.get("CAROLINE_APP_ROOT", HERE.parent))
def _default_wwwroot() -> Path:
    """A packaged install bundles wwwroot as a sibling of linux-shell/ (see
    the Makefile's Linux packaging target) -- that's checked first. Falling
    back to the WPF project's own copy only matters for running straight out
    of a dev-tree checkout, where no packaging step has run yet; a real
    install never has a Windows/ directory at all."""
    packaged = HERE.parent / "wwwroot"
    if packaged.is_dir():
        return packaged
    return HERE.parent / "Windows" / "Caroline" / "wwwroot"


WWWROOT = Path(os.environ.get("CAROLINE_WWWROOT", _default_wwwroot()))
SUPERVISOR_PY = Path(os.environ.get("CAROLINE_SUPERVISOR_PY", HERE.parent / "backend-py" / "supervisor.py"))
PYTHON = Path(os.environ.get("CAROLINE_PYTHON", APP_ROOT / "runtime" / "python" / "bin" / "python3"))
PAGE_PORT = 48767
SUPERVISOR_PORT = 48766
BACKEND_PORT = 48765
SUPERVISOR_STATUS_POLL_INTERVAL_MS = 15_000
MAX_TABS = 5
LOG_PATH = Path(os.environ.get("CAROLINE_SHELL_LOG", Path.home() / ".local" / "share" / "Caroline" / "shell.log"))
FONTS_DIR = HERE / "fonts"
ICONS_DIR = HERE / "icons"
APP_ICON_PATH = ICONS_DIR / "app.ico"

# The page asks for "Segoe UI" (Windows). Selawik (SIL OFL, Microsoft) has the
# same metrics and is shipped with the shell, so map Segoe UI's weights onto it.
FONT_CSS = """
@font-face { font-family: "Segoe UI"; font-weight: 300; src: url("__ORIGIN__/fonts/selawkl.woff2") format("woff2"); }
@font-face { font-family: "Segoe UI"; font-weight: 400; src: url("__ORIGIN__/fonts/selawk.woff2") format("woff2"); }
@font-face { font-family: "Segoe UI"; font-weight: 600; src: url("__ORIGIN__/fonts/selawksb.woff2") format("woff2"); }
@font-face { font-family: "Segoe UI"; font-weight: 700; src: url("__ORIGIN__/fonts/selawkb.woff2") format("woff2"); }
"""

ICON_CSS = """
#attachBtn, #micBtn, #stopBtn, #sendBtn, #settingsBtn, #closeSettingsBtn,
#voiceRestartBtn, #voiceStopBtn {
  font-size: 0 !important; background-repeat: no-repeat !important;
  background-position: center !important; background-size: 18px 18px !important;
  min-width: 22px; min-height: 22px;
}
#attachBtn { background-image: url("__ORIGIN__/icons/attach-dark.png") !important; }
#micBtn { background-image: url("__ORIGIN__/icons/mic-dark.png") !important; }
#stopBtn { background-image: url("__ORIGIN__/icons/stop.png") !important; }
#sendBtn { background-image: url("__ORIGIN__/icons/send.png") !important; }
#settingsBtn { background-image: url("__ORIGIN__/icons/settings.png") !important; }
#closeSettingsBtn { background-image: url("__ORIGIN__/icons/close.png") !important; }
#voiceStopBtn { background-image: url("__ORIGIN__/icons/stop-dark.png") !important; }
#voiceRestartBtn { background-image: url("__ORIGIN__/icons/restart-dark.png") !important; }
img.cg { width: 16px; height: 16px; vertical-align: -3px; display: inline-block; }
"""

GLYPH_JS = """
(function () {
  var ICONS = {
    "\\uD83D\\uDCCE": "attach-dark", "\\uD83C\\uDF99\\uFE0F": "mic-dark", "\\uD83C\\uDF99": "mic-dark",
    "\\uD83D\\uDD0A": "speaker-dark", "\\uD83D\\uDCC4": "document-dark",
    "\\uD83D\\uDDBC\\uFE0F": "image-dark", "\\uD83D\\uDDBC": "image-dark", "\\uD83C\\uDFAC": "clapper-dark",
    "\\u23F8": "pause-dark", "\\u25B6": "play-dark", "\\u2715": "close-dark",
    "\\u2714": "check-dark", "\\u25BE": "chevron-dark"
  };
  var KEYS = Object.keys(ICONS).sort(function (a, b) { return b.length - a.length; });
  var HOSTS = { BUTTON: 1, SPAN: 1, A: 1 };
  var CSS_ICON_BUTTONS = { attachBtn: 1, micBtn: 1, closeSettingsBtn: 1 };

  function swap(node) {
    var parent = node.parentNode;
    if (!parent || !HOSTS[parent.nodeName]) return;
    if (CSS_ICON_BUTTONS[parent.id]) return;
    var text = node.nodeValue;
    for (var i = 0; i < KEYS.length; i++) {
      var at = text.indexOf(KEYS[i]);
      if (at === -1) continue;
      var img = document.createElement("img");
      img.className = "cg";
      img.alt = "";
      img.src = "__ORIGIN__/icons/" + ICONS[KEYS[i]] + ".png";
      var rest = document.createTextNode(text.slice(at + KEYS[i].length));
      node.nodeValue = text.slice(0, at);
      parent.insertBefore(img, node.nextSibling);
      parent.insertBefore(rest, img.nextSibling);
      return swap(rest);
    }
  }

  function walk(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    var nodes = [];
    for (var n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n);
    nodes.forEach(swap);
  }

  function start() {
    walk(document.body);
    new MutationObserver(function (mutations) {
      mutations.forEach(function (m) {
        if (m.type === "characterData") swap(m.target);
        m.addedNodes.forEach(function (c) {
          if (c.nodeType === 3) swap(c);
          else if (c.nodeType === 1) walk(c);
        });
      });
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
"""

PAGE_CSS = (FONT_CSS + ICON_CSS).replace("__ORIGIN__", f"http://127.0.0.1:{PAGE_PORT}")

SHIM_JS = """
window.chrome = window.chrome || {};
(function () {
  var listeners = [];
  window.chrome.webview = {
    postMessage: function (obj) {
      window.webkit.messageHandlers.caroline.postMessage(JSON.stringify(obj));
    },
    addEventListener: function (name, fn) {
      if (name === "message") listeners.push(fn);
    },
    removeEventListener: function (name, fn) {
      if (name === "message") listeners = listeners.filter(function (f) { return f !== fn; });
    },
    // Python-side equivalent of CoreWebView2.PostWebMessageAsJson: dispatches
    // a "message" event to every addEventListener("message", ...) listener,
    // e.data set to the parsed payload -- same shape monaco_viewer.html/
    // slideshow.html already expect from the WebView2 build.
    __dispatch: function (jsonText) {
      var data = JSON.parse(jsonText);
      listeners.forEach(function (fn) { fn({ data: data }); });
    }
  };
})();
"""
SHIM_JS += GLYPH_JS.replace("__ORIGIN__", f"http://127.0.0.1:{PAGE_PORT}")

TAB_STRIP_CSS = b"""
#tab-strip { background-color: #3A3F8F; }
#tab-strip button { background: transparent; color: white; border: none;
  border-radius: 0; padding: 6px 10px; box-shadow: none; }
#tab-strip button.active { background-color: #4169E1; }
#tab-strip button:hover { background-color: alpha(white, 0.08); }
#tab-strip entry { min-width: 60px; }
"""

MODE_LABELS = (("claude", "Claude"), ("sw", "Squirrel Wisdom"), ("openai", "OpenAI"))

SPLASH_BANNERS_DIR = Path(os.environ.get("CAROLINE_SPLASH_BANNERS", APP_ROOT / "SplashBanners"))
SPLASH_CYCLE_INTERVAL_S = 2.5
# Same three constants as App.xaml.cs's own SplashMinDuration/SplashMaxWait/
# BackendPollInterval: a floor so a warm start still gets a brief branding
# beat, a ceiling so a genuinely broken backend doesn't strand the user on
# the splash forever (MainWindow's own health watchdog takes over either
# way), both mirrored exactly rather than re-tuned for this platform.
SPLASH_MIN_DURATION_S = 3.0
SPLASH_MAX_WAIT_S = 5 * 60.0
BACKEND_POLL_INTERVAL_S = 1.0

SPLASH_CSS = b"""
window.splash { background-color: #3A3F8F; }
#splash-banner { background-color: #000; border-radius: 18px;
  background-size: cover; background-position: center; background-repeat: no-repeat; }
#splash-text { color: white; }
"""


def log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {line}\n")


def _install_global_exception_logging() -> None:
    """Port of App.xaml.cs's InstallGlobalExceptionLogging: on Windows that
    catches DispatcherUnhandledException (UI thread, marked Handled so the
    app survives), AppDomain.UnhandledException, and TaskScheduler.
    UnobservedTaskException, so nothing dies with zero trace in
    caroline.log. The Python/GLib equivalents of those three: sys.
    excepthook (uncaught exceptions on the main thread -- PyGObject routes
    an exception raised inside a GTK/GLib signal callback through this the
    same way a plain script would) and threading.excepthook (background
    threads, e.g. Supervisor._pump_output -- there is no Python-level
    "unobserved" concept for bare threading.Thread the way asyncio/Task
    has one, so this is the closest equivalent). Logs only -- never
    re-raises or exits, matching Windows' Handled=true behavior of
    keeping the app alive."""
    def log_main_thread_exception(exc_type, exc_value, exc_tb) -> None:
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log(f"UNHANDLED EXCEPTION (main thread):\n{detail}")

    def log_background_thread_exception(args: threading.ExceptHookArgs) -> None:
        detail = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        name = args.thread.name if args.thread else "?"
        log(f"UNHANDLED EXCEPTION (thread {name}):\n{detail}")

    sys.excepthook = log_main_thread_exception
    threading.excepthook = log_background_thread_exception


def _is_trusted_source(uri: str | None) -> bool:
    """Port of WebViewOriginGuard.IsTrustedSource (Windows/Caroline/
    Services/WebViewOriginGuard.cs): every webview this shell creates for
    its own chat tabs only ever navigates to chat.html served from the
    local HTTP server (see add_tab's own load_uri call) -- checked here
    the same defensive way Windows checks its WebView2 message Source,
    even though nothing untrusted is reachable today under the current
    navigation policy."""
    if not uri:
        return False
    return uri.startswith(f"http://127.0.0.1:{PAGE_PORT}/")


class Supervisor:
    """Starts backend-py/supervisor.py from the bundled runtime. The Python
    side already sets PR_SET_PDEATHSIG on itself (see supervisor.py), so the
    supervisor and the backend it owns die with this shell however it exits."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> bool:
        if not PYTHON.exists():
            log(f"bundled python not found at {PYTHON} -- refusing to fall back to system python")
            return False
        env = dict(os.environ)
        env["CAROLINE_APP_ROOT"] = str(APP_ROOT)
        env["CAROLINE_PORT"] = str(BACKEND_PORT)
        env["CAROLINE_SUPERVISOR_PORT"] = str(SUPERVISOR_PORT)
        self.proc = subprocess.Popen(
            [str(PYTHON), str(SUPERVISOR_PY)],
            cwd=str(SUPERVISOR_PY.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        threading.Thread(target=self._pump_output, daemon=True).start()
        log(f"supervisor started, pid={self.proc.pid}")
        return True

    def _pump_output(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            log("[backend] " + raw.decode("utf-8", errors="replace").rstrip())

    def status(self) -> str:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{SUPERVISOR_PORT}/status", timeout=2) as r:
                return r.read().decode("utf-8")
        except Exception as exc:
            return f"unreachable ({exc.__class__.__name__})"

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)


class PageHandler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        path = urllib.parse.urlparse(path).path
        if path.startswith("/fonts/"):
            return str(FONTS_DIR / Path(path[len("/fonts/"):]).name)
        if path.startswith("/icons/"):
            return str(ICONS_DIR / Path(path[len("/icons/"):]).name)
        return super().translate_path(path)

    def log_message(self, format, *args):  # noqa: A002
        return


def serve_wwwroot() -> http.server.ThreadingHTTPServer:
    handler = functools.partial(PageHandler, directory=str(WWWROOT))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PAGE_PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _assets_version() -> int:
    try:
        return max(
            (WWWROOT / "chat.js").stat().st_mtime_ns,
            (WWWROOT / "chat.css").stat().st_mtime_ns,
        )
    except OSError as exc:
        log(f"could not read chat.js/chat.css mtime for assetsVersion (using 0): {exc}")
        return 0


class ChatTab:
    """Analog of MainWindow.xaml.cs's private ChatTab -- one per open chat,
    each with its own WebKit2.WebView (own DOM/JS/WebSocket state) navigated
    to chat.html?tab=<id>, same query contract the Windows WebView2 build
    already uses, so chat.js needs no changes at all."""

    def __init__(self, tab_id: str, name: str) -> None:
        self.id = tab_id
        self.name = name
        self.chat_mode = "claude"
        self.mode_available = {"claude": True, "sw": False, "openai": False}

        self.header_button = Gtk.Button(label=name)
        self.header_button.set_relief(Gtk.ReliefStyle.NONE)
        self.menu_button = Gtk.MenuButton()
        self.menu_button.set_relief(Gtk.ReliefStyle.NONE)
        self.menu_button.set_tooltip_text("Tab menu")
        menu_icon_path = ICONS_DIR / "menu.png"
        if menu_icon_path.exists():
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(menu_icon_path), 16, 16, True)
            self.menu_button.set_image(Gtk.Image.new_from_pixbuf(pixbuf))
        else:
            self.menu_button.set_label("=")

        self.header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.header_box.get_style_context().add_class("tab-header")
        self.header_box.pack_start(self.header_button, False, False, 0)
        self.header_box.pack_start(self.menu_button, False, False, 0)

        self.mode_items: dict[str, Gtk.CheckMenuItem] = {}
        self.close_item: Gtk.MenuItem | None = None

        self.webview: WebKit2.WebView | None = None


class SplashWindow:
    """Port of SplashWindow.xaml(.cs): a chrome-less, transparent window
    cycling the onboarding banners with a "Connecting..." label, shown while
    the backend starts and dismissed by App.axaml.cs's equivalent
    (wait_for_backend_then_show below) once it's actually healthy, a click,
    or a timeout, whichever comes first.

    True per-pixel window transparency depends on an RGBA visual being
    available from the X server/compositor -- requested here, but not
    guaranteed on every Linux desktop the way WPF's AllowsTransparency is
    on Windows; falls back to an opaque dark window if the screen has no
    such visual, rather than failing to show anything."""

    def __init__(self, on_dismissed) -> None:
        self.on_dismissed = on_dismissed
        self._photos: list[Path] = []
        self._photo_index = 0
        self._cycle_source: int | None = None
        self._dismissed = False

        self.window = Gtk.Window(title="Caroline")
        self.window.set_decorated(False)
        self.window.set_default_size(480, 320)
        self.window.set_resizable(False)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        self.window.set_skip_taskbar_hint(True)
        screen = self.window.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.window.set_visual(visual)
            self.window.set_app_paintable(True)
            self.window.connect("draw", self._draw_transparent_bg)
        else:
            # No compositor (confirmed live: WSLg and this test box's Xvfb+
            # openbox both lack one) -- an opaque window with GTK's default
            # light background left "Connecting..." (white text) unreadable.
            # Falls back to the same indigo used elsewhere in the UI
            # (TabStrip's own #3A3F8F) rather than trying for transparency
            # that isn't actually available.
            self.window.get_style_context().add_class("splash")
        self.window.connect("button-press-event", lambda *_a: self.dismiss())

        css = Gtk.CssProvider()
        css.load_from_data(SPLASH_CSS)
        Gtk.StyleContext.add_provider_for_screen(screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        self.banner = Gtk.EventBox()
        self.banner.set_name("splash-banner")
        self.banner.set_size_request(480, 270)
        self.text = Gtk.Label(label="Connecting...")
        self.text.set_name("splash-text")
        box.pack_start(self.banner, False, False, 0)
        box.pack_start(self.text, False, False, 0)
        self.window.add(box)

        self._load_banners()

    @staticmethod
    def _draw_transparent_bg(_widget, ctx) -> bool:
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.set_operator(1)  # cairo.OPERATOR_SOURCE
        ctx.paint()
        return False

    def _load_banners(self) -> None:
        try:
            if SPLASH_BANNERS_DIR.is_dir():
                self._photos = sorted(SPLASH_BANNERS_DIR.glob("*.png"))
        except OSError as exc:
            log(f"splash: could not list {SPLASH_BANNERS_DIR}: {exc}")
        if not self._photos:
            log(f"splash: no banners found under {SPLASH_BANNERS_DIR} -- showing a plain panel")
            return
        import random

        self._photo_index = random.randrange(len(self._photos))
        self._show_photo(self._photo_index)
        if len(self._photos) > 1:
            self._cycle_source = GLib.timeout_add(int(SPLASH_CYCLE_INTERVAL_S * 1000), self._on_cycle_tick)

    def _on_cycle_tick(self) -> bool:
        self._photo_index = (self._photo_index + 1) % len(self._photos)
        self._show_photo(self._photo_index)
        return True

    def _show_photo(self, index: int) -> None:
        path = self._photos[index].as_posix()
        css = f'#splash-banner {{ background-image: url("file://{path}"); }}'.encode()
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(css)
        except GLib.Error as exc:
            log(f"splash: could not load banner {path}: {exc}")
            return
        self.banner.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def show(self) -> None:
        self.window.show_all()

    def dismiss(self) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        if self._cycle_source is not None:
            GLib.source_remove(self._cycle_source)
            self._cycle_source = None
        self.window.destroy()
        self.on_dismissed()


def wait_for_backend_then_show(supervisor: Supervisor, splash: SplashWindow, reveal) -> None:
    """Port of App.xaml.cs's WaitForSplashDismissAsync: polls the BACKEND's
    own /api/status (not the supervisor's /status -- a different endpoint,
    same one chat.js's WS connects alongside) once a second, holding the
    splash until it reports no tab mid-startup-compaction AND at least
    SPLASH_MIN_DURATION_S has passed, or until SPLASH_MAX_WAIT_S elapses
    regardless. A click (SplashWindow.dismiss) short-circuits this entirely
    via the on_dismissed callback already firing reveal()."""
    start = time.monotonic()
    state = {"revealed": False}

    def do_reveal() -> None:
        if state["revealed"]:
            return
        state["revealed"] = True
        reveal()

    original_dismiss = splash.dismiss

    def dismiss_and_reveal() -> None:
        original_dismiss()
        do_reveal()

    splash.dismiss = dismiss_and_reveal  # type: ignore[method-assign]

    def backend_healthy() -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{BACKEND_PORT}/api/status", timeout=2) as r:
                body = json.loads(r.read().decode("utf-8"))
        except Exception:
            return False
        tabs = body.get("tabs")
        if isinstance(tabs, list):
            if any(t.get("forcedCompactionPending") for t in tabs if isinstance(t, dict)):
                return False
        return True

    def tick() -> bool:
        if state["revealed"]:
            return False
        elapsed = time.monotonic() - start
        if elapsed >= SPLASH_MAX_WAIT_S:
            log("wait_for_backend_then_show: SPLASH_MAX_WAIT_S elapsed, showing anyway")
            dismiss_and_reveal()
            return False
        if backend_healthy() and elapsed >= SPLASH_MIN_DURATION_S:
            dismiss_and_reveal()
            return False
        return True

    GLib.timeout_add(int(BACKEND_POLL_INTERVAL_S * 1000), tick)


class Shell:
    def __init__(self, supervisor: Supervisor, settings: AppSettings, settings_service: SettingsService) -> None:
        self.supervisor = supervisor
        self.settings = settings
        self.settings_service = settings_service
        self.tabs: list[ChatTab] = []
        self.active_tab: ChatTab | None = None
        self.viewer_windows: dict[str, ViewerWindow] = {}
        self._gave_up_shown = False
        # Port of MainWindow's _restoringTabs: suppresses PersistOpenTabIds-
        # equivalent saves while prepare() recreates the previously-open
        # tabs, so restoring them doesn't itself get recorded as a change.
        self._restoring_tabs = False
        # Port of MainWindow's _exitRequested: a plain X-button close hides
        # (tray keeps the app running, see _on_delete_event), same as
        # Windows' OnClosing; only RequestExit (the tray's own "Exit" item,
        # or SIGTERM/SIGINT) actually tears the app down.
        self._exit_requested = False
        # Set by main() right after construction, if a tray icon could be
        # created at all (see _build_tray_icon) -- stopped from on_destroy.
        self.tray_icon: Any = None

        self.window = Gtk.Window(title="Caroline")
        if settings.window_width is not None and settings.window_height is not None:
            self.window.set_default_size(int(settings.window_width), int(settings.window_height))
        else:
            self.window.set_default_size(420, 640)
        geometry = Gdk.Geometry()
        geometry.min_width = 320
        geometry.min_height = 400
        self.window.set_geometry_hints(None, geometry, Gdk.WindowHints.MIN_SIZE)
        self.window.set_keep_above(settings.always_on_top)
        if APP_ICON_PATH.exists():
            try:
                self.window.set_icon_from_file(str(APP_ICON_PATH))
            except GLib.Error as exc:
                log(f"Shell: set_icon_from_file failed: {exc}")
        self.window.connect("destroy", self.on_destroy)
        self.window.connect("delete-event", self._on_delete_event)

        css = Gtk.CssProvider()
        css.load_from_data(TAB_STRIP_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.tab_strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.tab_strip.set_name("tab-strip")
        root.pack_start(self.tab_strip, False, False, 0)

        self.content_stack = Gtk.Stack()
        root.pack_start(self.content_stack, True, True, 0)
        self.window.add(root)

        self.add_tab_button = Gtk.Button(label="+")
        self.add_tab_button.set_relief(Gtk.ReliefStyle.NONE)
        self.add_tab_button.connect("clicked", lambda _b: self.add_tab(self._next_tab_id(), select=True))

    def prepare(self) -> None:
        """Creates the first tab (and its WebKit2.WebView, already navigating
        to chat.html) without showing the window -- unlike the WPF build,
        no WebView2 "airspace" bug forces a Show()/Hide() dance here first;
        a GTK window never shown at all is enough. Called while the splash
        is up so the chat page is already loading underneath it."""
        self._restoring_tabs = True
        tab_ids = self.settings.open_tab_ids or ["1"]
        for i, tab_id in enumerate(tab_ids):
            self.add_tab(tab_id, select=(i == 0))
        self._restoring_tabs = False
        self._persist_open_tab_ids()
        GLib.timeout_add(SUPERVISOR_STATUS_POLL_INTERVAL_MS, self._poll_supervisor_status)

    def _persist_open_tab_ids(self) -> None:
        """Port of MainWindow's PersistOpenTabIds."""
        if self._restoring_tabs:
            return
        self.settings.open_tab_ids = [t.id for t in self.tabs]
        self.settings_service.save(self.settings, caller="_persist_open_tab_ids")

    def _poll_supervisor_status(self) -> bool:
        """Port of MainWindow.xaml.cs's PollSupervisorStatus: the
        supervisor's own health-poll loop autonomously detects and
        recovers a frozen/crashed backend (whole-process AND per-tab)
        without any help from here. This only watches for the one case
        still worth a human-visible dialog -- has it given up
        auto-restarting (needed rescue more than MAX_RESTARTS_PER_WINDOW
        times)? Shown exactly once per session, same as Windows. Returns
        True/False the way every GLib.timeout_add callback in this file
        does -- True to keep polling, False to stop."""
        if self._gave_up_shown:
            return False
        raw = self.supervisor.status()
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            return True
        if body.get("gaveUp"):
            self._gave_up_shown = True
            log("Shell: supervisor reports it has given up auto-restarting the backend")
            show_error_dialog("Caroline's backend keeps failing", f"Check {LOG_PATH}, or restart the app.")
            return False
        return True

    def reveal(self) -> None:
        self.window.show_all()
        if self.settings.window_left is not None and self.settings.window_top is not None:
            self.window.move(int(self.settings.window_left), int(self.settings.window_top))

    def toggle_visibility(self) -> None:
        """Port of ToggleVisibility: hide if shown, show+raise+focus if
        hidden -- same global-hotkey toggle, bound in main() to
        settings.hotkey_modifiers/hotkey_key (Ctrl+Alt+C by default, see
        settings_service.py)."""
        if self.window.get_visible():
            self.window.hide()
        else:
            self.window.present()

    def show_and_activate(self) -> None:
        """Port of MainWindow's ShowAndActivate -- the tray's "Open
        Caroline" item and its default (left-)click both use this rather
        than toggle_visibility, matching Windows: always brings the window
        to front, never hides an already-visible one."""
        self.window.show_all()
        self.window.deiconify()
        self.window.present()

    def _on_delete_event(self, _widget, _event) -> bool:
        """Port of MainWindow's OnClosing: closing via the window's own X
        button hides instead of quitting (the tray icon, when available,
        is the way back in -- see _build_tray_icon -- and the global
        hotkey always still works regardless of whether a tray host is
        present on this desktop). Returning True tells GTK to cancel the
        close; request_exit() is the only path that actually proceeds to
        "destroy" (the tray's own "Exit" item, or SIGTERM/SIGINT)."""
        if self._exit_requested:
            return False
        self.window.hide()
        return True

    def request_exit(self) -> None:
        """Port of MainWindow's RequestExit: the one path that lets a
        close through _on_delete_event instead of hiding."""
        self._exit_requested = True
        self.window.close()

    def toggle_voice_recording_from_hotkey(self) -> None:
        """Port of ToggleVoiceRecordingFromHotkey (Ctrl+Shift+C): same JS
        call, targeted at whichever tab is currently active."""
        log("hotkey: toggle voice recording (Ctrl+Shift+C)")
        if self.active_tab and self.active_tab.webview:
            self.active_tab.webview.run_javascript(
                "window.carolineToggleVoiceRecording && window.carolineToggleVoiceRecording();", None, None, None,
            )

    def _next_tab_id(self) -> str:
        for i in range(1, MAX_TABS + 1):
            candidate = str(i)
            if not any(t.id == candidate for t in self.tabs):
                return candidate
        raise RuntimeError("no free tab id")

    def _make_webview(self, tab: ChatTab) -> WebKit2.WebView:
        manager = WebKit2.UserContentManager()
        manager.add_script(WebKit2.UserScript(
            SHIM_JS, WebKit2.UserContentInjectedFrames.ALL_FRAMES,
            WebKit2.UserScriptInjectionTime.START, None, None,
        ))
        manager.add_style_sheet(WebKit2.UserStyleSheet(
            PAGE_CSS, WebKit2.UserContentInjectedFrames.ALL_FRAMES,
            WebKit2.UserStyleLevel.USER, None, None,
        ))
        manager.register_script_message_handler("caroline")
        manager.connect("script-message-received::caroline", lambda m, r: self.on_page_message(tab, r))
        return WebKit2.WebView(user_content_manager=manager)

    def add_tab(self, tab_id: str, select: bool) -> None:
        if len(self.tabs) >= MAX_TABS:
            log(f"add_tab: refused, already at MAX_TABS ({MAX_TABS})")
            return
        saved_name = self.settings.tab_names.get(tab_id)
        tab = ChatTab(tab_id, saved_name or f"Tab {tab_id}")
        tab.webview = self._make_webview(tab)
        tab.header_button.connect("clicked", lambda _b, t=tab: self.select_tab(t))
        tab.header_button.connect("button-press-event", lambda _b, e, t=tab: self._maybe_rename(t, e))
        self._build_tab_menu(tab)

        self.content_stack.add_named(tab.webview, tab.id)
        self.tabs.append(tab)
        self.rebuild_tab_strip()
        self._persist_open_tab_ids()

        url = (
            f"http://127.0.0.1:{PAGE_PORT}/chat.html"
            f"?port={BACKEND_PORT}&tab={urllib.parse.quote(tab.id)}"
            f"&alwaysOnTop={'1' if self.settings.always_on_top else '0'}&assetsVersion={_assets_version()}"
        )
        tab.webview.load_uri(url)
        tab.webview.show_all()
        if select:
            self.select_tab(tab)

    def _build_tab_menu(self, tab: ChatTab) -> None:
        menu = Gtk.Menu()
        for mode_id, label in MODE_LABELS:
            item = Gtk.CheckMenuItem(label=label)
            item.connect("toggled", lambda it, t=tab, m=mode_id: self._on_mode_toggled(t, m, it))
            tab.mode_items[mode_id] = item
            menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        clear_item = Gtk.MenuItem(label="Clear")
        clear_item.connect("activate", lambda _i, t=tab: self._run_js(t, "window.carolineRequestClear && window.carolineRequestClear();"))
        menu.append(clear_item)
        close_item = Gtk.MenuItem(label="Close")
        close_item.connect("activate", lambda _i, t=tab: self.close_tab(t))
        tab.close_item = close_item
        menu.append(close_item)
        menu.show_all()
        tab.menu_button.set_popup(menu)
        self._apply_tab_mode_style(tab)

    def _on_mode_toggled(self, tab: ChatTab, mode_id: str, item: Gtk.CheckMenuItem) -> None:
        if not item.get_active():
            return
        if tab.chat_mode == mode_id or not tab.mode_available.get(mode_id):
            return
        self._run_js(tab, f"window.carolineSetChatMode && window.carolineSetChatMode({json.dumps(mode_id)});")

    def _apply_tab_mode_style(self, tab: ChatTab) -> None:
        # _on_mode_toggled itself no-ops when chat_mode already matches, so
        # set_active() here re-entering it during a restyle is harmless --
        # simpler than blocking/unblocking the signal handler.
        for mode_id, item in tab.mode_items.items():
            available = tab.mode_available.get(mode_id, False)
            item.set_sensitive(available)
            item.set_active(tab.chat_mode == mode_id)
        if tab.close_item:
            tab.close_item.set_sensitive(len(self.tabs) > 1)

    def _run_js(self, tab: ChatTab, script: str) -> None:
        if tab.webview is not None:
            tab.webview.run_javascript(script, None, None, None)

    def _maybe_rename(self, tab: ChatTab, event) -> bool:
        if event.type != Gdk.EventType._2BUTTON_PRESS:
            return False
        self._begin_rename(tab)
        return True

    def _begin_rename(self, tab: ChatTab) -> None:
        entry = Gtk.Entry()
        entry.set_text(tab.name)

        def commit(save: bool) -> None:
            tab.header_box.remove(entry)
            tab.header_box.pack_start(tab.header_button, False, False, 0)
            tab.header_box.reorder_child(tab.header_button, 0)
            tab.header_button.show()
            if save:
                new_name = entry.get_text().strip()
                if new_name and new_name != tab.name:
                    tab.name = new_name
                    tab.header_button.set_label(new_name)
                    log(f"tab {tab.id} renamed to {new_name!r}")
                    self.settings.tab_names[tab.id] = new_name
                    self.settings_service.save(self.settings, caller="_begin_rename")

        def on_key(_w, event) -> bool:
            if event.keyval == Gdk.KEY_Return:
                commit(True)
                return True
            if event.keyval == Gdk.KEY_Escape:
                commit(False)
                return True
            return False

        entry.connect("key-press-event", on_key)
        entry.connect("focus-out-event", lambda *_: (commit(True), False)[1])
        tab.header_box.remove(tab.header_button)
        entry.show()
        tab.header_box.pack_start(entry, False, False, 0)
        tab.header_box.reorder_child(entry, 0)
        entry.grab_focus()
        entry.select_region(0, -1)

    def rebuild_tab_strip(self) -> None:
        for child in self.tab_strip.get_children():
            self.tab_strip.remove(child)
        for tab in self.tabs:
            ctx = tab.header_button.get_style_context()
            menu_ctx = tab.menu_button.get_style_context()
            if tab is self.active_tab:
                ctx.add_class("active")
                menu_ctx.add_class("active")
            else:
                ctx.remove_class("active")
                menu_ctx.remove_class("active")
            self.tab_strip.pack_start(tab.header_box, False, False, 1)
            if tab.close_item:
                tab.close_item.set_sensitive(len(self.tabs) > 1)
        self.add_tab_button.set_sensitive(len(self.tabs) < MAX_TABS)
        self.add_tab_button.set_tooltip_text("New tab" if len(self.tabs) < MAX_TABS else f"Maximum {MAX_TABS} tabs")
        self.tab_strip.pack_start(self.add_tab_button, False, False, 0)
        self.tab_strip.show_all()

    def select_tab(self, tab: ChatTab) -> None:
        self.active_tab = tab
        self.content_stack.set_visible_child_name(tab.id)
        self.rebuild_tab_strip()

    def close_tab(self, tab: ChatTab) -> None:
        if len(self.tabs) <= 1:
            return
        log(f"closing tab {tab.id}")
        self.tabs.remove(tab)
        if tab.webview is not None:
            self.content_stack.remove(tab.webview)
        if self.active_tab is tab:
            self.select_tab(self.tabs[0])
        else:
            self.rebuild_tab_strip()
        self._persist_open_tab_ids()

    def on_page_message(self, tab: ChatTab, js_result) -> None:
        if not _is_trusted_source(tab.webview.get_uri()):
            log(f"[page:{tab.id}] on_page_message rejected untrusted source={tab.webview.get_uri()!r}")
            return
        try:
            msg = json.loads(js_result.get_js_value().to_string())
        except Exception as exc:
            log(f"page message unparseable: {exc}")
            return
        kind = msg.get("type")
        if kind == "client_log":
            log(f"[page:{tab.id}] {msg.get('message', '')}")
        elif kind == "set_always_on_top":
            value = bool(msg.get("value"))
            self.window.set_keep_above(value)
            self.settings.always_on_top = value
            self.settings_service.save(self.settings, caller="set_always_on_top")
        elif kind == "tab_state":
            mode = msg.get("mode")
            if isinstance(mode, str):
                tab.chat_mode = mode
            available = msg.get("available")
            if isinstance(available, dict):
                for mode_id in tab.mode_available:
                    tab.mode_available[mode_id] = bool(available.get(mode_id))
            self._apply_tab_mode_style(tab)
        elif kind in ("open_editor", "open_office_editor", "close_editor"):
            self._on_editor_message(tab, msg)
        elif kind == "open_login":
            self._open_login_viewer(tab, msg)
        elif kind == "open_payment":
            self._open_payment_viewer(tab, msg)
        elif kind == "visual_speech_audio":
            # Visual Mode (XcfaRenderer-based talking-head rendering) isn't
            # ported -- that library is a vendored C# ProjectReference with
            # no bridge from this Python shell, a separate, large undertaking
            # (see docs/LINUX_PORT_PLAN.md's Phase 4 section). chat.js
            # already treats played=false as "fall back to plain audio" for
            # the exact same reason on Windows (a not-yet-warmed model) --
            # replying with it immediately here keeps every voice reply
            # audible instead of chat.js waiting forever for a
            # visual_speech_done that would otherwise never arrive.
            self._dispatch(tab, {"type": "visual_speech_done", "requestId": msg.get("requestId"), "played": False})
        elif kind in ("visual_mode_config", "visual_speech_start", "visual_speech_stop", "visual_speech_cancel"):
            pass  # no native reply expected for these on Windows either
        else:
            log(f"[page:{tab.id}] unhandled message type={kind!r} (not implemented in the Linux shell yet)")

    def _on_editor_message(self, tab: ChatTab, msg: dict) -> None:
        path = msg.get("path", "")
        kind = msg.get("type")
        if kind == "close_editor":
            viewer = self.viewer_windows.get(path)
            if viewer:
                viewer.close()
            return
        if kind == "open_office_editor":
            log(f"[page:{tab.id}] open_office_editor not implemented in the Linux shell yet (path={path})")
            return
        request_id = msg.get("requestId", "")
        viewer_key = path or request_id
        viewer_kind = msg.get("kind", "")

        def on_done(result: ViewerResult) -> None:
            self.viewer_windows.pop(viewer_key, None)
            payload = {"type": "editor_result", "requestId": request_id, "outcome": result.outcome, "path": result.path}
            self._dispatch(tab, payload)

        viewer = ViewerWindow(PAGE_PORT, SHIM_JS, PAGE_CSS, on_done=on_done)
        if viewer_kind in ("code", "slideshow"):
            viewer.show_web_page(msg.get("title", ""), viewer_kind, path, msg)
        elif viewer_kind == "image":
            viewer.show_image(path)
        elif viewer_kind == "video":
            viewer.show_video(path)
        else:
            log(f"[page:{tab.id}] open_editor kind={viewer_kind!r} not implemented in the Linux shell yet")
            return
        self.viewer_windows[viewer_key] = viewer
        viewer.show()

    PAYMENT_WINDOW_KEY = "squirrelwisdom-payment"

    def _open_payment_viewer(self, tab: ChatTab, msg: dict) -> None:
        """Port of MainWindow.OnOpenPayment: one payment viewer at a time,
        a bare WebView pointed at the Revolut checkout URL, replying with
        payment_result the moment the window closes for any reason (no
        outcome to distinguish -- Revolut's own webhook is the real source
        of truth for completion, same as Windows)."""
        checkout_url = msg.get("checkoutUrl", "")
        if not checkout_url:
            log(f"[page:{tab.id}] open_payment with no checkoutUrl, ignored")
            return
        existing = self.viewer_windows.get(self.PAYMENT_WINDOW_KEY)
        if existing:
            existing.close()

        def on_done(_result: ViewerResult) -> None:
            self.viewer_windows.pop(self.PAYMENT_WINDOW_KEY, None)
            self._dispatch(tab, {"type": "payment_result"})

        viewer = ViewerWindow(PAGE_PORT, SHIM_JS, PAGE_CSS, on_done=on_done)
        viewer.show_payment(checkout_url)
        self.viewer_windows[self.PAYMENT_WINDOW_KEY] = viewer
        viewer.show()

    LOGIN_WINDOW_KEY = "squirrelwisdom-login"

    def _open_login_viewer(self, tab: ChatTab, msg: dict) -> None:
        request_id = msg.get("requestId", "")
        existing = self.viewer_windows.get(self.LOGIN_WINDOW_KEY)
        if existing:
            existing.close()

        def on_login_done(email, password, cancelled, is_register, open_settings_instead):
            self.viewer_windows.pop(self.LOGIN_WINDOW_KEY, None)
            payload = {
                "type": "login_result", "requestId": request_id, "email": email,
                "password": password, "cancelled": cancelled, "isRegister": is_register,
            }
            self._dispatch(tab, payload)
            if open_settings_instead and self.active_tab and self.active_tab.webview:
                self.active_tab.webview.run_javascript(
                    "window.carolineOpenSettings && window.carolineOpenSettings();", None, None, None,
                )

        viewer = ViewerWindow(PAGE_PORT, SHIM_JS, PAGE_CSS, on_login_done=on_login_done)
        viewer.show_login(msg.get("error"), bool(msg.get("noAiAtAll")))
        self.viewer_windows[self.LOGIN_WINDOW_KEY] = viewer
        viewer.show()

    def _dispatch(self, tab: ChatTab, payload: dict) -> None:
        if tab.webview is not None:
            tab.webview.run_javascript(
                f"window.chrome.webview.__dispatch({json.dumps(json.dumps(payload))});", None, None, None,
            )

    def on_destroy(self, _widget) -> None:
        """Port of MainWindow's Cleanup: persists window geometry before
        tearing down. Querying position/size here (GtkWidget::destroy,
        not after the widget is actually gone) is the same pattern
        wait_for_backend_then_show etc. already rely on GLib callbacks
        for -- the window is still a valid, queryable widget at this
        point in the signal."""
        try:
            left, top = self.window.get_position()
            width, height = self.window.get_size()
            self.settings.window_left, self.settings.window_top = float(left), float(top)
            self.settings.window_width, self.settings.window_height = float(width), float(height)
            self.settings_service.save(self.settings, caller="on_destroy")
        except Exception as exc:
            log(f"on_destroy: failed to persist window geometry: {exc}")
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception as exc:
                log(f"on_destroy: tray_icon.stop() failed: {exc}")
        self.supervisor.stop()
        Gtk.main_quit()


def acquire_single_instance_lock() -> object | None:
    """Analog of the WPF App.xaml.cs's named Mutex -- flock on a file under
    the XDG data dir. Returns the open file (keep a reference alive for the
    process's lifetime, or the lock releases) or None if another instance
    already holds it. Unlike a Windows named Mutex, the kernel releases an
    flock automatically if this process dies without closing it (crash,
    SIGKILL), so no stale-lock cleanup is needed -- same effective behavior
    as a Windows abandoned-mutex, just via a different OS mechanism."""
    import fcntl

    xdg_data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    lock_dir = Path(xdg_data_home) / "caroline"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "caroline.lock"
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def show_already_running_dialog() -> None:
    dialog = Gtk.MessageDialog(
        message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.OK,
        text="Caroline is already running.",
    )
    dialog.set_title("Caroline")
    dialog.run()
    dialog.destroy()


def show_error_dialog(summary: str, detail: str) -> None:
    dialog = Gtk.MessageDialog(
        message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK, text=summary,
    )
    dialog.format_secondary_text(detail)
    dialog.set_title("Caroline")
    dialog.run()
    dialog.destroy()


def _build_tray_icon(shell: Shell) -> Any:
    """Port of Tray/TrayIconManager.cs: "Open Caroline" (also the default/
    primary-click action) + Exit, same two-item menu. No single Linux API
    covers every desktop the way Win32's NotifyIcon does (GNOME shows
    nothing at all without the user's own AppIndicator extension, see
    docs/LINUX_PORT_PLAN.md) -- pystray (vendored, see vendor/pystray/
    NOTICE.md) already tries AppIndicator, then GtkStatusIcon, then a raw
    X11 icon window, in that order, so this gets the best available
    option on whatever desktop it's running on without this file needing
    to know which one that is.

    Best-effort: returns None (logged, not fatal -- the window is still
    fully usable, just without a tray icon or a way to un-hide it besides
    the global hotkey) if pystray or Pillow aren't actually importable, or
    if construction fails for any other reason."""
    try:
        import pystray
        from PIL import Image
    except ImportError as exc:
        log(f"tray: pystray/Pillow not importable, tray disabled: {exc}")
        return None
    try:
        image = Image.open(APP_ICON_PATH)
        image.size = (64, 64)
        image.load()
    except Exception as exc:
        log(f"tray: could not load icon from {APP_ICON_PATH}, tray disabled: {exc}")
        return None

    def on_open(_icon: Any = None, _item: Any = None) -> None:
        GLib.idle_add(shell.show_and_activate)

    def on_exit(_icon: Any = None, _item: Any = None) -> None:
        GLib.idle_add(shell.request_exit)

    menu = pystray.Menu(
        pystray.MenuItem("Open Caroline", on_open, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", on_exit),
    )
    try:
        return pystray.Icon("caroline", image, "Caroline", menu)
    except Exception as exc:
        log(f"tray: pystray.Icon construction failed, tray disabled: {exc}")
        return None


def main() -> int:
    _install_global_exception_logging()
    GLib.set_prgname("caroline")
    lock = acquire_single_instance_lock()
    if lock is None:
        log("another instance is already running -- exiting")
        show_already_running_dialog()
        return 1

    supervisor = Supervisor()
    if not supervisor.start():
        # Confirmed live (2026-10-05, AppImage test): this used to exit with
        # nothing visible at all -- no window, no terminal output (log() only
        # writes to the log file) -- when the bundled runtime/python was
        # missing. A packaged app failing this early needs to actually tell
        # the person running it something, not just leave them looking at
        # nothing with no clue where to even look.
        show_error_dialog(
            "Caroline couldn't start",
            f"The bundled Python runtime was not found at {PYTHON}.\n\nSee {LOG_PATH} for details.",
        )
        return 1
    serve_wwwroot()
    settings_service = SettingsService(log=log)
    settings = settings_service.load()
    shell = Shell(supervisor, settings, settings_service)
    shell.prepare()
    shell.tray_icon = _build_tray_icon(shell)
    if shell.tray_icon is not None:
        # Per pystray's own docs: call run_detached() BEFORE entering the
        # other library's main loop (Gtk.main(), at the end of this
        # function) so its Linux backends integrate with that loop
        # instead of spinning up their own.
        shell.tray_icon.run_detached()

    splash = SplashWindow(on_dismissed=lambda: None)
    splash.show()
    wait_for_backend_then_show(supervisor, splash, shell.reveal)

    hotkeys = GlobalHotkeyListener()
    if not hotkeys.register(settings.hotkey_modifiers, settings.hotkey_key, shell.toggle_visibility):
        log("[hotkey] registration failed -- already in use, or this keyboard layout has no matching keycode")
    if not hotkeys.register(MOD_CONTROL | MOD_SHIFT, "c", shell.toggle_voice_recording_from_hotkey):
        log("[hotkey] voice-record hotkey registration failed -- already in use by another app?")
    hotkeys.start()

    signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(shell.on_destroy, None))
    signal.signal(signal.SIGINT, lambda *_: GLib.idle_add(shell.on_destroy, None))
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
