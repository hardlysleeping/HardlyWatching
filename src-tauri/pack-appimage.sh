#!/usr/bin/env bash
# Assemble HardlyWatching_<version>_<arch>.AppImage — runs INSIDE the build container,
# with the working directory at /src/src-tauri.
#
# Why this is not `cargo tauri build --bundles appimage`:
#
# Tauri's AppImage step runs linuxdeploy, which deploys the dependencies of every ELF
# it finds in the AppDir. The frozen Python server is a tree of ELFs whose shared
# libraries are *renamed with a hash by PyInstaller* (libheif-c26addd2.so.1.23.2,
# libavif-8a7f9d56.so.16.4.2, ...). Those only resolve through the bootloader's own
# LD_LIBRARY_PATH at run time, so linuxdeploy cannot resolve them and aborts with
# "ERROR: Could not find dependency: libheif-<hash>.so"; there is no option to skip a
# directory, `--exclude-library` only silences one hashed name per run, and Tauri
# offers no way to pass extra linuxdeploy arguments. Installing ffmpeg-free in the
# build image fixes the *other* stop (bin/ffmpeg's libavdevice.so.62) but not this.
#
# So the AppImage is packaged directly with appimagetool: the payload is the same tree
# the .deb installs to /usr/lib/HardlyWatching/sidecar, and nothing is patched or
# rewritten. The trade-off, stated plainly: linuxdeploy's library deployment is
# skipped, so GTK 3 / WebKitGTK 4.1 come from the host, exactly as Tauri documents
# for AppImages (webkit2gtk is not relocatable anyway).
set -euo pipefail

SRC=${SRC:-/src}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$HERE"

VERSION=$(sed -n 's/^ *"version": *"\([^"]*\)".*/\1/p' tauri.conf.json | head -n 1)
[ -n "$VERSION" ] || { echo "pack-appimage: no version in tauri.conf.json" >&2; exit 1; }
PRODUCT=HardlyWatching
case "$(uname -m)" in
  x86_64)  APPDIR_ARCH=x86_64; PKG_ARCH=amd64 ;;
  aarch64) APPDIR_ARCH=aarch64; PKG_ARCH=arm64 ;;
  *) echo "pack-appimage: unsupported machine $(uname -m)" >&2; exit 2 ;;
esac

SIDECAR=$SRC/dist/sidecar
[ -x "$SIDECAR/hardlywatching-server" ] || {
  echo "pack-appimage: $SIDECAR/hardlywatching-server is missing (run packaging/sidecar/build.sh)" >&2
  exit 1
}

echo "==> cargo build --release"
cargo build --release

APPDIR=target/release/bundle/appimage/$PRODUCT.AppDir
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" \
         "$APPDIR/usr/lib/$PRODUCT" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/128x128/apps"

install -m 755 target/release/hardlywatching "$APPDIR/usr/bin/hardlywatching"
echo "==> bundling the frozen server from $SIDECAR ($(du -sh "$SIDECAR" | cut -f1))"
cp -a "$SIDECAR" "$APPDIR/usr/lib/$PRODUCT/sidecar"

install -m 644 icons/128x128.png "$APPDIR/hardlywatching.png"
install -m 644 icons/128x128.png "$APPDIR/usr/share/icons/hicolor/128x128/apps/hardlywatching.png"
ln -sf hardlywatching.png "$APPDIR/.DirIcon"

# Same content Tauri generates for the .deb, so the two bundles present one app.
cat > "$APPDIR/hardlywatching.desktop" <<'DESKTOP'
[Desktop Entry]
Categories=Utility;
Comment=HardlyWatching desktop shell
Exec=hardlywatching
Icon=hardlywatching
Name=HardlyWatching
StartupWMClass=hardlywatching
Terminal=false
Type=Application
X-AppImage-Name=HardlyWatching
DESKTOP
install -m 644 "$APPDIR/hardlywatching.desktop" "$APPDIR/usr/share/applications/hardlywatching.desktop"

# The AppImage runtime executes $APPDIR/AppRun and exports APPDIR; main.rs uses both.
ln -sf usr/bin/hardlywatching "$APPDIR/AppRun"

TOOL=/tmp/appimagetool
if [ ! -x "$TOOL" ]; then
  echo "==> downloading appimagetool"
  curl -sSL -o "$TOOL" \
    https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$TOOL"
fi

OUT=target/release/bundle/appimage/${PRODUCT}_${VERSION}_${PKG_ARCH}.AppImage
rm -f "$OUT"
echo "==> appimagetool $APPDIR -> $OUT"
ARCH=$APPDIR_ARCH VERSION=$VERSION APPIMAGE_EXTRACT_AND_RUN=1 \
  "$TOOL" --no-appstream "$APPDIR" "$OUT"
chmod 755 "$OUT"
echo "==> $OUT ($(du -h "$OUT" | cut -f1), $(du -sb "$OUT" | cut -f1) bytes)"
