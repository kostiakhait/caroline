"""The Linux shell's update client (linux-shell/update_checker.py, UpdateClient) against a fake backend: what it does for each action of the rule.
Run: python -m pytest test_linux_update_client.py -q"""

import http.server
import json
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "linux-shell"))
from update_checker import UpdateClient  # noqa: E402


class Backend:
    def __init__(self):
        self.status = {"status": "idle"}
        self.posted: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self):
                body = json.dumps(outer.status).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send()

            def do_POST(self):
                outer.posted.append(self.path)
                self._send()

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"


@pytest.fixture
def world(tmp_path):
    backend = Backend()
    appimage = tmp_path / "bin" / "Caroline-x86_64.AppImage"
    appimage.parent.mkdir()
    appimage.write_bytes(b"old")
    verified = tmp_path / "dl" / "Caroline-1.1.0-x86_64.AppImage"
    verified.parent.mkdir()
    verified.write_bytes(b"new-appimage")
    events: list[tuple] = []
    spawned: list[list] = []
    client = UpdateClient(
        backend.base, appimage, lambda m: None,
        on_update_available=lambda v: events.append(("available", v)), on_status_changed=lambda t: events.append(("status", t)),
        on_restart=lambda: events.append(("restart",)), spawn=lambda cmd, **kw: spawned.append(cmd),
    )
    yield backend, client, appimage, verified, events, spawned
    backend.httpd.shutdown()


def ready(backend, verified, action, confirmed=False):
    backend.status = {"status": "ready", "version": "1.1.0", "path": str(verified), "action": action, "confirmed": confirmed}


def test_nothing_ready_means_nothing_happens(world):
    backend, client, appimage, _v, events, spawned = world
    client.poll()
    backend.status = {"status": "available", "action": "none"}
    client.poll()
    assert events == [] and spawned == [] and appimage.read_bytes() == b"old"


def test_progress_goes_to_the_status_bar_once_per_value(world):
    backend, client, *_rest, events, _s = world
    backend.status = {"status": "downloading", "progress": 40}
    client.poll()
    client.poll()
    assert events == [("status", "Caroline: Update ongoing... 40%")]


def test_inactive_installs_by_itself_without_a_dialog(world):
    backend, client, appimage, verified, events, spawned = world
    ready(backend, verified, "install")
    client.poll()
    assert appimage.read_bytes() == b"new-appimage"
    assert os.name == "nt" or appimage.stat().st_mode & 0o111  # Windows has no execute bit; on Linux the new file must be runnable
    assert spawned == [[str(appimage)]] and ("restart",) in events
    assert backend.posted == ["/api/update/installing"]
    assert not list(appimage.parent.glob("*.update-tmp"))


def test_in_use_only_offers_it_and_touches_nothing(world):
    backend, client, appimage, verified, events, spawned = world
    ready(backend, verified, "ask")
    client.poll()
    assert ("available", "1.1.0") in events and any(e[0] == "status" and "installs when you are done" in e[1] for e in events)
    assert appimage.read_bytes() == b"old" and spawned == [] and ("restart",) not in events


def test_the_owners_yes_confirms_and_installs(world):
    backend, client, appimage, verified, events, spawned = world
    ready(backend, verified, "ask")
    client.poll()

    class Confirming(Backend):
        pass

    # the backend turns "ask" into "install" once it hears /api/update/confirm
    original_post = backend.httpd.RequestHandlerClass.do_POST

    def post(handler):
        if handler.path == "/api/update/confirm":
            ready(backend, verified, "install", confirmed=True)
        original_post(handler)

    backend.httpd.RequestHandlerClass.do_POST = post
    client.confirm()
    assert "/api/update/confirm" in backend.posted
    assert appimage.read_bytes() == b"new-appimage" and spawned == [[str(appimage)]]


def test_a_failed_copy_leaves_the_old_appimage_and_tries_again_later(world, monkeypatch):
    backend, client, appimage, verified, events, spawned = world
    ready(backend, verified, "install")
    monkeypatch.setattr("update_checker.shutil.copyfile", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    client.poll()
    assert appimage.read_bytes() == b"old" and spawned == [] and ("restart",) not in events
    assert any(e[0] == "status" and "disk full" in e[1] for e in events)
    monkeypatch.undo()
    client.poll()  # the next poll tries again
    assert appimage.read_bytes() == b"new-appimage"


def test_an_unreachable_backend_is_quiet(tmp_path):
    client = UpdateClient("http://127.0.0.1:9", tmp_path / "a.AppImage", lambda m: None, lambda v: None, lambda t: None, lambda: None)
    client.poll()
