#!/usr/bin/env bash
# Build the self-contained HardlyWatching sidecar: dist/sidecar/
#
#   bash packaging/sidecar/build.sh
#
# What it does, in order:
#   1. makes .venv-build (a build-only venv, never the app's .venv) with
#      PyInstaller + Pillow + pillow-heif
#   2. runs PyInstaller with packaging/sidecar/hardlywatching.spec -> dist/sidecar
#   3. copies the external tools the app shells out to into dist/sidecar/bin/
#   4. writes dist/sidecar/bin/PROVENANCE.txt (where each file came from) and
#      prints the bundle size
#
# Idempotent: rerunning reuses .venv-build, wipes and rebuilds dist/sidecar.
# Nothing here reads or writes the live app (~/.config/hardlywatching, ~/instagram)
# or the running service on 127.0.0.1:8765.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BUILD_VENV="$ROOT/.venv-build"
DIST="$ROOT/dist/sidecar"
WORK="$BUILD_VENV/pyinstaller-work"
SPEC="$HERE/hardlywatching.spec"

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for cand in /usr/bin/python3 python3; do
    if command -v "$cand" >/dev/null 2>&1; then PYTHON="$(command -v "$cand")"; break; fi
  done
fi
[ -n "$PYTHON" ] || { echo "build.sh: no python3 found; set PYTHON=/path/to/python3" >&2; exit 1; }

say() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

say "repo    $ROOT"
say "python  $PYTHON ($("$PYTHON" -V 2>&1))"

# ---------------------------------------------------------------------------
# 1. build venv
# ---------------------------------------------------------------------------
if [ ! -x "$BUILD_VENV/bin/python" ]; then
  say "creating $BUILD_VENV"
  "$PYTHON" -m venv "$BUILD_VENV"
else
  say "reusing $BUILD_VENV"
fi
VENV_PY="$BUILD_VENV/bin/python"

# Install only what is missing, so a second run makes no network calls.
if ! "$VENV_PY" - <<'PY' >/dev/null 2>&1
import PyInstaller, PIL
PY
then
  say "installing pyinstaller + Pillow into .venv-build"
  "$VENV_PY" -m pip install --disable-pip-version-check --quiet \
    --upgrade pip pyinstaller Pillow
fi

# pillow-heif is optional for the app (engine.ensure_pillow_formats tolerates
# its absence), so a failure here is a warning, not a failed build.
if "$VENV_PY" -c "import pillow_heif" >/dev/null 2>&1; then
  say "pillow-heif present"
else
  say "installing pillow-heif (HEIC thumbnails)"
  "$VENV_PY" -m pip install --disable-pip-version-check --quiet pillow-heif \
    || warn "pillow-heif install failed: HEIC photos will not thumbnail in the bundle"
fi

# instaloader is bundled as a library, and re-exposed as the program the app
# shells out to (see the wrapper written in step 3). It is pure Python with one
# dependency (requests), so this is a few MB, not another runtime.
IL_OK=0
if "$VENV_PY" -c "import instaloader" >/dev/null 2>&1; then
  IL_OK=1
  say "instaloader $( "$VENV_PY" -c 'import instaloader; print(instaloader.__version__)' ) present"
else
  say "installing instaloader into .venv-build"
  if "$VENV_PY" -m pip install --disable-pip-version-check --quiet instaloader; then
    IL_OK=1
  else
    warn "instaloader install failed: Instagram downloads will not work in this bundle"
  fi
fi

say "pyinstaller $( "$VENV_PY" -c 'import PyInstaller; print(PyInstaller.__version__)' )"

# ---------------------------------------------------------------------------
# 2. PyInstaller
# ---------------------------------------------------------------------------
say "running PyInstaller (onedir, this takes a minute)"
rm -rf "$DIST" "$WORK"
HWA_ROOT="$ROOT" "$VENV_PY" -m PyInstaller \
  --noconfirm \
  --clean \
  --log-level WARN \
  --distpath "$ROOT/dist" \
  --workpath "$WORK" \
  "$SPEC"

[ -x "$DIST/hardlywatching-server" ] || { echo "build.sh: PyInstaller produced no exe" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 3. external tools -> dist/sidecar/bin/
# ---------------------------------------------------------------------------
BIN="$DIST/bin"
mkdir -p "$BIN"
PROV="$BIN/PROVENANCE.txt"
{
  echo "# HardlyWatching sidecar -- external tools, and where this build found them."
  echo "# generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ') on $(uname -srm)"
  echo "# copied-by: packaging/sidecar/build.sh"
  echo
} >"$PROV"

# is_elf: a copy is portable only if it is a real executable, not a #! script
# pointing at one machine's interpreter (a pipx console script, for example).
is_elf() { [ -f "$1" ] && [ "$(head -c 4 "$1" | od -An -tx1 | tr -d ' \n')" = "7f454c46" ]; }

# --- yt-dlp: must be the standalone build; a pipx console script is a python
# --- script with an absolute shebang and is useless inside a bundle.
YTDLP=""
for cand in \
  "${YTDLP_BIN:-}" \
  "$(command -v yt-dlp 2>/dev/null || true)" \
  "/usr/local/bin/yt-dlp" \
  "/usr/bin/yt-dlp"
do
  [ -n "${cand:-}" ] || continue
  if is_elf "$cand"; then YTDLP="$cand"; break; fi
  [ -f "$cand" ] && warn "skipping $cand: not a standalone executable (script, needs its own python)"
done
if [ -z "$YTDLP" ]; then
  warn "no standalone yt-dlp found; TikTok downloads will fail unless the host has one."
  warn "set YTDLP_BIN=/path/to/yt-dlp_linux and rerun."
fi

# --- ffmpeg / ffprobe: the host's own binaries.
FFMPEG="${FFMPEG_BIN:-$(command -v ffmpeg 2>/dev/null || true)}"
FFPROBE="${FFPROBE_BIN:-$(command -v ffprobe 2>/dev/null || true)}"
[ -n "$FFMPEG" ] || warn "no ffmpeg on PATH; thumbnails and clip transcoding will fail."
if [ -z "$FFPROBE" ]; then
  warn "no ffprobe on PATH; the bundle will ship without it (optional for the app)."
fi

copy_tool() {
  local src="$1" name="$2" desc="$3"
  [ -n "$src" ] || return 0
  install -m 0755 "$src" "$BIN/$name"
  local ver sha
  ver="$("$BIN/$name" --version 2>&1 | head -n 1 || true)"
  case "$name" in ffmpeg|ffprobe) ver="$("$BIN/$name" -version 2>&1 | head -n 1 || true)";; esac
  sha="$(sha256sum "$BIN/$name" | cut -d' ' -f1)"
  local link=""
  [ -L "$src" ] && link=" (symlink -> $(readlink -f "$src"))"
  printf '%s\n  source : %s%s\n  version: %s\n  sha256 : %s\n  note   : %s\n\n' \
    "$BIN/$name" "$src" "$link" "$ver" "$sha" "$desc" >>"$PROV"
  say "bundled $name <- $src"
}

# --- instaloader: not a copy of anything. This wrapper re-enters the bundle's
# --- own binary, so the bundled instaloader package runs on the bundled
# --- CPython. engine.py runs whatever `instaloader` resolves to, so the name
# --- and the argv passthrough are the whole contract (exit code included).
if [ "$IL_OK" = 1 ]; then
  cat >"$BIN/instaloader" <<'SH'
#!/bin/sh
# instaloader, from the HardlyWatching bundle. Not a separate install: this
# hands the arguments to the bundle's own interpreter (--run-instaloader), so
# Instagram downloads work on a machine with no Python and no pipx.
here=$(dirname "$(readlink -f "$0")")
exec "$here/../hardlywatching-server" --run-instaloader "$@"
SH
  chmod 0755 "$BIN/instaloader"
  printf '%s\n  source : written by packaging/sidecar/build.sh (not copied)\n  version: %s (bundled library)\n  note   : %s\n\n' \
    "$BIN/instaloader" \
    "$( "$VENV_PY" -c 'import instaloader; print(instaloader.__version__)' )" \
    "Shell wrapper -> ../hardlywatching-server --run-instaloader. Runs instaloader inside the frozen interpreter." \
    >>"$PROV"
  say "wrote bin/instaloader -> hardlywatching-server --run-instaloader"
fi

copy_tool "$YTDLP" "yt-dlp" \
  "Standalone PyInstaller build of yt-dlp (its own python inside). Runs on any glibc x86_64 host."
copy_tool "$FFMPEG" "ffmpeg" \
  "Host ffmpeg. DYNAMICALLY linked against the host's shared FFmpeg libs -- see docs/sidecar.md."
copy_tool "$FFPROBE" "ffprobe" \
  "Host ffprobe, same dynamic-linking caveat as ffmpeg."

# ---------------------------------------------------------------------------
# 4. report
# ---------------------------------------------------------------------------
say "laid out $DIST"
find "$DIST" -maxdepth 1 -mindepth 1 -printf '    %-24f %10s\n' 2>/dev/null | sort || true
echo
say "bundle size"
du -sh "$DIST" | awk '{printf "    %-24s %s\n", "dist/sidecar", $1}'
for part in "$DIST/hardlywatching-server" "$DIST/_internal" "$DIST/bin"; do
  [ -e "$part" ] && du -sh "$part" | awk -v n="$(basename "$part")" '{printf "      %-22s %s\n", n, $1}'
done
printf '    %-24s %s bytes\n' "total (du -sb)" "$(du -sb "$DIST" | cut -f1)"
echo
say "next:  dist/sidecar/hardlywatching-server --self-check"
