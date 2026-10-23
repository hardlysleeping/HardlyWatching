#!/usr/bin/env bash
# Build the HardlyWatching desktop shell in a throwaway container.
#
# Why: the Tauri/Linux build needs the WebKitGTK + GTK + libsoup *development*
# headers. Bazzite is an immutable Fedora desktop with no -devel packages and no
# rpm-ostree layering allowed, so the build runs in a container instead of on the
# host. Nothing is installed outside podman's storage.
#
# Usage:
#   src-tauri/build-in-container.sh build            # cargo build (debug)
#   src-tauri/build-in-container.sh deb              # release + .deb bundle
#   src-tauri/build-in-container.sh appimage         # release + AppImage bundle
#   src-tauri/build-in-container.sh shell            # interactive shell in the image
#
# Artifacts land in src-tauri/target/ (git-ignored) because that directory is
# mounted into the container. A second run is fast: the cargo registry and the
# cargo/tauri CLI live in the named volume hardlywatching-cargo.
set -euo pipefail

IMAGE=${IMAGE:-docker.io/library/fedora:latest}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WHAT=${1:-build}
mkdir -p "$REPO/src-tauri/target"

case "$WHAT" in
  build)    CMD="cargo build" ;;
  deb)      CMD="cargo tauri build --bundles deb" ;;
  appimage) CMD="cargo tauri build --bundles appimage" ;;
  shell)    CMD="bash" ;;
  *) echo "unknown target '$WHAT' (build|deb|appimage|shell)" >&2; exit 2 ;;
esac

TTY=()
[ -t 0 ] && TTY=(-it)

exec podman run --rm "${TTY[@]}" --network=host --security-opt label=disable \
  --workdir /src/src-tauri \
  -v "$REPO":/src:z \
  -v hardlywatching-cargo:/cargo \
  -e CARGO_HOME=/cargo \
  -e CARGO_TARGET_DIR=/src/src-tauri/target \
  -e PATH=/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  "$IMAGE" bash -lc '
    set -e
    if ! command -v cargo >/dev/null; then
      dnf install -y --setopt=install_weak_deps=False \
        cargo rust gcc gcc-c++ make pkgconf-pkg-config \
        webkit2gtk4.1-devel javascriptcoregtk4.1-devel gtk3-devel libsoup3-devel \
        openssl-devel libappindicator-gtk3-devel librsvg2-devel patchelf file which git tar xz
    fi
    if ! command -v cargo-tauri >/dev/null; then
      cargo install tauri-cli --version "^2" --locked
    fi
    '"$CMD"'
  '
