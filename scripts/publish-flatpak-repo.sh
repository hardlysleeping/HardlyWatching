#!/usr/bin/env bash
# Publish the flatpak repository to GitHub Pages.
#
# The app is installed by clicking one link:
#
#   https://hardlysleeping.github.io/HardlyWatching/hardlywatching.flatpakref
#
# That file points at a signed OSTree repository served from the `gh-pages`
# branch of this repository. Nothing here touches the source release: the
# AppImage and DEB come from `packaging/sidecar` and tauri, this only builds the
# flatpak and republishes the branch.
#
#   ./scripts/publish-flatpak-repo.sh 0.3.2
#
# Requirements, all already done once on this machine:
#   - org.flatpak.Builder, org.gnome.Sdk//49, org.gnome.Platform//49 and
#     org.freedesktop.Sdk.Extension.rust-stable//25.08 in the *user* flatpak
#     installation, plus a user flathub remote for them.
#   - ~/apps/hardlywatching-flatpak-repo/gnupg: the repository signing key.
#     BACK THIS UP. Without it a published repository can never sign an update
#     again, and every installed copy has to be uninstalled and re-added by hand.
#
# Env: FLATPAK_SYSTEM_DIR is overridden because org.flatpak.Builder's sandbox
# only sees the system installation, and the SDK lives in the user one.
set -euo pipefail

VERSION="${1:?usage: $0 <version, e.g. 0.3.2>}"
TAG="v${VERSION}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${HOME}/apps/hardlywatching-flatpak-repo"
REPO="${WORK}/repo-public"
GPG_HOME="${WORK}/gnupg"
BUILD="${WORK}/build"
OUT="${WORK}/publish"
PAGES_URL="https://hardlysleeping.github.io/HardlyWatching"

export GNUPGHOME="${GPG_HOME}"
KEY="$(gpg --list-keys --with-colons | awk -F: '/^fpr/{print $10; exit}')"
[ -n "${KEY}" ] || { echo "no signing key in ${GPG_HOME}" >&2; exit 1; }

echo "== the tag has to exist: the manifest fetches the tree from it"
git -C "${ROOT}" rev-parse "${TAG}" >/dev/null

echo "== build ${TAG}"
rm -rf "${BUILD}"
flatpak run \
  --env=FLATPAK_SYSTEM_DIR="${HOME}/.local/share/flatpak" \
  --command=flatpak-builder org.flatpak.Builder \
  --user --force-clean --disable-rofiles-fuse \
  --repo="${WORK}/repo-staging" --state-dir="${WORK}/state" \
  "${BUILD}" "${ROOT}/flatpak/io.github.hardlysleeping.HardlyWatching.json"

echo "== export into the published repository as branch stable"
# build-export, not the builder's own repo: that one also carries a Debug
# runtime nobody needs to download (~30 MB of symbols).
flatpak build-export --disable-fsync "${REPO}" "${BUILD}" stable
flatpak build-sign --gpg-sign="${KEY}" "${REPO}" \
  io.github.hardlysleeping.HardlyWatching stable
flatpak build-update-repo --gpg-sign="${KEY}" --title='HardlyWatching' \
  --default-branch=stable --prune "${REPO}"

echo "== stage the Pages tree"
rm -rf "${OUT}"
mkdir -p "${OUT}"
cp -a "${REPO}/." "${OUT}/"
rm -rf "${OUT}/tmp" "${OUT}/.lock"
gpg --export --armor "${KEY}" > "${WORK}/hardlywatching.gpg.asc"
gpg --export "${KEY}" | base64 -w0 > "${WORK}/hardlywatching.gpg.b64"
KEYB64="$(cat "${WORK}/hardlywatching.gpg.b64")"
cat > "${OUT}/hardlywatching.flatpakref" <<REF
[Flatpak Ref]
Title=HardlyWatching
Name=io.github.hardlysleeping.HardlyWatching
Branch=stable
Url=${PAGES_URL}/
RuntimeRepo=https://dl.flathub.org/repo/flathub.flatpakrepo
IsRuntime=false
GPGKey=${KEYB64}
Comment=Download queue and viewer for Instagram and TikTok
REF
cat > "${OUT}/index.html" <<'HTML'
<!doctype html>
<meta charset="utf-8">
<title>HardlyWatching — downloads</title>
<style>body{font:16px/1.55 system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1.2rem;color:#222;background:#fff}
a.btn{display:inline-block;padding:.7rem 1.2rem;background:#2b6cb0;color:#fff;border-radius:8px;text-decoration:none;font-weight:600}
code{background:#f2f2f2;padding:.15rem .35rem;border-radius:4px;font-size:.95em}@media(prefers-color-scheme:dark){body{background:#111;color:#eee}code{background:#222}}</style>
<h1>HardlyWatching</h1>
<p>This is the flatpak repository for
<a href="https://github.com/hardlysleeping/HardlyWatching">HardlyWatching</a>.
It holds the packages only — the app's source, releases and screenshots are on GitHub.</p>
<p><a class="btn" href="hardlywatching.flatpakref">Install HardlyWatching</a></p>
<p>That link opens your software installer. On a terminal it is:</p>
<p><code>flatpak install https://hardlysleeping.github.io/HardlyWatching/hardlywatching.flatpakref</code></p>
<p>Updates arrive with <code>flatpak update</code>. The app needs the
<code>org.gnome.Platform//49</code> runtime from Flathub, which the installer fetches automatically.</p>
HTML
touch "${OUT}/.nojekyll"

echo "== publish to gh-pages"
cd "${OUT}"
git init -q
git checkout -q -B gh-pages
git add -A
git -c user.name='hardlysleeping' \
    -c user.email='67934624+hardlysleeping@users.noreply.github.com' \
    commit -q -m "Flatpak repository: ${VERSION}, signed"
git remote add origin git@github.com:hardlysleeping/HardlyWatching.git 2>/dev/null || \
  git remote set-url origin git@github.com:hardlysleeping/HardlyWatching.git
git push --force origin gh-pages

echo "== also keep the committed copy of the ref in the source tree"
cp "${OUT}/hardlywatching.flatpakref" "${ROOT}/flatpak/hardlywatching.flatpakref"
echo
echo "Done. Pages builds in a minute; then:"
echo "  curl -sI ${PAGES_URL}/summary.idx | head -1"
echo "  flatpak install -y ${PAGES_URL}/hardlywatching.flatpakref"
echo "Remember to bump the tag in flatpak/*.json in the same commit as VERSION."
