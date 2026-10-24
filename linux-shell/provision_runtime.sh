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

# Added 2026-10-21 (Linux parity pass, messenger-integrations testing plan):
# the WhatsApp/Messenger sidecars (whatsapp_channel.py/messenger_channel.py,
# Node/Baileys and fca-unofficial respectively) need a Node.js runtime, and
# Signal (signal_channel.py) needs signal-cli. Neither was provisioned here
# before -- out of scope is standing up real LOGGED-IN accounts for any of
# these (separate, not-yet-done work), but the interpreter/binary itself
# must exist for the sidecar process to even start.
NODE_VERSION="22.23.4"
NODE_ARCHIVE="node-v${NODE_VERSION}-linux-x64.tar.xz"
NODE_URL="https://nodejs.org/dist/v${NODE_VERSION}/${NODE_ARCHIVE}"
NODE_SHA256="1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce"

# signal-cli's own "-Linux-native" build (GraalVM native-image) is fully
# self-contained -- no separate JRE to provision/pin, unlike the plain
# cross-platform .tar.gz distribution which needs Java on PATH. Checksum
# computed directly against the downloaded release asset (signal-cli
# doesn't publish a SHA256SUMS file the way Node/Codex do).
SIGNAL_CLI_VERSION="0.14.9"
SIGNAL_CLI_ARCHIVE="signal-cli-${SIGNAL_CLI_VERSION}-Linux-native.tar.gz"
SIGNAL_CLI_URL="https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/${SIGNAL_CLI_ARCHIVE}"
SIGNAL_CLI_SHA256="a8fab78b5e8053abf3db5725272957ad11d1745b9a16848ddb2ace4743dfa5d5"

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

echo "--- fetching playwright's own Chromium build (app_browser_cdp.py / _linux_app_browser.py) ---"
"$RUNTIME_DIR/bin/python3" -m playwright install chromium

# Caroline's own web-search browser (app/web_browser.py, 2026-10-23): the full Chromium of
# the revision this Playwright expects (its "new" headless mode -- the separate headless shell
# is turned away by search engines, seen live 2026-10-24), bundled INSIDE the install (not the
# user's cache), launched by its explicit path from $INSTALL_ROOT/browsers.
echo "--- bundling the Chromium of Caroline's own web search (app/web_browser.py) ---"
PLAYWRIGHT_BROWSERS_PATH="$INSTALL_ROOT/browsers" "$RUNTIME_DIR/bin/python3" -m playwright install --no-shell chromium

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

NODE_DIR="$INSTALL_ROOT/runtime/node"
rm -rf "$NODE_DIR"
mkdir -p "$NODE_DIR"
NODE_TMP="$(mktemp -d)"
echo "--- downloading $NODE_ARCHIVE ---"
curl -sL -o "$NODE_TMP/$NODE_ARCHIVE" "$NODE_URL"
echo "$NODE_SHA256  $NODE_TMP/$NODE_ARCHIVE" | sha256sum -c -
tar -xJf "$NODE_TMP/$NODE_ARCHIVE" -C "$NODE_DIR" --strip-components=1
rm -rf "$NODE_TMP"
"$NODE_DIR/bin/node" --version

echo "runtime/node provisioned at $NODE_DIR"

SIGNAL_CLI_DIR="$INSTALL_ROOT/runtime/signal-cli"
rm -rf "$SIGNAL_CLI_DIR"
mkdir -p "$SIGNAL_CLI_DIR"
SIGNAL_CLI_TMP="$(mktemp -d)"
echo "--- downloading $SIGNAL_CLI_ARCHIVE ---"
curl -sL -o "$SIGNAL_CLI_TMP/$SIGNAL_CLI_ARCHIVE" "$SIGNAL_CLI_URL"
echo "$SIGNAL_CLI_SHA256  $SIGNAL_CLI_TMP/$SIGNAL_CLI_ARCHIVE" | sha256sum -c -
tar -xzf "$SIGNAL_CLI_TMP/$SIGNAL_CLI_ARCHIVE" -C "$SIGNAL_CLI_DIR"
rm -rf "$SIGNAL_CLI_TMP"
chmod +x "$SIGNAL_CLI_DIR/signal-cli"
"$SIGNAL_CLI_DIR/signal-cli" --version

echo "runtime/signal-cli provisioned at $SIGNAL_CLI_DIR"
