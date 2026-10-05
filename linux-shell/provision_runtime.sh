#!/bin/bash
# Builds the Linux runtime/ tree for a Caroline install:
#   runtime/python -- a self-contained CPython (astral-sh/python-build-standalone)
#                     with backend-py's pinned dependencies installed into it
#   runtime/codex  -- the pinned Codex app-server package (openai/codex release,
#                     sha256-verified against the release's own SHA256SUMS)
# Must run ON Linux (executes the downloaded Linux binaries) -- unlike
# dist-linux in the Makefile, which is pure file copying and works from any host.
#
# Usage: provision_runtime.sh <install_root>
#   <install_root> is a dist-linux output directory (or an installed
#   CAROLINE_APP_ROOT) -- the one that already has backend-py/ in it.
set -euo pipefail

PBS_RELEASE="20261003"
PBS_PYTHON_VERSION="3.12.15"
PBS_TARBALL="cpython-${PBS_PYTHON_VERSION}+${PBS_RELEASE}-x86_64-unknown-linux-gnu-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_TARBALL}"

CODEX_VERSION="0.155.1"
CODEX_ARCHIVE="codex-app-server-package-x86_64-unknown-linux-musl.tar.gz"
CODEX_URL="https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/${CODEX_ARCHIVE}"
CODEX_SHA256="a1784b0f3991e4853caaddcc167d2bc8c540f12eddb1b5e40b1ec49f2dbcc024"

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <install_root>" >&2
    exit 1
fi

INSTALL_ROOT="$(readlink -f "$1")"
HERE="$(dirname "$(readlink -f "$0")")"
REQUIREMENTS="$HERE/../backend-py/requirements-linux.txt"

if [ ! -f "$REQUIREMENTS" ]; then
    echo "error: $REQUIREMENTS not found" >&2
    exit 1
fi

RUNTIME_DIR="$INSTALL_ROOT/runtime/python"
rm -rf "$RUNTIME_DIR"
mkdir -p "$INSTALL_ROOT/runtime"

TMP_TARBALL="$(mktemp -d)/pbs-python.tar.gz"
echo "--- downloading $PBS_TARBALL ---"
curl -sL -o "$TMP_TARBALL" "$PBS_URL"
tar -xzf "$TMP_TARBALL" -C "$INSTALL_ROOT/runtime"
rm -rf "$(dirname "$TMP_TARBALL")"

echo "--- installing backend-py dependencies ---"
"$RUNTIME_DIR/bin/pip" install -q -r "$REQUIREMENTS"

echo "--- verifying import ---"
( cd "$INSTALL_ROOT/backend-py" && CAROLINE_APP_ROOT="$INSTALL_ROOT" "$RUNTIME_DIR/bin/python3" -c 'from app.main import PORT, app; print("ok, PORT =", PORT)' )

echo "runtime/python provisioned at $RUNTIME_DIR"

CODEX_DIR="$INSTALL_ROOT/runtime/codex"
rm -rf "$CODEX_DIR"
mkdir -p "$CODEX_DIR"
CODEX_TMP="$(mktemp -d)"
echo "--- downloading $CODEX_ARCHIVE ---"
curl -sL -o "$CODEX_TMP/$CODEX_ARCHIVE" "$CODEX_URL"
echo "$CODEX_SHA256  $CODEX_TMP/$CODEX_ARCHIVE" | sha256sum -c -
tar -xzf "$CODEX_TMP/$CODEX_ARCHIVE" -C "$CODEX_DIR"
rm -rf "$CODEX_TMP"
"$CODEX_DIR/bin/codex-app-server" --version

echo "runtime/codex provisioned at $CODEX_DIR"
