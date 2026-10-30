#!/usr/bin/env python3
"""The release tool of Caroline (docs/UPDATE_EVERYWHERE_PLAN.md, stage 1): one version, one signed manifest for every platform.

  release.py keygen KEYFILE
      Makes the release key once: the 32-byte seed (hex) goes to KEYFILE, which must be outside every repository; prints the PUBLIC key
      (hex) to paste into RELEASE_PUBLIC_KEY of backend-py/app/release_manifest.py. Refuses to overwrite a key.

  release.py build --version X.Y.Z --key KEYFILE --out DIR --artifact PLATFORM=PATH[,runtime=N] [--artifact ...]
      Writes DIR/release.json and DIR/release.json.sig for the given artifact files (their names, sha256 and sizes). The files themselves are
      not copied: upload them from their paths together with the two files of DIR, all of them or none. Platforms: windows, linux-x86_64, beroun.

  release.py overlay DIST OUT.zip
      Makes the code-layer artifact (the platforms `xopek` and `beroun`) from a dist: `make dist-linux` for xopek, `make dist-intellicat` for beroun. Only the
      paths the update owns (backend-py/app, wwwroot, ...); what is built is exactly what app/overlay_install.py applies.

  release.py verify DIR
      Checks DIR/release.json against its signature and the artifacts found next to it by name (the public key as built in, or
      CAROLINE_RELEASE_PUBLIC_KEY). Run it on the folder that was uploaded, or on what was downloaded.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("release_manifest", HERE.parent / "backend-py" / "app" / "release_manifest.py")
rm = importlib.util.module_from_spec(spec)
sys.modules["release_manifest"] = rm  # the module's dataclass needs to find itself here
spec.loader.exec_module(rm)


def read_seed(path: Path) -> bytes:
    try:
        seed = bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        raise SystemExit(f"{path}: not a release key file (hex seed)")
    if len(seed) != 32:
        raise SystemExit(f"{path}: the seed must be 32 bytes")
    return seed


def cmd_keygen(args: argparse.Namespace) -> int:
    key = Path(args.keyfile).expanduser()
    if key.exists():
        raise SystemExit(f"{key} exists: a release key is made once; deleting it would orphan every installed Caroline")
    top = subprocess.run(["git", "-C", str(key.parent), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode == 0:
        raise SystemExit(f"{key} is inside the repository {top.stdout.strip()}: put the key outside every repository")
    import secrets
    seed = secrets.token_bytes(32)
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text(seed.hex() + "\n", encoding="ascii")
    try:
        key.chmod(0o600)
    except OSError:
        pass
    print(f"key written to {key} (back it up: it cannot be remade)")
    print(f"public key: {rm.ed25519_public_key(seed).hex()}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    rm.parse_version(args.version)
    seed = read_seed(Path(args.key).expanduser())
    artifacts: dict[str, dict] = {}
    for item in args.artifact:
        head, _, rest = item.partition(",")
        platform, _, path = head.partition("=")
        if platform not in rm.PLATFORMS or not path:
            raise SystemExit(f'--artifact "{item}": expected PLATFORM=PATH[,runtime=N], platform one of {", ".join(rm.PLATFORMS)}')
        if platform in artifacts:
            raise SystemExit(f"{platform} given twice")
        file = Path(path)
        if not file.is_file():
            raise SystemExit(f"{file}: no such file")
        entry = {"file": file.name, "sha256": rm.file_sha256(file), "size": file.stat().st_size}
        if rest:
            key, _, value = rest.partition("=")
            if key != "runtime" or not value.isdigit():
                raise SystemExit(f'--artifact "{item}": the only option is runtime=N')
            entry["runtime"] = int(value)
        artifacts[platform] = entry
    commit = args.commit or subprocess.run(["git", "-C", str(HERE.parent), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    manifest = {"version": args.version, "commit": commit, "date": args.date or date.today().isoformat(), "artifacts": artifacts}
    rm.check_manifest(manifest)
    body = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "release.json").write_bytes(body)
    (out / "release.json.sig").write_text(base64.b64encode(rm.ed25519_sign(seed, body)).decode("ascii") + "\n", encoding="ascii")
    print(f"{out / 'release.json'} and release.json.sig written for {args.version}; upload with them:")
    for platform, entry in artifacts.items():
        print(f"  {platform}: {entry['file']} ({entry['size']} bytes)")
    return 0


def cmd_overlay(args: argparse.Namespace) -> int:
    oi_spec = importlib.util.spec_from_file_location("overlay_install", HERE.parent / "backend-py" / "app" / "overlay_install.py")
    oi = importlib.util.module_from_spec(oi_spec)
    sys.modules["overlay_install"] = oi
    oi_spec.loader.exec_module(oi)
    try:
        entries = oi.build_overlay(args.dist, args.out)
    except oi.OverlayError as exc:
        raise SystemExit(str(exc))
    print(f"{args.out}: {len(entries)} files from {args.dist}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    folder = Path(args.dir)
    try:
        manifest = rm.verify_manifest((folder / "release.json").read_bytes(), (folder / "release.json.sig").read_text(encoding="ascii").strip())
        for platform in manifest["artifacts"]:
            art, _ = rm.artifact_for(manifest, platform, have_runtime=10 ** 9)
            rm.check_file(folder / art.file, art)
            print(f"  {platform}: {art.file} ok")
    except (rm.ReleaseError, OSError) as exc:
        print(f"NOT OK: {exc}", file=sys.stderr)
        return 1
    print(f"release {manifest['version']} ({manifest['commit']}) verified")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("keygen")
    p.add_argument("keyfile")
    p = sub.add_parser("build")
    p.add_argument("--version", default=(HERE.parent / "backend-py" / "VERSION").read_text(encoding="utf-8").strip(), help="default: backend-py/VERSION, the version every platform ships")
    p.add_argument("--key", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--artifact", action="append", required=True)
    p.add_argument("--commit")
    p.add_argument("--date")
    p = sub.add_parser("overlay")
    p.add_argument("dist")
    p.add_argument("out")
    p = sub.add_parser("verify")
    p.add_argument("dir")
    args = ap.parse_args()
    return {"keygen": cmd_keygen, "build": cmd_build, "overlay": cmd_overlay, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
