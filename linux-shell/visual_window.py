"""The window Visual Mode plays its talking-head clips in on Linux (2026-10-24): port of the
behaviour of Windows/Caroline/VisualModeWindow.xaml.cs -- borderless, transparent, above the
other windows, it stays up between the remarks of a dialogue and closes when the user presses
Stop (or when a clip played from a message's speaker button has ended).

A clip is the .webm the renderer made (VP9 with an alpha channel, Opus audio muxed in); a
WebKit <video> plays it, picture and sound together. Playback is reported through a script
message: "ended" (the clip played to its end), "error" (it could not be played), "stopped"
(the user pressed Stop). visual_bridge.py turns those into chat.js's visual_speech_done."""

from __future__ import annotations

import json
import urllib.parse
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: E402

WIDTH, HEIGHT = 320, 480

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:transparent;overflow:hidden;width:100%;height:100%}
/* Sized by the window, never in fixed pixels: on a screen scaled to 125% the page sees a 256x384 window, and layers
   of 320x480 CSS pixels showed the figure enlarged and cut off at the knees (seen 2026-10-24). */
.layer{position:absolute;left:0;top:0;width:100%;height:100%;object-fit:contain;background:transparent}
#stop{position:fixed;right:6px;top:6px;border:0;border-radius:12px;padding:3px 10px;font:12px sans-serif;
      background:rgba(0,0,0,.55);color:#fff;cursor:pointer;opacity:0;transition:opacity .15s;z-index:5}
body:hover #stop{opacity:1}
</style></head><body>
<img id="rest" class="layer" style="visibility:hidden" alt="">
<video id="v0" class="layer" playsinline preload="auto" style="visibility:hidden"></video>
<video id="v1" class="layer" playsinline preload="auto" style="visibility:hidden"></video>
<button id="stop">Stop</button>
<script>
// Two video elements of a fixed size, taking turns (2026-10-24): a clip is loaded into the one that is hidden and shown
// only once its first frame has really been painted -- one element changing its source was drawn for about 0.4 s as a
// tiny squashed figure (the default 300x150 box) before it grew to its size. Between clips the model's rest pose
// stands in the window (mouth closed), as in the Windows window; the frozen last frame of a clip is a mouth open.
const vids=[document.getElementById('v0'),document.getElementById('v1')];
const rest=document.getElementById('rest');
let cur=-1, hasRest=false, token=0;
function post(kind,extra){window.webkit.messageHandlers.visual.postMessage(Object.assign({kind:kind},extra||{}));}
function setRest(src){rest.onload=function(){hasRest=true; if(cur<0||vids[cur].style.visibility==='hidden') rest.style.visibility='visible';}; rest.src=src;}
function showRest(){
  if(hasRest){rest.style.visibility='visible'; vids.forEach(function(v){v.style.visibility='hidden';});}
}
vids.forEach(function(v,i){
  v.addEventListener('ended',function(){ if(i===cur){showRest(); post('ended');} });
  v.addEventListener('error',function(){ if(i===cur||cur<0){post('error',{code:v.error&&v.error.code,message:v.error&&v.error.message});} });
});
document.getElementById('stop').onclick=function(){ if(cur>=0){vids[cur].pause();} showRest(); post('stopped'); };
function playClip(src){
  token++; const mine=token;
  const next=(cur+1)%2, v=vids[next], old=cur>=0?vids[cur]:null;
  cur=next;
  v.style.visibility='hidden';
  v.src=src; v.load();
  let shown=false;
  const reveal=function(){
    if(shown||mine!==token) return; shown=true;
    v.style.visibility='visible'; rest.style.visibility='hidden';
    if(old){old.pause(); old.style.visibility='hidden';}
    post('playing');
  };
  if(v.requestVideoFrameCallback){ v.requestVideoFrameCallback(function(){ requestAnimationFrame(function(){ requestAnimationFrame(reveal); }); }); }
  else { v.addEventListener('playing',function(){ setTimeout(reveal,80); },{once:true}); }
  v.play().catch(function(e){post('error',{message:String(e)});});
}
function stopClip(){ if(cur>=0){vids[cur].pause();} showRest(); }
</script></body></html>"""


class VisualWindow:
    def __init__(self, log: Callable[[str], None]) -> None:
        self._log = log
        self._on_event: Callable[[str, dict], None] | None = None
        self.window = Gtk.Window(title="Caroline animation")
        self.window.set_decorated(False)
        self.window.set_keep_above(True)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_default_size(WIDTH, HEIGHT)
        self.window.set_accept_focus(False)
        screen = self.window.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.window.set_visual(visual)
        self.window.set_app_paintable(True)
        manager = WebKit2.UserContentManager()
        manager.register_script_message_handler("visual")
        manager.connect("script-message-received::visual", self._on_message)
        self.webview = WebKit2.WebView(user_content_manager=manager)
        self.webview.set_background_color(Gdk.RGBA(0, 0, 0, 0))
        settings = self.webview.get_settings()
        settings.set_property("media-playback-requires-user-gesture", False)
        settings.set_property("allow-file-access-from-file-urls", True)
        settings.set_property("enable-media-stream", False)
        self._loaded = False
        self.webview.connect("load-changed", self._on_load)
        self._pending: str | None = None
        self.webview.load_html(_PAGE, "file:///")
        self.window.add(self.webview)
        self.window.connect("delete-event", self._on_delete)

    # -- control ---------------------------------------------------------------
    def play(self, path: str, on_event: Callable[[str, dict], None], chat_rect: tuple[int, int, int, int] | None = None) -> None:
        """Shows the window if it is not up and plays the clip at `path`. `chat_rect` (x, y, width,
        height): where the chat window is -- the animation window is put beside it, never over it."""
        self._on_event = on_event
        self.window.show_all()
        self._place(chat_rect)
        src = "file://" + urllib.parse.quote(path)
        if self._loaded:
            self.webview.run_javascript(f"playClip({json.dumps(src)});", None, None, None)
        else:
            self._pending = src

    def set_rest(self, png_path: str) -> None:
        """The picture that stands in the window between clips: the model's rest pose."""
        src = "file://" + urllib.parse.quote(png_path)
        self._rest_src = src
        if self._loaded:
            self.webview.run_javascript(f"setRest({json.dumps(src)});", None, None, None)

    def stop_playback(self) -> None:
        if self._loaded:
            self.webview.run_javascript("stopClip();", None, None, None)

    def hide(self) -> None:
        self.stop_playback()
        self.window.hide()

    def is_visible(self) -> bool:
        return bool(self.window.get_visible())

    # -- internals ---------------------------------------------------------------
    def _place(self, chat_rect: tuple[int, int, int, int] | None) -> None:
        """Beside the chat window, never over it (2026-10-24, explicit instruction): to its left when
        there is room for the whole window there, else to its right, else in the screen's bottom right
        corner. Bottom edges aligned. Done on every clip, since the chat window can have been moved;
        the window manager is asked after the window is on the screen, where it takes the request."""
        try:
            display = Gdk.Display.get_default()
            try:
                monitor = display.get_monitor_at_point(*(chat_rect[:2] if chat_rect else (0, 0))) or display.get_primary_monitor() or display.get_monitor(0)
                area = monitor.get_workarea()
                left, top, right, bottom = area.x, area.y, area.x + area.width, area.y + area.height
            except Exception:  # noqa: BLE001 -- no monitor information: the whole screen
                screen = self.window.get_screen()
                left, top, right, bottom = 0, 0, screen.get_width(), screen.get_height()
            gap = 12
            if chat_rect is None:
                x, y = right - WIDTH - 24, bottom - HEIGHT - 24
            else:
                cx, cy, cw, ch = chat_rect
                if cx - gap - WIDTH >= left:
                    x = cx - gap - WIDTH
                elif cx + cw + gap + WIDTH <= right:
                    x = cx + cw + gap
                else:
                    x = right - WIDTH - 24
                y = min(max(cy + ch - HEIGHT, top), bottom - HEIGHT)
            self._target = (int(x), int(y))
            self.window.move(*self._target)
            GLib.timeout_add(150, self._move_again)
        except Exception as exc:  # noqa: BLE001 -- the window manager places it
            self._log(f"visual window: not placed: {exc}")

    def _move_again(self) -> bool:
        target = getattr(self, "_target", None)
        if target is not None and self.window.get_visible():
            self.window.move(*target)
        return False

    def _on_load(self, _view, event) -> None:
        if event == WebKit2.LoadEvent.FINISHED:
            self._loaded = True
            if getattr(self, "_rest_src", None):
                self.webview.run_javascript(f"setRest({json.dumps(self._rest_src)});", None, None, None)
            if self._pending is not None:
                src, self._pending = self._pending, None
                self.webview.run_javascript(f"playClip({json.dumps(src)});", None, None, None)

    def _on_message(self, _manager, result) -> None:
        try:
            # WebKit2 4.1 hands the page's message over as a JSCValue; older ones wrap it in a result.
            value = result.get_js_value() if hasattr(result, "get_js_value") else result
            payload = json.loads(value.to_json(0))
        except Exception as exc:  # noqa: BLE001
            self._log(f"visual window: unreadable page message: {exc}")
            return
        kind = payload.get("kind", "")
        self._log(f"visual window: {kind} {payload.get('message') or ''}".strip())
        if self._on_event is not None and kind in ("ended", "error", "stopped"):
            self._on_event(kind, payload)

    def _on_delete(self, *_args) -> bool:
        # Closing the window is a Stop: reported, then hidden (the window is reused).
        if self._on_event is not None:
            self._on_event("stopped", {})
        self.window.hide()
        return True
