#!/bin/bash
# Wraps a dist-linux + provision_runtime.sh output into a real .AppImage.
# Must run ON Linux -- appimagetool is a Linux ELF binary -- and assumes
# <install_root> already has runtime/python and runtime/codex provisioned
# (see provision_runtime.sh; this script refuses to run without them, same
# shape as that script's own missing-REQUIREMENTS check).
#
# This captures, as a reusable/repeatable script, the AppImage mechanism
# already proven manually on a real test box (2026-10-06, see
# docs/LINUX_PORT_PLAN.md's Phase 5 section): launched, resolved every
# path correctly, and rendered the splash screen. It was never committed
# as a script before this.
#
# Produces an AppImage that still depends on the HOST having python3-gi/
# gir1.2-webkit2-4.1/GTK3/WebKitGTK installed -- it does NOT bundle those
# shared libraries into the AppDir. A fully self-contained AppImage (one
# that works on a host with none of those packages) would need
# linuxdeploy's GTK/webkit plugins to pull in and rewrite the library
# search paths for the whole GTK3/WebKitGTK stack -- flagged in the port
# plan as "more involved than a typical AppImage, worth validating early
# rather than assuming it'll 'just work'", and NOT attempted here; this
# script only automates the narrower, already-validated mechanism.
#
# Usage: build_appimage.sh <install_root> <output_appimage_path>

set -euo pipefail

APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <install_root> <output_appimage_path>" >&2
    exit 1
fi

INSTALL_ROOT="$(readlink -f "$1")"
OUTPUT_DIR="$(readlink -f "$(dirname "$2")")"
OUTPUT_PATH="$OUTPUT_DIR/$(basename "$2")"
HERE="$(dirname "$(readlink -f "$0")")"

if [ ! -d "$INSTALL_ROOT/runtime/python" ] || [ ! -d "$INSTALL_ROOT/runtime/codex" ]; then
    echo "error: $INSTALL_ROOT/runtime/{python,codex} not found -- run provision_runtime.sh first:" >&2
    echo "    linux-shell/provision_runtime.sh $INSTALL_ROOT" >&2
    exit 1
fi

WORK_DIR="$(mktemp -d)"
APPDIR="$WORK_DIR/Caroline.AppDir"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "--- assembling AppDir at $APPDIR ---"
mkdir -p "$APPDIR"
# $INSTALL_ROOT already has linux-shell/, wwwroot/, backend-py/, runtime/
# as siblings (dist-linux's own layout) -- copied as-is, so
# caroline_shell.py's own path resolution (HERE.parent, _default_wwwroot)
# needs no AppImage-specific changes to work inside the AppDir.
cp -r "$INSTALL_ROOT/." "$APPDIR/"
find "$APPDIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

cp "$HERE/AppRun" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"

# AppImage's own top-level-desktop-file convention: Exec= names the
# AppDir's own entry point (AppRun), not the @INSTALL_DIR@-templated
# absolute path caroline.desktop uses for a plain, non-AppImage install
# (see that file's own header) -- substituted here, not by editing the
# shared source file.
sed 's#^Exec=.*#Exec=AppRun#' "$HERE/caroline.desktop" > "$APPDIR/caroline.desktop"
# AppImage's own icon convention: a <name>.png matching Icon= in the
# .desktop file, at the AppDir root (see linux-shell/icons/caroline.png's
# own generation, build_appimage.sh is not what creates it).
cp "$HERE/icons/caroline.png" "$APPDIR/caroline.png"

APPIMAGETOOL="$HERE/.cache/appimagetool.AppImage"
if [ ! -x "$APPIMAGETOOL" ]; then
    mkdir -p "$HERE/.cache"
    echo "--- downloading appimagetool ---"
    curl -sL -o "$APPIMAGETOOL" "$APPIMAGETOOL_URL"
    chmod +x "$APPIMAGETOOL"
fi

echo "--- running appimagetool ---"
mkdir -p "$OUTPUT_DIR"
# Per docs/LINUX_PORT_PLAN.md's own test-box finding: FUSE is available on
# a normal box (appimagetool, itself an AppImage, just runs), but
# --appimage-extract-and-run is the documented fallback where it isn't
# (containers, some CI images) -- tried second, not first, since it's
# slower (extracts the whole tool to a temp dir every run).
if ! ARCH=x86_64 "$APPIMAGETOOL" "$APPDIR" "$OUTPUT_PATH" 2>"$WORK_DIR/appimagetool.err"; then
    echo "appimagetool's normal run failed (no FUSE available?), retrying with --appimage-extract-and-run:" >&2
    cat "$WORK_DIR/appimagetool.err" >&2
    ARCH=x86_64 "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUTPUT_PATH"
fi

echo "AppImage built at $OUTPUT_PATH"
