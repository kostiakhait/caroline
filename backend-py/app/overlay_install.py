"""The code layer of Caroline as an update artifact (docs/UPDATE_EVERYWHERE_PLAN.md): a zip of her portable code, applied over an installed dist.

For the platforms whose runtime (Python, Node, the models) is not the update's business and whose Caroline is a folder, not an installer or an AppImage:
the Xopek desktops (platform `xopek`, applied when a desktop starts, before Caroline does) and OS Beroun (platform `beroun`, applied by `shelld`).

The zip holds only OWNED paths, relative to the root of a dist (the folder with backend-py/ and wwwroot/):
  directories, replaced whole (so a module that was removed in the new version is removed too: plugins are discovered by listing their folder)
  files, copied over (the sidecars' `node_modules` live beside them and are not the update's)
Anything else in the zip, a path that leaves the root, a link, or a zip that unpacks past the size limit is refused before anything on disk is touched.
Applying swaps directories one by one with the old one kept aside; if any step fails everything is put back (and a crash in the middle is repaired at
the next call). Pure standard library.
"""

from __future__ import annotations

import os
import shutil
import stat
import zipfile
from pathlib import Path

OWNED_DIRS = ("backend-py/app", "backend-py/skills-src", "wwwroot", "linux-shell")
OWNED_FILES = (
    "backend-py/run_server.py", "backend-py/supervisor.py", "backend-py/VERSION", "backend-py/requirements-linux.txt",
    "whatsapp-sidecar/index.js", "whatsapp-sidecar/package.json",
    "messenger-sidecar/index.js", "messenger-sidecar/package.json", "messenger-sidecar/fca-config.json",
)
REQUIRED = "backend-py/app/main.py"
MAX_UNPACKED_BYTES = 600 * 1024 * 1024
STAGING = ".update-staging"
PREVIOUS = ".update-previous"


class OverlayError(ValueError):
    """The overlay is not acceptable or could not be applied; the message says why. The installed dist is as it was."""


def _owned(name: str) -> bool:
    return name in OWNED_FILES or any(name == d or name.startswith(d + "/") for d in OWNED_DIRS)


def build_overlay(dist_root: str | Path, out_zip: str | Path) -> list[str]:
    """Writes the overlay zip from a dist (`make dist-linux`, or an edition's cut of it). Returns the entries. The same OWNED lists as the apply, so what is built is what is applied."""
    root = Path(dist_root)
    if not (root / REQUIRED).is_file():
        raise OverlayError(f"{root} has no {REQUIRED}: not a dist of Caroline")
    entries: list[str] = []
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(OWNED_FILES):
            if (root / rel).is_file():
                zf.write(root / rel, rel)
                entries.append(rel)
        for d in OWNED_DIRS:
            for path in sorted((root / d).rglob("*")) if (root / d).is_dir() else []:
                rel = path.relative_to(root).as_posix()
                if path.is_file() and "__pycache__" not in rel and not rel.endswith((".pyc", ".pyo")):
                    zf.write(path, rel)
                    entries.append(rel)
    return entries


def _check_zip(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if not infos:
        raise OverlayError("the overlay is empty")
    total = 0
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in Path(name).parts or "\\" in name or ":" in name:
            raise OverlayError(f"{name}: a path that leaves the root")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise OverlayError(f"{name}: a link")
        if not _owned(name):
            raise OverlayError(f"{name}: not a path the update owns")
        total += info.file_size
        if total > MAX_UNPACKED_BYTES:
            raise OverlayError("the overlay unpacks to more than the limit")
    if REQUIRED not in {i.filename for i in infos}:
        raise OverlayError(f"the overlay has no {REQUIRED}")
    return infos


def recover(root: str | Path) -> None:
    """A previous apply that died in the middle left `.update-previous`: put its directories and files back, then clear the leftovers."""
    base = Path(root)
    prev = base / PREVIOUS
    if prev.is_dir():
        for rel in OWNED_DIRS + OWNED_FILES:
            if (prev / rel).exists():  # its old version is aside: whatever stands in its place now is a half-applied new one
                target = base / rel
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists():
                    target.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(prev / rel), target)
        shutil.rmtree(prev, ignore_errors=True)
    shutil.rmtree(base / STAGING, ignore_errors=True)


def apply_overlay(zip_path: str | Path, root: str | Path) -> list[str]:
    """Applies the overlay over the dist at `root`. Returns what was replaced. OverlayError, with the dist as it was, when anything is wrong."""
    base = Path(root)
    recover(base)
    staging, prev = base / STAGING, base / PREVIOUS
    moved: list[str] = []  # owned paths whose old version is in `prev`
    created: list[str] = []  # owned paths that did not exist before
    present_dirs: list[str] = []
    present_files: list[str] = []
    try:
        try:
            zf = zipfile.ZipFile(zip_path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise OverlayError(f"not a zip: {exc}") from exc
        with zf:
            infos = _check_zip(zf)
            staging.mkdir()
            for info in infos:
                zf.extract(info, staging)
        present_dirs.extend(d for d in OWNED_DIRS if (staging / d).is_dir())
        present_files.extend(f for f in OWNED_FILES if (staging / f).is_file())
        for rel in present_dirs + present_files:
            target = base / rel
            if target.exists():
                (prev / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), prev / rel)
                moved.append(rel)
            else:
                created.append(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging / rel), target)
    except Exception as exc:
        for rel in present_dirs + present_files:
            target = base / rel
            if rel in moved or rel in created:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists():
                    target.unlink()
        for rel in moved:
            if (prev / rel).exists():
                (base / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(prev / rel), base / rel)
        shutil.rmtree(prev, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, OverlayError):
            raise
        raise OverlayError(f"could not apply the overlay, the old version is back: {exc}") from exc
    shutil.rmtree(prev, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    return present_dirs + present_files
