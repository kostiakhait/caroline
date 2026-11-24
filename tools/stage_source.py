"""Stage the committed sources of a release build (2026-11-22).

A release is built from HEAD, never from the working tree: uncommitted edits (another
session's work in progress) must not ship. This writes the tracked files of HEAD under the
given top-level paths into DEST, plus the few local-only files a release needs and git
deliberately does not hold (--local, e.g. backend-py/app/sw_service_keys_local.py).

Incremental, so Make's stamps keep working: a file is written only when its content changed
since the last staging (DEST/.stage-index.json keeps each path's blob id), so unchanged files
keep their mtimes; files gone from HEAD are deleted. Build outputs inside DEST (bin/, obj/)
are never touched -- only paths this tool wrote itself are ever removed.

  python tools/stage_source.py --repo . --dest .build/src --paths Windows backend-py ... \
      --local backend-py/app/sw_service_keys_local.py
--ref stages another commit or branch without checking it out (the constructor's variant lives
on the branch `ferret`). Writes DEST/.stage-commit (the short commit) for the release manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def tree(repo: Path, ref: str, paths: list[str]) -> dict[str, str]:
    """path -> blob id of every tracked file of REF under the given paths."""
    out = git(repo, "ls-tree", "-r", "-z", ref, "--", *paths)
    files = {}
    for entry in out.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, kind, blob = meta.split(b" ")
        if kind == b"blob":
            files[path.decode("utf-8")] = blob.decode("ascii")
    return files


def read_blobs(repo: Path, blobs: list[str]) -> dict[str, bytes]:
    """Contents of the given blobs, one `git cat-file --batch` process for all of them."""
    proc = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def feed() -> None:  # in its own thread: git stops reading requests while its output pipe is full
        proc.stdin.write("".join(b + "\n" for b in blobs).encode("ascii"))
        proc.stdin.close()

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    contents = {}
    for blob in blobs:
        header = proc.stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise SystemExit(f"stage_source: git cat-file gave {header!r} for {blob}")
        size = int(header[2])
        contents[blob] = proc.stdout.read(size)
        proc.stdout.read(1)  # the newline after each object
    feeder.join()
    if proc.wait() != 0:
        raise SystemExit("stage_source: git cat-file failed")
    return contents


def write_if_changed(target: Path, data: bytes) -> bool:
    if target.is_file() and target.read_bytes() == data:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--ref", default="HEAD", help="commit or branch to stage (default HEAD), e.g. ferret")
    ap.add_argument("--paths", nargs="+", required=True, help="top-level paths of HEAD to stage")
    ap.add_argument("--local", nargs="*", default=[], help="untracked files a release still needs, copied from the working tree")
    args = ap.parse_args()

    repo, dest = Path(args.repo).resolve(), Path(args.dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    index_file = dest / ".stage-index.json"
    try:
        old = json.loads(index_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}

    new = tree(repo, args.ref, args.paths)
    stale = [p for p, b in new.items() if old.get(p) != b or not (dest / p).is_file()]
    contents = read_blobs(repo, sorted({new[p] for p in stale}))
    written = sum(write_if_changed(dest / p, contents[new[p]]) for p in stale)

    removed = 0
    for path in old:
        if path not in new and not path.startswith("local:"):
            try:
                (dest / path).unlink()
                removed += 1
            except FileNotFoundError:
                pass

    for rel in args.local:
        src = repo / rel
        if not src.is_file():
            raise SystemExit(f"stage_source: local file {rel} is missing -- a release needs it")
        data = src.read_bytes()
        written += write_if_changed(dest / rel, data)
        new["local:" + rel] = hashlib.sha1(data).hexdigest()
    for key in old:
        if key.startswith("local:") and key not in new:
            (dest / key[len("local:"):]).unlink(missing_ok=True)
            removed += 1

    index_file.write_text(json.dumps(new, indent=0, sort_keys=True), encoding="utf-8")
    commit = git(repo, "rev-parse", "--short", args.ref + "^{commit}").decode("ascii").strip()
    write_if_changed(dest / ".stage-commit", (commit + "\n").encode("ascii"))
    print(f"staged {commit}: {len(new)} files, {written} written, {removed} removed -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
