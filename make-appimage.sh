#!/usr/bin/env bash
# Build the HardlyWatching AppImage.
#
#   ./make-appimage.sh
#
# The result is a small launcher AppImage: it carries the entry point, the icon
# and a menu installer, and runs the checkout's own code and venv (see
# packaging/appimage/AppRun for why). Rebuild it when this script, AppRun or the
# icon changes -- NOT when the app's Python code changes, because the AppImage
# reads that from the install every time it starts.
#
# Needs appimagetool, which is downloaded into ~/.cache on first use.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

DIST="$HERE/dist"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/hardlywatching-appimage"
TOOL="$CACHE/appimagetool-x86_64.AppImage"
URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
OUT="$DIST/HardlyWatching-x86_64.AppImage"

say() { printf '%s\n' "$*"; }

mkdir -p "$DIST" "$CACHE"

if [ ! -x "$TOOL" ]; then
  say "Fetching appimagetool..."
  curl -fL --retry 3 -o "$TOOL" "$URL"
  chmod +x "$TOOL"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
APPDIR="$WORK/AppDir"
mkdir -p "$APPDIR"

# Only the launcher bits go in. The app's code stays in the checkout, which is
# what the systemd unit that runs at login already uses.
install -m 755 packaging/appimage/AppRun "$APPDIR/AppRun"
install -m 644 packaging/appimage/hardlywatching.desktop "$APPDIR/hardlywatching.desktop"
install -m 644 icon-192.png "$APPDIR/hardlywatching.png"
cp "$APPDIR/hardlywatching.png" "$APPDIR/.DirIcon"

say "Building $OUT"
# appimagetool is itself an AppImage: run it without FUSE so this works in
# containers and on machines with libfuse2 missing.
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 \
  "$TOOL" "$APPDIR" "$OUT" >/dev/null

chmod +x "$OUT"
say ""
say "Built: $OUT  ($(du -h "$OUT" | cut -f1))"
say "Double-click it, or run: $OUT --install   (adds a menu entry)"
