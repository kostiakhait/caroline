"""Port of Windows/Caroline/DocumentViewerWindow.xaml(.cs) -- Caroline's
floating window for showing a photo/video or editing code/a slideshow,
opened by chat.js's "open_editor"/"open_office_editor" messages (see
caroline_shell.py's on_page_message).

Ported here: image, video, code/slideshow (via the same wwwroot pages the
WPF build already uses, monaco_viewer.html/slideshow.html, reusing the
local HTTP server caroline_shell.py already runs), and the SquirrelWisdom
login form. NOT ported: the OnlyOffice document editor ("office" kind) and
the payment checkout viewer -- both depend on backend features (a real
SquirrelWisdom/OnlyOffice session, a Revolut checkout URL) that can't be
exercised without a live backend, and are a real, separate chunk of work
on top of this, not a smaller version of it.

Verified live on the test box: Monaco shows the file with syntax highlighting
once the page has finished loading, image and video render (video needs the
GStreamer plugins listed in docs/LINUX_PORT_PLAN.md), slideshow pages through
its images, and the login form renders and routes Cancel/X through the same
finish path as a real Cancel click.
"""

from __future__ import annotations

import json
import shutil
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2  # noqa: E402

HEADER_CSS = b"""
#viewer-header { background-color: #3A3F8F; }
#viewer-header label { color: white; }
#viewer-header button { background: #4169E1; color: white; border: none; border-radius: 0; box-shadow: none; }
#viewer-header button:hover { background: #5A7FEA; }
#viewer-content { background-color: #222222; }
#login-label, #login-check label { color: #CCCCCC; }
#login-subtitle { color: #AAAAAA; }
#login-error { color: #FF8080; }
"""


@dataclass
class ViewerResult:
    outcome: str  # "saved" | "cancelled" | "closed" | "error"
    path: str | None


class ViewerWindow:
    def __init__(
        self,
        page_port: int,
        shim_js: str,
        font_css: str,
        on_done: Callable[[ViewerResult], None] | None = None,
        on_login_done: Callable[[str | None, str | None, bool, bool, bool], None] | None = None,
    ) -> None:
        self._page_port = page_port
        self._shim_js = shim_js
        self._font_css = font_css
        self._on_done = on_done
        self._on_login_done = on_login_done
        self._result_reported = False
        self._path = ""
        self._kind = ""

        self.window = Gtk.Window(title="Caroline Viewer")
        self.window.set_default_size(900, 700)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        self.window.connect("delete-event", self._on_close_request)

        css = Gtk.CssProvider()
        css.load_from_data(HEADER_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            self.window.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_name("viewer-header")
        self.title_label = Gtk.Label(label="")
        self.title_label.set_halign(Gtk.Align.START)
        self.title_label.set_selectable(False)
        self.title_label.set_can_focus(False)
        self.title_label.set_margin_top(6)
        self.title_label.set_margin_bottom(6)
        self.title_label.set_margin_start(12)
        header.pack_start(self.title_label, True, True, 0)
        self.button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.button_box.set_margin_top(4)
        self.button_box.set_margin_bottom(4)
        self.button_box.set_margin_end(8)
        header.pack_end(self.button_box, False, False, 0)
        root.pack_start(header, False, False, 0)

        self.content = Gtk.Box()
        self.content.set_name("viewer-content")
        root.pack_start(self.content, True, True, 0)
        self.window.add(root)

    # -- construction helpers, one per DocumentViewerWindow constructor overload --

    def show_image(self, path: str) -> None:
        self._path, self._kind = path, "image"
        self.title_label.set_label(Path(path).name)
        self._add_close_button()
        self._load_html(f'<img src="file://{urllib.parse.quote(path)}" style="max-width:100%;max-height:100vh;display:block;margin:auto;">')

    def show_video(self, path: str) -> None:
        self._path, self._kind = path, "video"
        self.title_label.set_label(Path(path).name)
        self._add_close_button()
        self._load_html(f'<video src="file://{urllib.parse.quote(path)}" controls autoplay style="max-width:100%;max-height:100vh;display:block;margin:auto;"></video>')

    def show_web_page(self, title: str, kind: str, path: str, payload: dict) -> None:
        """code/slideshow: loads the SAME wwwroot page the WPF build uses
        (monaco_viewer.html/slideshow.html) from the local HTTP server, and
        dispatches the payload via the shim's __dispatch once it's loaded --
        the same "message" event those pages already listen for."""
        self._path, self._kind = path, kind
        self.title_label.set_label(title or (Path(path).name if path else kind))
        self._add_close_button()
        page = "monaco_viewer.html" if kind == "code" else "slideshow.html"
        webview = self._make_webview()
        self.content.pack_start(webview, True, True, 0)

        def on_load_changed(wv, event):
            if event == WebKit2.LoadEvent.FINISHED:
                wv.run_javascript(
                    f"window.chrome.webview.__dispatch({json.dumps(json.dumps(payload))});", None, None, None,
                )

        webview.connect("load-changed", on_load_changed)
        webview.load_uri(f"http://127.0.0.1:{self._page_port}/{page}")
        webview.show_all()

    def show_login(self, error: str | None, no_ai_at_all: bool) -> None:
        self._kind = "login"
        self.title_label.set_label("Log in to SquirrelWisdom")

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        form.set_halign(Gtk.Align.CENTER)
        form.set_valign(Gtk.Align.CENTER)
        form.set_size_request(320, -1)

        if no_ai_at_all:
            subtitle = Gtk.Label(
                label="Caroline can't talk to you right now -- log in or register with "
                      "SquirrelWisdom below, or use your own Claude account instead.",
            )
            subtitle.set_line_wrap(True)
            subtitle.set_name("login-subtitle")
            form.pack_start(subtitle, False, False, 0)

        email_label = Gtk.Label(label="Email", halign=Gtk.Align.START)
        email_label.set_name("login-label")
        form.pack_start(email_label, False, False, 0)
        email_entry = Gtk.Entry()
        form.pack_start(email_entry, False, False, 0)
        password_label = Gtk.Label(label="Password", halign=Gtk.Align.START)
        password_label.set_name("login-label")
        form.pack_start(password_label, False, False, 0)
        password_entry = Gtk.Entry(visibility=False)
        form.pack_start(password_entry, False, False, 0)
        register_check = Gtk.CheckButton(label="I don't have an account -- register instead")
        register_check.set_name("login-check")
        form.pack_start(register_check, False, False, 0)

        error_label = Gtk.Label(label=error or "")
        error_label.set_line_wrap(True)
        error_label.set_name("login-error")
        if error:
            form.pack_start(error_label, False, False, 0)

        login_button = Gtk.Button(label="Log In")
        cancel_button = Gtk.Button(label="Cancel")
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.pack_start(login_button, True, True, 0)
        btn_row.pack_start(cancel_button, True, True, 0)
        form.pack_start(btn_row, False, False, 0)

        if no_ai_at_all:
            settings_button = Gtk.Button(label="I have my own Claude account")
            form.pack_start(settings_button, False, False, 0)
            settings_button.connect("clicked", lambda _b: self._finish_login(None, None, True, False, True))

        def do_login(_b):
            email, password = email_entry.get_text().strip(), password_entry.get_text()
            if not email or not password:
                error_label.set_label("Enter both email and password.")
                if error_label.get_parent() is None:
                    form.pack_start(error_label, False, False, 0)
                    error_label.show()
                return
            self._finish_login(email, password, False, register_check.get_active(), False)

        login_button.connect("clicked", do_login)
        register_check.connect("toggled", lambda cb: login_button.set_label(
            "Register" if cb.get_active() else "Log In",
        ))
        cancel_button.connect("clicked", lambda _b: self._finish_login(None, None, True, False, False))

        self.content.pack_start(form, True, True, 0)
        form.show_all()

    # -- shared plumbing --

    def _make_webview(self) -> WebKit2.WebView:
        manager = WebKit2.UserContentManager()
        manager.add_script(WebKit2.UserScript(
            self._shim_js, WebKit2.UserContentInjectedFrames.ALL_FRAMES,
            WebKit2.UserScriptInjectionTime.START, None, None,
        ))
        manager.add_style_sheet(WebKit2.UserStyleSheet(
            self._font_css, WebKit2.UserContentInjectedFrames.ALL_FRAMES,
            WebKit2.UserStyleLevel.USER, None, None,
        ))
        manager.register_script_message_handler("caroline")
        manager.connect("script-message-received::caroline", self._on_page_message)
        return WebKit2.WebView(user_content_manager=manager)

    def _on_page_message(self, _manager, js_result) -> None:
        try:
            msg = json.loads(js_result.get_js_value().to_string())
        except Exception:
            return
        if msg.get("type") != "save":
            return
        if not self._path:
            return
        content = msg.get("content", "")
        try:
            p = Path(self._path)
            if p.exists():
                shutil.copy(p, p.with_suffix(p.suffix + ".bak"))
            p.write_text(content, encoding="utf-8")
        except OSError:
            pass

    def _load_html(self, body_html: str) -> None:
        webview = self._make_webview()
        self.content.pack_start(webview, True, True, 0)
        base_uri = Path(self._path).parent.as_uri() + "/"
        webview.load_html(f"<html><body style='margin:0;background:#222'>{body_html}</body></html>", base_uri)
        webview.show_all()

    def _add_close_button(self) -> None:
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda _b: self._report("closed"))
        self.button_box.pack_start(close_button, False, False, 0)

    def _finish_login(self, email, password, cancelled, is_register, open_settings_instead) -> None:
        if self._result_reported:
            return
        self._result_reported = True
        if self._on_login_done:
            self._on_login_done(email, password, cancelled, is_register, open_settings_instead)
        self.window.destroy()

    def _report(self, outcome: str) -> None:
        if self._result_reported:
            return
        self._result_reported = True
        if self._on_done:
            self._on_done(ViewerResult(outcome, self._path or None))
        self.window.destroy()

    def _on_close_request(self, *_args) -> bool:
        if self._kind == "login":
            self._finish_login(None, None, True, False, False)
        else:
            # Closed via the window's own X button, same as the WPF
            # OnClosing handler: an office session would count as "saved"
            # here (not ported, see module docstring), everything else as
            # "closed".
            self._report("closed")
        return False

    def show(self) -> None:
        self.window.show_all()

    def close(self) -> None:
        """Programmatic close -- mirrors close_editor's existing.Close(),
        which raises the same OnClosing path a user's own X-button click
        would, so route through the identical kind-based dispatch."""
        self._on_close_request()
