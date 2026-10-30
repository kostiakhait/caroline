"""The release manifest of Caroline (docs/UPDATE_EVERYWHERE_PLAN.md): one signed `release.json` per release, read by every platform's updater.

    {"version": "1.42.0", "commit": "a7b052e", "date": "2026-10-30",
     "artifacts": {"windows": {"file", "sha256", "size"}, "linux-x86_64": {...}, "beroun": {..., "runtime": 3}}}

`release.json.sig` holds the base64 Ed25519 signature over the exact bytes of `release.json`. The key is Caroline's release key (not the app store's of
OS Beroun); its public half is `RELEASE_PUBLIC_KEY` below. Pure standard library (RFC 8032), because a signature is checked once per update and
this module is also run by the release tool on the maintainer's machine, which has no `cryptography`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The public half of the release key, hex. Made once by `tools/release.py keygen`; the private half never enters a repository.
RELEASE_PUBLIC_KEY = "7c347b8cb521198c71f4e83cc4430e03764c788258fa2c185fbbdbdde16d3f97"

PLATFORMS = ("windows", "linux-x86_64", "beroun", "xopek")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReleaseError(ValueError):
    """The manifest or its signature is not acceptable. The message says why (it goes into the log)."""


# ---- Ed25519 (RFC 8032) --------------------------------------------------------------------------------------------------------
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _x_recover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P:
        x = x * _I % _P
    if (x * x - xx) % _P:
        raise ValueError("not a point")
    return _P - x if x & 1 else x


_BY = 4 * pow(5, _P - 2, _P) % _P
_B = (_x_recover(_BY) % _P, _BY % _P, 1, _x_recover(_BY) * _BY % _P)


def _add(a, b):
    A, B = (a[1] - a[0]) * (b[1] - b[0]) % _P, (a[1] + a[0]) * (b[1] + b[0]) % _P
    C, Dd = 2 * a[3] * b[3] * _D % _P, 2 * a[2] * b[2] % _P
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(s: int, pt):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _eq(a, b) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _decode(s: bytes):
    if len(s) != 32:
        raise ValueError("bad point")
    y = int.from_bytes(s, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    if y >= _P:
        raise ValueError("bad point")
    x = _x_recover(y)
    if x & 1 != sign:
        x = _P - x
    return (x, y, 1, x * y % _P)


def _encode(pt) -> bytes:
    zi = pow(pt[2], _P - 2, _P)
    x, y = pt[0] * zi % _P, pt[1] * zi % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _expand(seed: bytes) -> tuple[int, bytes]:
    h = hashlib.sha512(seed).digest()
    return (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254), h[32:]


def ed25519_public_key(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("the seed is 32 bytes")
    return _encode(_mul(_expand(seed)[0], _B))


def ed25519_sign(seed: bytes, message: bytes) -> bytes:
    a, prefix = _expand(seed)
    pub = _encode(_mul(a, _B))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _L
    rb = _encode(_mul(r, _B))
    h = int.from_bytes(hashlib.sha512(rb + pub + message).digest(), "little") % _L
    return rb + ((r + h * a) % _L).to_bytes(32, "little")


def ed25519_verify(public_key: bytes, signature: bytes, message: bytes) -> bool:
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        a, r = _decode(public_key), _decode(signature[:32])
        s = int.from_bytes(signature[32:], "little")
        if s >= _L:
            return False
        h = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _L
        return _eq(_mul(s, _B), _add(r, _mul(h, a)))
    except ValueError:
        return False


# ---- the manifest ---------------------------------------------------------------------------------------------------------------

def release_public_key() -> bytes:
    """The key releases must be signed with; `CAROLINE_RELEASE_PUBLIC_KEY` (hex) overrides it, for tests of an updater against a test release."""
    hexkey = (os.environ.get("CAROLINE_RELEASE_PUBLIC_KEY") or RELEASE_PUBLIC_KEY).strip()
    try:
        return bytes.fromhex(hexkey)
    except ValueError:
        return b""


def parse_version(text: str) -> tuple[int, int, int]:
    m = _VERSION.match(str(text).strip())
    if not m:
        raise ReleaseError(f'"{text}" is not a version x.y.z')
    return int(m[1]), int(m[2]), int(m[3])


def is_newer(remote: str, local: str) -> bool:
    """True when `remote` is higher than `local`. An unreadable local version (a dev tree) counts as older than any release."""
    try:
        have = parse_version(local)
    except ReleaseError:
        return True
    return parse_version(remote) > have


@dataclass(frozen=True)
class Artifact:
    platform: str
    file: str
    sha256: str
    size: int
    runtime: int
    payload: dict | None = None  # Windows: the Caroline.zip the installer downloads ({file, sha256, size}); its hash is signed with the rest


def verify_manifest(manifest_bytes: bytes, signature_b64: str | bytes, public_key: bytes | None = None) -> dict[str, Any]:
    """The manifest as a dict, once its signature is valid and its shape is right; otherwise ReleaseError. Nothing of an unsigned manifest is trusted."""
    key = release_public_key() if public_key is None else public_key
    if len(key) != 32:
        raise ReleaseError("no release public key is configured")
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise ReleaseError("the signature is not base64") from exc
    if not ed25519_verify(key, signature, manifest_bytes):
        raise ReleaseError("the signature does not match the manifest")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("the manifest is not JSON") from exc
    check_manifest(manifest)
    return manifest


def check_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict):
        raise ReleaseError("the manifest is not an object")
    parse_version(manifest.get("version", ""))
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ReleaseError("the manifest has no artifacts")
    for platform, art in artifacts.items():
        if platform not in PLATFORMS:
            raise ReleaseError(f'unknown platform "{platform}"')
        if not isinstance(art, dict):
            raise ReleaseError(f"{platform}: not an object")
        name = art.get("file")
        if not isinstance(name, str) or not name or name != Path(name).name or name.startswith("."):
            raise ReleaseError(f"{platform}: the file must be a plain file name")
        if not _SHA256.match(str(art.get("sha256", ""))):
            raise ReleaseError(f"{platform}: sha256 is not 64 hex digits")
        if not isinstance(art.get("size"), int) or art["size"] < 0:
            raise ReleaseError(f"{platform}: size is not a number")
        if "runtime" in art and (not isinstance(art["runtime"], int) or art["runtime"] < 0):
            raise ReleaseError(f"{platform}: runtime is not a number")
        if "payload" in art:
            pay = art["payload"]
            pname = pay.get("file") if isinstance(pay, dict) else None
            if not isinstance(pname, str) or not pname or pname != Path(pname).name or pname.startswith("."):
                raise ReleaseError(f"{platform}: the payload must name a plain file")
            if not _SHA256.match(str(pay.get("sha256", ""))) or not isinstance(pay.get("size"), int) or pay["size"] < 0:
                raise ReleaseError(f"{platform}: the payload needs a sha256 and a size")


def artifact_for(manifest: dict[str, Any], platform: str, have_runtime: int = 0) -> tuple[Artifact | None, str]:
    """(artifact, "ok") when this platform can install the release; (None, "absent") when the release has nothing for it; (artifact, "needs_runtime")
    when it asks for a newer runtime than the platform has (the platform waits for its own update, and installs nothing)."""
    art = (manifest.get("artifacts") or {}).get(platform)
    if art is None:
        return None, "absent"
    found = Artifact(platform, art["file"], art["sha256"], art["size"], art.get("runtime", 0), art.get("payload"))
    if found.runtime > have_runtime:
        return found, "needs_runtime"
    return found, "ok"


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_file(path: str | Path, artifact: Artifact) -> None:
    """ReleaseError unless the downloaded file is exactly the artifact the signed manifest names."""
    size = Path(path).stat().st_size
    if size != artifact.size:
        raise ReleaseError(f"{artifact.file}: {size} bytes, the manifest says {artifact.size}")
    if file_sha256(path) != artifact.sha256:
        raise ReleaseError(f"{artifact.file}: sha256 differs from the manifest")
