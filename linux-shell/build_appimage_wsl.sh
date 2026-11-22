#!/bin/bash
# Builds the AppImage from a dist-linux tree made on Windows, inside WSL (called by `make appimage-wsl`, see deploy.mk).
#
# The heavy part (runtime/python with its pip packages, Playwright's Chromium, codex, node, signal-cli, the sidecars'
# node_modules) does not change from release to release, so it lives in a persistent build root in WSL's own file system
# and is provisioned again only when the files that define it change (requirements-linux.txt, provision_runtime.sh, the two
# sidecar package.json). Every build copies the current code over that root and wraps it with build_appimage.sh.
#
# Usage: build_appimage_wsl.sh <dist-linux directory> <output .AppImage path>     (both as WSL paths)
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <dist-linux directory> <output .AppImage path>" >&2
    exit 1
fi

SRC="$(readlink -f "$1")"
OUT="$(readlink -f "$(dirname "$2")")/$(basename "$2")"
WORK="${CAROLINE_WSL_WORK:-$HOME/.intellicat/lin-build/appimage}"
ROOT="$WORK/root"
mkdir -p "$ROOT" "$(dirname "$OUT")"

for need in rsync curl tar xz sha256sum npm; do
    command -v "$need" >/dev/null || { echo "error: $need is not installed in WSL" >&2; exit 1; }
done
[ -f "$SRC/backend-py/requirements-linux.txt" ] || { echo "error: $SRC is not a dist-linux tree (make dist-linux)" >&2; exit 1; }

echo "--- code: $SRC -> $ROOT ---"
for dir in linux-shell wwwroot SplashBanners backend-py; do
    mkdir -p "$ROOT/$dir"
    # .cache holds the downloaded appimagetool, node_modules the sidecars' packages: kept across builds
    rsync -a --delete --exclude .cache --exclude node_modules "$SRC/$dir/" "$ROOT/$dir/"
done
for dir in whatsapp-sidecar messenger-sidecar; do
    mkdir -p "$ROOT/$dir"
    rsync -a --delete --exclude node_modules "$SRC/$dir/" "$ROOT/$dir/"
done
# backend-py was replaced above; the provisioned runtime lives beside it and is untouched

STAMP="$(cat "$SRC/backend-py/requirements-linux.txt" "$SRC/linux-shell/provision_runtime.sh" \
    "$SRC/whatsapp-sidecar/package.json" "$SRC/messenger-sidecar/package.json" | sha256sum | cut -d' ' -f1)"
if [ "$(cat "$WORK/provisioned.stamp" 2>/dev/null || true)" != "$STAMP" ] || [ ! -d "$ROOT/runtime/python" ]; then
    echo "--- provisioning the runtime (first build, or its definition changed) ---"
    rm -f "$WORK/provisioned.stamp"
    bash "$ROOT/linux-shell/provision_runtime.sh" "$ROOT"
    for sidecar in whatsapp-sidecar messenger-sidecar; do
        PATH="$ROOT/runtime/node/bin:$PATH" npm install --prefix "$ROOT/$sidecar"
    done
    echo "$STAMP" > "$WORK/provisioned.stamp"
else
    echo "--- runtime up to date (stamp ${STAMP:0:12}) ---"
fi

rm -f "$OUT"
bash "$ROOT/linux-shell/build_appimage.sh" "$ROOT" "$OUT"
ls -l "$OUT"
