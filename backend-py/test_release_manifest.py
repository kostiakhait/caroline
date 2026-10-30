"""The release manifest (app/release_manifest.py) and the release tool (tools/release.py): the signature, the shape, the choice of an artifact.
Run: python -m pytest test_release_manifest.py -q"""

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import release_manifest as rm

TOOL = Path(__file__).resolve().parent.parent / "tools" / "release.py"
SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")  # RFC 8032, test 1


def manifest_bytes(**over) -> bytes:
    manifest = {"version": "1.2.3", "commit": "abc", "date": "2026-10-30", "artifacts": {
        "windows": {"file": "CarolineInstaller-1.2.3.exe", "sha256": "a" * 64, "size": 10},
        "beroun": {"file": "caroline-beroun-1.2.3.zip", "sha256": "b" * 64, "size": 20, "runtime": 3}}}
    manifest.update(over)
    return json.dumps(manifest).encode()


def signed(body: bytes) -> tuple[bytes, str]:
    return rm.ed25519_public_key(SEED), base64.b64encode(rm.ed25519_sign(SEED, body)).decode()


def test_ed25519_matches_the_rfc_8032_vector():
    assert rm.ed25519_public_key(SEED).hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    sig = rm.ed25519_sign(SEED, b"")
    assert sig.hex() == ("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
                         "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert rm.ed25519_verify(rm.ed25519_public_key(SEED), sig, b"")
    assert not rm.ed25519_verify(rm.ed25519_public_key(SEED), sig, b"x")


def test_a_valid_manifest_is_accepted():
    body = manifest_bytes()
    pub, sig = signed(body)
    assert rm.verify_manifest(body, sig, pub)["version"] == "1.2.3"


def test_a_changed_manifest_or_a_wrong_key_is_refused():
    body = manifest_bytes()
    pub, sig = signed(body)
    with pytest.raises(rm.ReleaseError, match="does not match"):
        rm.verify_manifest(body.replace(b"1.2.3", b"9.9.9"), sig, pub)
    with pytest.raises(rm.ReleaseError, match="does not match"):
        rm.verify_manifest(body, sig, rm.ed25519_public_key(b"\x01" * 32))
    with pytest.raises(rm.ReleaseError, match="not base64"):
        rm.verify_manifest(body, "!!!", pub)
    with pytest.raises(rm.ReleaseError, match="no release public key"):
        rm.verify_manifest(body, sig, b"")


def test_the_shape_is_checked_even_when_signed():
    for bad in (manifest_bytes(version="1.2"), manifest_bytes(artifacts={}), manifest_bytes(artifacts={"amiga": {}}),
                manifest_bytes(artifacts={"windows": {"file": "../x.exe", "sha256": "a" * 64, "size": 1}}),
                manifest_bytes(artifacts={"windows": {"file": "x.exe", "sha256": "zz", "size": 1}})):
        pub, sig = signed(bad)
        with pytest.raises(rm.ReleaseError):
            rm.verify_manifest(bad, sig, pub)


def test_versions_never_go_down():
    assert rm.is_newer("1.10.0", "1.9.9") and not rm.is_newer("1.9.9", "1.10.0") and not rm.is_newer("1.2.3", "1.2.3")
    assert rm.is_newer("0.0.1", "dev")  # a dev tree is older than any release


def test_an_artifact_waits_for_the_runtime_it_needs():
    manifest = json.loads(manifest_bytes())
    assert rm.artifact_for(manifest, "windows")[1] == "ok"
    assert rm.artifact_for(manifest, "linux-x86_64") == (None, "absent")
    art, why = rm.artifact_for(manifest, "beroun", have_runtime=2)
    assert why == "needs_runtime" and art.runtime == 3
    assert rm.artifact_for(manifest, "beroun", have_runtime=3)[1] == "ok"


def test_a_downloaded_file_must_be_exactly_the_named_one(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    ok = rm.Artifact("windows", "a.bin", rm.file_sha256(f), 5, 0)
    rm.check_file(f, ok)
    with pytest.raises(rm.ReleaseError, match="sha256"):
        rm.check_file(f, rm.Artifact("windows", "a.bin", "0" * 64, 5, 0))
    with pytest.raises(rm.ReleaseError, match="bytes"):
        rm.check_file(f, rm.Artifact("windows", "a.bin", ok.sha256, 6, 0))


def run_tool(*args, env=None):
    import os
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], capture_output=True, text=True, env={**os.environ, **(env or {})})


def test_the_tool_makes_a_key_a_release_and_verifies_it(tmp_path):
    key = tmp_path / "keys" / "release.key"
    made = run_tool("keygen", key)
    assert made.returncode == 0, made.stderr
    public = made.stdout.split("public key:")[1].strip()
    assert run_tool("keygen", key).returncode != 0  # never overwritten

    win = tmp_path / "CarolineInstaller-1.2.3.exe"
    win.write_bytes(b"installer")
    out = tmp_path / "out"
    built = run_tool("build", "--version", "1.2.3", "--key", key, "--out", out, "--artifact", f"windows={win}", "--commit", "abc")
    assert built.returncode == 0, built.stderr
    (out / win.name).write_bytes(b"installer")  # what the upload puts next to the manifest
    env = {"CAROLINE_RELEASE_PUBLIC_KEY": public}
    assert run_tool("verify", out, env=env).returncode == 0
    (out / win.name).write_bytes(b"tampered!")
    bad = run_tool("verify", out, env=env)
    assert bad.returncode == 1 and "NOT OK" in bad.stderr
    (out / win.name).write_bytes(b"installer")
    assert run_tool("verify", out, env={"CAROLINE_RELEASE_PUBLIC_KEY": rm.ed25519_public_key(b"\x02" * 32).hex()}).returncode == 1
