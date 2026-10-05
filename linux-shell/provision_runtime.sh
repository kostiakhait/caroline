#!/bin/bash
# Builds runtime/python for a Linux Caroline install: a self-contained
# CPython (via astral-sh/python-build-standalone) with backend-py's pinned
# dependencies installed into it. Must run ON Linux (downloads and executes
# a Linux CPython build) -- unlike dist-linux in the Makefile, which is pure
# file copying and works from any host.
#
# Usage: provision_runtime.sh <install_root>
#   <install_root> is a dist-linux output directory (or an installed
#   CAROLINE_APP_ROOT) -- the one that already has backend-py/ in it.
# Result: <install_root>/runtime/python/bin/python3 with every package in
# requirements-linux.txt installed.
set -euo pipefail

PBS_RELEASE="20261003"
PBS_PYTHON_VERSION="3.12.15"
PBS_TARBALL="cpython-${PBS_PYTHON_VERSION}+${PBS_RELEASE}-x86_64-unknown-linux-gnu-install_only.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_TARBALL}"

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
