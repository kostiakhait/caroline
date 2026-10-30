"""The shared update client (app/update_service.py) against a real HTTP server serving a signed release: check, download, verify, the rule.
Run: python -m pytest test_update_service.py -q"""

import base64
import functools
import http.server
import json
import threading
from pathlib import Path

import pytest

from app import release_manifest as rm
from app.update_service import UpdateService

SEED = b"\x07" * 32
PUB = rm.ed25519_public_key(SEED)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def publish(root: Path, version="1.1.0", artifacts=None, payload=b"installer-bytes", tamper=False, sign_with=SEED):
    artifacts = artifacts or {"windows": ("CarolineInstaller-1.1.0.exe", 0)}
    entries = {}
    for platform, (name, runtime) in artifacts.items():
        (root / name).write_bytes(payload)
        entries[platform] = {"file": name, "sha256": rm.file_sha256(root / name), "size": len(payload), **({"runtime": runtime} if runtime else {})}
        if tamper:  # the manifest describes the genuine file, the server serves another
            (root / name).write_bytes(b"EVIL" + payload)
    body = json.dumps({"version": version, "commit": "abc", "date": "2026-10-30", "artifacts": entries}).encode()
    (root / "release.json").write_bytes(body)
    (root / "release.json.sig").write_text(base64.b64encode(rm.ed25519_sign(sign_with, body)).decode())


def service(base, tmp_path, **kw):
    return UpdateService(base_url=base, platform=kw.pop("platform", "windows"), version=kw.pop("version", "1.0.0"), runtime=kw.pop("runtime", 0),
                         download_dir=tmp_path / "dl", public_key=kw.pop("public_key", PUB))


def test_no_release_published_is_quietly_up_to_date(server, tmp_path):
    _root, base = server
    assert service(base, tmp_path).check()["status"] == "up_to_date"


def test_a_newer_release_is_downloaded_verified_and_ready(server, tmp_path):
    root, base = server
    publish(root)
    svc = service(base, tmp_path)
    state = svc.run_once()
    assert state["status"] == "ready" and state["version"] == "1.1.0"
    assert Path(state["path"]).read_bytes() == b"installer-bytes"
    assert not list((tmp_path / "dl").glob("*.part"))


def test_the_same_or_an_older_release_is_not_an_update(server, tmp_path):
    root, base = server
    publish(root, version="1.0.0")
    assert service(base, tmp_path).run_once()["status"] == "up_to_date"
    publish(root, version="0.9.0")
    assert service(base, tmp_path).run_once()["status"] == "up_to_date"


def test_a_manifest_signed_with_another_key_is_refused(server, tmp_path):
    root, base = server
    publish(root, sign_with=b"\x09" * 32)
    state = service(base, tmp_path).run_once()
    assert state["status"] == "failed" and "signature" in state["error"]
    assert not (tmp_path / "dl").exists() or not any((tmp_path / "dl").iterdir())


def test_a_file_that_differs_from_the_signed_hash_is_never_ready(server, tmp_path):
    root, base = server
    publish(root, tamper=True)
    state = service(base, tmp_path).run_once()
    assert state["status"] == "failed" and ("sha256" in state["error"] or "bytes" in state["error"])
    assert state["path"] is None


def test_a_release_needing_a_newer_runtime_waits(server, tmp_path):
    root, base = server
    publish(root, artifacts={"beroun": ("caroline-beroun-1.1.0.zip", 3)})
    svc = service(base, tmp_path, platform="beroun", runtime=2)
    assert svc.run_once()["status"] == "needs_runtime"
    assert not (tmp_path / "dl").exists() or not any((tmp_path / "dl").iterdir())
    assert service(base, tmp_path, platform="beroun", runtime=3).run_once()["status"] == "ready"


def test_a_release_without_this_platform_is_not_an_update(server, tmp_path):
    root, base = server
    publish(root)
    assert service(base, tmp_path, platform="linux-x86_64").run_once()["status"] == "up_to_date"


def test_a_ready_file_is_not_downloaded_twice(server, tmp_path):
    root, base = server
    publish(root)
    svc = service(base, tmp_path)
    svc.run_once()
    (root / "CarolineInstaller-1.1.0.exe").unlink()  # the server no longer has it; the verified copy is enough
    assert svc.run_once()["status"] == "ready"


def test_the_rule_installs_when_inactive_asks_when_in_use_and_obeys_a_yes(server, tmp_path):
    root, base = server
    publish(root)
    svc = service(base, tmp_path)
    assert svc.action(busy=False) == "none"  # nothing ready yet
    svc.run_once()
    assert svc.action(busy=False) == "install"
    assert svc.action(busy=True) == "ask"
    svc.confirm()
    assert svc.action(busy=True) == "install"
    svc.mark_installed()
    assert svc.action(busy=True) == "none" and svc.status()["confirmed"] is False


def test_leftovers_of_an_older_version_are_removed(server, tmp_path):
    root, base = server
    publish(root)
    (tmp_path / "dl").mkdir()
    (tmp_path / "dl" / "CarolineInstaller-1.0.5.exe").write_bytes(b"old")
    service(base, tmp_path).run_once()
    assert [p.name for p in (tmp_path / "dl").iterdir()] == ["CarolineInstaller-1.1.0.exe"]
