#!/usr/bin/env bash
# Build the HardlyWatching desktop shell in a throwaway container.
#
# Why: the Tauri/Linux build needs the WebKitGTK + GTK + libsoup *development*
# headers. An immutable Fedora desktop has no -devel packages and no
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
#
# deb/appimage also need the frozen Python server: tauri.conf.json ships it as a
# bundle resource ("../dist/sidecar" -> <prefix>/lib/HardlyWatching/sidecar). If
# dist/sidecar is missing this script builds it first with
# packaging/sidecar/build.sh, run on the HOST with the host's python3 so the
# frozen bundle links against the host's glibc and not the container's. Set
# HWA_SKIP_SIDECAR=1 to bundle whatever is already there and nothing else.
set -euo pipefail

IMAGE=${IMAGE:-docker.io/library/fedora:latest}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WHAT=${1:-build}
mkdir -p "$REPO/src-tauri/target"

case "$WHAT" in
  build)    CMD="cargo build" ;;
  deb)      CMD="cargo tauri build --bundles deb" ;;
  # Tauri's own appimage step runs linuxdeploy, which cannot resolve the hashed
  # private libraries of the frozen server and aborts. pack-appimage.sh packages
  # the same payload with appimagetool instead; read the comment at its top.
  appimage) CMD="bash /src/src-tauri/pack-appimage.sh" ;;
  shell)    CMD="bash" ;;
  *) echo "unknown target '$WHAT' (build|deb|appimage|shell)" >&2; exit 2 ;;
esac

# The bundled server is a frozen Python tree; it is copied into both bundles as-is.
# The AppImage needs no linuxdeploy pass at all (see pack-appimage.sh), so nothing
# here has to work around the tree's hashed private libraries.

# The bundle resource is the frozen server, so it has to exist before Tauri looks
# for it. Build it on the host once (same result a release build would get).
if [ "$WHAT" = deb ] || [ "$WHAT" = appimage ]; then
  if [ ! -x "$REPO/dist/sidecar/hardlywatching-server" ]; then
    if [ "${HWA_SKIP_SIDECAR:-0}" = 1 ]; then
      echo "dist/sidecar/hardlywatching-server is missing (HWA_SKIP_SIDECAR=1)" >&2
      exit 2
    fi
    echo "==> dist/sidecar is missing: building the frozen server on the host first"
    echo "    (packaging/sidecar/build.sh; HWA_SKIP_SIDECAR=1 skips this)"
    ( cd "$REPO" && bash packaging/sidecar/build.sh )
  fi
  echo "==> bundling the frozen server from dist/sidecar ($(du -sh "$REPO/dist/sidecar" | cut -f1))"
fi

# The bundle version comes from VERSION, the one number this repo keeps (upstream
# keeps its version in the tag; the frozen server reads VERSION for /api/version and
# tauri.conf.json copies it). Derive it here so a DEB can never ship a stale number
# just because someone forgot to edit a second file.
VERSION_FILE="$REPO/VERSION"
if [ -f "$VERSION_FILE" ]; then
  WANT=$(tr -d '[:space:]' < "$VERSION_FILE")
  HAVE=$(sed -n 's/^ *"version": *"\([^"]*\)".*/\1/p' "$REPO/src-tauri/tauri.conf.json" | head -n 1)
  if [ -n "$WANT" ] && [ "$WANT" != "$HAVE" ]; then
    echo "==> tauri.conf.json version $HAVE -> $WANT (from VERSION)"
    python3 - "$REPO/src-tauri/tauri.conf.json" "$WANT" <<'PY'
import json, sys
path, want = sys.argv[1], sys.argv[2]
with open(path) as fh:
    conf = json.load(fh)
conf["version"] = want
with open(path, "w") as fh:
    json.dump(conf, fh, indent=2)
    fh.write("\n")
PY
fi
# Crate metadata, for tidiness: nothing reads it, but a stale one is a lie in `cargo tree`.
if [ -n "$WANT" ] && ! grep -q "^version = \"$WANT\"" "$REPO/src-tauri/Cargo.toml"; then
sed -i "0,/^version = \"[^\"]*\"/s//version = \"$WANT\"/" "$REPO/src-tauri/Cargo.toml"
fi
fi

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
    # linuxdeploy (AppImage) and appimagetool shell out to these; the .deb bundler needs none of them.
    if ! command -v mksquashfs >/dev/null || ! command -v desktop-file-validate >/dev/null; then
      dnf install -y --setopt=install_weak_deps=False \
        squashfs-tools desktop-file-utils fuse fuse-libs zsync bsdtar
    fi
    '"$CMD"'
  '
