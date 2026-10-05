"""Caroline's Linux desktop shell: a GTK window hosting the existing chat page
(Windows/Caroline/wwwroot) in WebKit2, plus the supervisor/backend lifecycle.

Replaces the WPF shell's job on Linux (see docs/LINUX_PORT_PLAN.md, Phase 4).
The page talks to the native host through window.chrome.webview.postMessage
(a WebView2 API), so a small document-start shim maps that call onto a WebKit
script message handler. Messages the page sends are handled here; anything not
handled yet is logged, so gaps show up in the log instead of failing silently.
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
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

HERE = Path(__file__).resolve().parent
APP_ROOT = Path(os.environ.get("CAROLINE_APP_ROOT", HERE.parent))
WWWROOT = Path(os.environ.get("CAROLINE_WWWROOT", HERE.parent / "Windows" / "Caroline" / "wwwroot"))
SUPERVISOR_PY = Path(os.environ.get("CAROLINE_SUPERVISOR_PY", HERE.parent / "backend-py" / "supervisor.py"))
PYTHON = Path(os.environ.get("CAROLINE_PYTHON", APP_ROOT / "runtime" / "python" / "bin" / "python3"))
PAGE_PORT = 48767
SUPERVISOR_PORT = 48766
LOG_PATH = Path(os.environ.get("CAROLINE_SHELL_LOG", Path.home() / ".local" / "share" / "Caroline" / "shell.log"))

SHIM_JS = """
window.chrome = window.chrome || {};
window.chrome.webview = {
  postMessage: function (obj) {
    window.webkit.messageHandlers.caroline.postMessage(JSON.stringify(obj));
  },
  addEventListener: function () {},
  removeEventListener: function () {}
};
"""


def log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {line}\n")


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
        env["CAROLINE_PORT"] = "48765"
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


def serve_wwwroot() -> http.server.ThreadingHTTPServer:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(WWWROOT))
    handler.log_message = lambda *a, **k: None  # type: ignore[assignment]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PAGE_PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Shell:
    def __init__(self, supervisor: Supervisor) -> None:
        self.supervisor = supervisor
        self.window = Gtk.Window(title="Caroline")
        self.window.set_default_size(420, 760)
        self.window.connect("destroy", self.on_destroy)

        manager = WebKit2.UserContentManager()
        manager.add_script(WebKit2.UserScript(
            SHIM_JS,
            WebKit2.UserContentInjectedFrames.ALL_FRAMES,
            WebKit2.UserScriptInjectionTime.START,
            None, None,
        ))
        manager.register_script_message_handler("caroline")
        manager.connect("script-message-received::caroline", self.on_page_message)

        self.view = WebKit2.WebView(user_content_manager=manager)
        self.window.add(self.view)

    def show(self) -> None:
        self.window.show_all()
        self.view.load_uri(f"http://127.0.0.1:{PAGE_PORT}/chat.html")
        GLib.timeout_add_seconds(5, self.report_status)

    def report_status(self) -> bool:
        log("backend status: " + self.supervisor.status())
        return True

    def on_page_message(self, _manager, js_result) -> None:
        try:
            msg = json.loads(js_result.get_js_value().to_string())
        except Exception as exc:
            log(f"page message unparseable: {exc}")
            return
        kind = msg.get("type")
        if kind == "client_log":
            log(f"[page:{msg.get('tabId', '?')}] {msg.get('message', '')}")
        elif kind == "set_always_on_top":
            self.window.set_keep_above(bool(msg.get("value")))
        else:
            log(f"[page] unhandled message type={kind!r} (not implemented in the Linux shell yet)")

    def on_destroy(self, _widget) -> None:
        self.supervisor.stop()
        Gtk.main_quit()


def main() -> int:
    supervisor = Supervisor()
    if not supervisor.start():
        return 1
    serve_wwwroot()
    shell = Shell(supervisor)
    shell.show()
    signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(shell.on_destroy, None))
    signal.signal(signal.SIGINT, lambda *_: GLib.idle_add(shell.on_destroy, None))
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
