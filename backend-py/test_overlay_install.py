"""The code-layer artifact (app/overlay_install.py) and the start-time update (app/update_cli.py): what is applied, what is refused, the rollback.
Run: python -m pytest test_overlay_install.py -q"""

import base64
import functools
import http.server
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest

from app import overlay_install as oi
from app import release_manifest as rm
from app import update_cli, update_service

SEED = b"\x05" * 32


def make_dist(root: Path, tag: str, extra: dict[str, str] | None = None) -> Path:
    files = {
        "backend-py/app/main.py": f"# main {tag}", "backend-py/app/plugins/new_{0}.py".format(tag): "x",
        "backend-py/run_server.py": f"run {tag}", "backend-py/VERSION": f"{tag}\n",
        "wwwroot/chat.html": f"chat {tag}", "linux-shell/caroline_shell.py": f"shell {tag}",
        "whatsapp-sidecar/index.js": f"wa {tag}", **(extra or {}),
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def test_build_then_apply_replaces_what_is_owned_and_keeps_the_rest(tmp_path):
    old = make_dist(tmp_path / "old", "1.0.0")
    (old / "runtime/python").mkdir(parents=True)
    (old / "runtime/python/python3").write_text("the runtime")
    (old / "whatsapp-sidecar/node_modules").mkdir()
    (old / "whatsapp-sidecar/node_modules/dep.js").write_text("dep")
    new = make_dist(tmp_path / "new", "1.1.0")
    zip_path = tmp_path / "overlay.zip"
    oi.build_overlay(new, zip_path)

    replaced = oi.apply_overlay(zip_path, old)

    assert "backend-py/app" in replaced and "backend-py/VERSION" in replaced
    assert read(old, "backend-py/app/main.py") == "# main 1.1.0" and read(old, "backend-py/VERSION") == "1.1.0\n"
    assert read(old, "wwwroot/chat.html") == "chat 1.1.0" and read(old, "whatsapp-sidecar/index.js") == "wa 1.1.0"
    assert not (old / "backend-py/app/plugins/new_1.0.0.py").exists()  # a module the new version dropped is gone: plugins are found by listing the folder
    assert (old / "backend-py/app/plugins/new_1.1.0.py").exists()
    assert read(old, "runtime/python/python3") == "the runtime" and read(old, "whatsapp-sidecar/node_modules/dep.js") == "dep"
    assert not (old / ".update-staging").exists() and not (old / ".update-previous").exists()


def test_the_overlay_carries_no_caches(tmp_path):
    dist = make_dist(tmp_path / "d", "1", {"backend-py/app/__pycache__/main.cpython-312.pyc": "bytecode"})
    names = oi.build_overlay(dist, tmp_path / "o.zip")
    assert not any("__pycache__" in n for n in names)


def zip_with(path: Path, entries: dict[str, str], link: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in entries.items():
            zf.writestr(name, text)
        if link:
            info = zipfile.ZipInfo(link)
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "../../etc/passwd")
    return path


@pytest.mark.parametrize("bad", [
    {"backend-py/app/main.py": "x", "../evil.py": "x"},
    {"backend-py/app/main.py": "x", "/etc/passwd": "x"},
    {"backend-py/app/main.py": "x", "runtime/python/python3": "x"},          # not owned
    {"backend-py/app/main.py": "x", "wwwroot/../../x": "x"},
    {"wwwroot/chat.html": "x"},                                              # no backend-py/app/main.py
])
def test_a_bad_overlay_is_refused_and_nothing_changes(tmp_path, bad):
    dist = make_dist(tmp_path / "d", "1.0.0")
    with pytest.raises(oi.OverlayError):
        oi.apply_overlay(zip_with(tmp_path / "bad.zip", bad), dist)
    assert read(dist, "backend-py/app/main.py") == "# main 1.0.0" and read(dist, "wwwroot/chat.html") == "chat 1.0.0"
    assert not (tmp_path / "evil.py").exists()


def test_a_link_in_the_overlay_is_refused(tmp_path):
    dist = make_dist(tmp_path / "d", "1.0.0")
    with pytest.raises(oi.OverlayError, match="link"):
        oi.apply_overlay(zip_with(tmp_path / "l.zip", {"backend-py/app/main.py": "x"}, link="wwwroot/link"), dist)


def test_a_failure_in_the_middle_puts_everything_back(tmp_path, monkeypatch):
    dist = make_dist(tmp_path / "d", "1.0.0")
    zip_path = tmp_path / "o.zip"
    oi.build_overlay(make_dist(tmp_path / "n", "1.1.0"), zip_path)
    real_move = oi.shutil.move
    calls = {"n": 0}

    def flaky(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 5:  # after some paths have already been swapped
            raise OSError("disk went away")
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(oi.shutil, "move", flaky)
    with pytest.raises(oi.OverlayError, match="old version is back"):
        oi.apply_overlay(zip_path, dist)
    monkeypatch.undo()
    for rel, text in {"backend-py/app/main.py": "# main 1.0.0", "wwwroot/chat.html": "chat 1.0.0", "linux-shell/caroline_shell.py": "shell 1.0.0",
                      "backend-py/VERSION": "1.0.0\n", "whatsapp-sidecar/index.js": "wa 1.0.0"}.items():
        assert read(dist, rel) == text, rel
    assert not (dist / ".update-previous").exists()


def test_a_crash_in_the_middle_is_repaired_at_the_next_call(tmp_path):
    dist = make_dist(tmp_path / "d", "1.0.0")
    # what a crash leaves: the old wwwroot aside, the new one half in place
    (dist / ".update-previous").mkdir()
    os.replace(dist / "wwwroot", dist / ".update-previous" / "wwwroot")
    (dist / "wwwroot").mkdir()
    (dist / "wwwroot/half.html").write_text("half")
    oi.recover(dist)
    assert read(dist, "wwwroot/chat.html") == "chat 1.0.0" and not (dist / "wwwroot/half.html").exists()
    assert not (dist / ".update-previous").exists()


# ---- the start-time update, end to end against a real local server ----------------------------------------------------------------

class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def publish(www: Path, version: str, overlay: Path):
    name = f"caroline-xopek-{version}.zip"
    (www / name).write_bytes(overlay.read_bytes())
    entry = {"file": name, "sha256": rm.file_sha256(overlay), "size": overlay.stat().st_size}
    body = json.dumps({"version": version, "commit": "abc", "date": "2026-10-30", "artifacts": {"xopek": entry}}).encode()
    (www / "release.json").write_bytes(body)
    (www / "release.json.sig").write_text(base64.b64encode(rm.ed25519_sign(SEED, body)).decode())


@pytest.fixture
def world(tmp_path, monkeypatch):
    www = tmp_path / "www"
    www.mkdir()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(www)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    dist = make_dist(tmp_path / "dist", "1.0.0")
    monkeypatch.setenv("CAROLINE_UPDATE_URL", f"http://127.0.0.1:{httpd.server_port}")
    monkeypatch.setenv("CAROLINE_UPDATE_PLATFORM", "xopek")
    monkeypatch.setenv("CAROLINE_UPDATE_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("CAROLINE_RELEASE_PUBLIC_KEY", rm.ed25519_public_key(SEED).hex())
    monkeypatch.setattr(update_service, "current_version", lambda: "1.0.0")
    yield www, dist, tmp_path
    httpd.shutdown()


def test_the_start_time_update_installs_a_newer_release(world):
    www, dist, tmp = world
    oi.build_overlay(make_dist(tmp / "new", "1.1.0"), tmp / "o.zip")
    publish(www, "1.1.0", tmp / "o.zip")
    assert update_cli.main(["apply", "--root", str(dist)]) == 0
    assert read(dist, "backend-py/app/main.py") == "# main 1.1.0"


def test_the_start_never_fails_without_a_release_or_with_a_bad_one(world, capsys):
    www, dist, tmp = world
    assert update_cli.main(["apply", "--root", str(dist)]) == 0  # nothing published
    assert read(dist, "backend-py/app/main.py") == "# main 1.0.0"
    # a signed release whose overlay is not acceptable: refused, the desktop starts with what it has
    bad = zip_with(tmp / "bad.zip", {"backend-py/app/main.py": "x", "runtime/python/python3": "evil"})
    publish(www, "1.1.0", bad)
    assert update_cli.main(["apply", "--root", str(dist)]) == 0
    assert "refused" in capsys.readouterr().out and read(dist, "backend-py/app/main.py") == "# main 1.0.0"
