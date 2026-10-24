"""Byte-identical duplicates in the archive.

Why this exists: an Instagram collab post is served from *every* collaborator's
profile, so instaloader writes the same carousel into two profile folders. The
bytes are identical, the paths are not, and nothing else in the app notices --
the gallery is per-profile by design, so both copies simply sit there. A repost
by a different account is a re-encode and will not match; this only finds real
copies.

Two rules shape the implementation:

* It never deletes. The scan is read-only, and the only removal path offered is
  the app's existing move-to-trash (see server.py), which is recoverable.
* It only compares files *within a platform*. The same photo on Instagram and
  TikTok is not a duplicate to be tidied away; the two archives are answers to
  two different questions.

The comparison is size first, hash second: a size is one stat, while a sha256 is
a full read of the file. Only sizes that occur more than once are ever hashed,
which on a real archive is a small fraction of the files.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

# Same sets the gallery and the TikTok importer use, plus the containers yt-dlp
# writes when a clip has no H.264 version. Kept as literals rather than imported
# from engine so this module stays standalone and testable without the app.
MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif",
    ".mp4", ".mov", ".webm", ".m4v", ".mkv",
}
# A duplicate has to be worth the read. Below this, identical files are usually
# a placeholder or an icon, not a post.
MIN_SIZE = 1024
# Directories that hold derived data or app state, never archive media.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "backups", "run", "logs",
    ".thumbnails", "thumbnails", "thumbs", ".cache",
}
MAX_GROUPS = 400
_HASH_CHUNK = 1 << 20  # 1 MiB


# Where the gallery keeps a post that has been moved out of the profile's main
# folder. Both live *inside* the profile folder (`<profile>/trash/<file>`), which
# is what move_media writes and what the gallery reads back.
SECTIONS = ("favorites", "trash")


def _section_and_profile(root: Path, path: Path) -> tuple[str, str, str]:
    """(platform, section, profile) for a file, from its place in the archive.

    The layout: `root/<profile>/<file>` for Instagram, `root/tiktok/<profile>/`
    for TikTok, and a section folder inside the profile for anything moved out of
    it (`root/<profile>/trash/<file>`). A top-level `trash/<profile>/<file>` is
    also recognised, because earlier builds wrote that and archives made with
    them still exist.
    """
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return "instagram", "main", ""
    if not parts:
        return "instagram", "main", ""
    platform = "tiktok" if parts[0] == "tiktok" else "instagram"
    rest = parts[1:] if platform == "tiktok" else parts
    if not rest:
        return platform, "main", ""
    if len(rest) >= 3 and rest[1] in SECTIONS:
        return platform, rest[1], rest[0]
    if rest[0] in SECTIONS and len(rest) >= 2:
        return platform, rest[0], rest[1]
    return platform, "main", rest[0]


def iter_media(roots):
    """Yield (root, path) for every candidate media file. Symlinks are skipped:
    a link is a pointer to a file counted elsewhere, and following it would
    report the same bytes under two names forever."""
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(
                d for d in dirnames
                if d.lower() not in SKIP_DIRS and not d.startswith(".")
            )
            base = Path(dirpath)
            for name in filenames:
                if name.startswith("."):
                    continue
                p = base / name
                if p.suffix.lower() not in MEDIA_EXTS:
                    continue
                yield root, p


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def scan(roots, platform: str | None = None, min_size: int = MIN_SIZE,
         on_progress=None, should_stop=None) -> dict:
    """Find groups of byte-identical files. Read-only.

    Returns {"groups": [...], "scanned": n, "hashed": n, "candidates": n,
             "errors": [...], "wasted": bytes, "stopped": bool}
    where each group is
    {"sha256", "size", "kind", "files": [{path, name, profile, platform,
     section, mtime}], "wasted"} and `wasted` counts the bytes recoverable by
    keeping one copy (size * (len(files) - 1)).
    """
    by_size: dict[int, list[tuple]] = {}
    scanned = 0
    errors: list[str] = []

    for root, path in iter_media(roots):
        plat, section, profile = _section_and_profile(root, path)
        if platform and plat != platform:
            continue
        try:
            st = path.stat()
        except OSError as exc:
            if len(errors) < 20:
                errors.append(f"{path}: {exc}")
            continue
        # A tombstone is a 0-byte placeholder that stops a re-download; it is
        # not a copy of anything, whatever else is 0 bytes.
        if not st.st_size or st.st_size < min_size:
            continue
        scanned += 1
        by_size.setdefault(st.st_size, []).append(
            (path, plat, section, profile, st.st_mtime))
        if on_progress and scanned % 500 == 0:
            on_progress(scanned, 0)

    candidates = sum(len(v) for v in by_size.values() if len(v) > 1)
    hashed = 0
    groups: dict[tuple, dict] = {}
    for size, items in by_size.items():
        if len(items) < 2:
            continue
        for path, plat, section, profile, mtime in items:
            if should_stop and should_stop():
                return _finish(groups, scanned, hashed, candidates, errors, True)
            try:
                # Re-stat: the file may have moved between the walk and now.
                digest = _sha256(path)
            except OSError as exc:
                if len(errors) < 20:
                    errors.append(f"{path}: {exc}")
                continue
            hashed += 1
            if on_progress and hashed % 25 == 0:
                on_progress(scanned, hashed)
            key = (plat, digest)
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    "sha256": digest,
                    "size": size,
                    "kind": "video" if path.suffix.lower() in
                            (".mp4", ".mov", ".webm", ".m4v", ".mkv") else "image",
                    "files": [],
                }
            g["files"].append({
                "path": str(path),
                "name": path.name,
                "profile": profile,
                "platform": plat,
                "section": section,
                "mtime": mtime,
            })
    return _finish(groups, scanned, hashed, candidates, errors, False)


def _finish(groups, scanned, hashed, candidates, errors, stopped) -> dict:
    out = []
    for g in groups.values():
        if len(g["files"]) < 2:
            continue
        g = dict(g)
        # Keep the oldest copy first: it is the one a person is most likely to
        # think of as "the original", and the UI pre-selects the others.
        g["files"] = sorted(g["files"], key=lambda f: (f["mtime"], f["path"]))
        # Only copies still in the profile's main folder are worth money: one
        # already moved to Trash is set aside on purpose, and counting it as
        # "recoverable" would inflate the total with work already done.
        in_main = sum(1 for f in g["files"] if f["section"] == "main")
        g["wasted"] = g["size"] * max(0, in_main - 1)
        out.append(g)
    out.sort(key=lambda g: (-g["wasted"], g["files"][0]["path"]))
    return {
        "groups": out[:MAX_GROUPS],
        "group_count": len(out),
        "scanned": scanned,
        "hashed": hashed,
        "candidates": candidates,
        "errors": errors,
        "wasted": sum(g["wasted"] for g in out),
        "stopped": stopped,
    }


class Scanner:
    """Runs a scan in a thread so the HTTP request that starts it can return.

    A 159k-file archive is minutes of stat() calls; doing that inside the
    request would time out in the browser and block the server's single thread.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.report: dict = {}
        self.state = {
            "running": False, "scanned": 0, "hashed": 0,
            "started": None, "finished": None, "error": None,
        }

    def start(self, roots, platform: str | None = None,
              min_size: int = MIN_SIZE) -> bool:
        with self._lock:
            if self.state["running"]:
                return False
            self._stop.clear()
            self.state = {
                "running": True, "scanned": 0, "hashed": 0,
                "started": time.time(), "finished": None, "error": None,
            }
            self.report = {}
        self._thread = threading.Thread(
            target=self._run, args=(list(roots), platform, min_size),
            name="dupe-scan", daemon=True)
        self._thread.start()
        return True

    def _run(self, roots, platform, min_size):
        def progress(scanned, hashed):
            with self._lock:
                self.state["scanned"] = scanned
                self.state["hashed"] = hashed
        try:
            result = scan(roots, platform=platform, min_size=min_size,
                          on_progress=progress, should_stop=self._stop.is_set)
            with self._lock:
                self.report = result
                self.state.update(running=False, finished=time.time(),
                                  scanned=result["scanned"],
                                  hashed=result["hashed"])
        except Exception as exc:  # a scan must never take the server down
            with self._lock:
                self.state.update(running=False, finished=time.time(),
                                  error=f"{type(exc).__name__}: {exc}")

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            out = dict(self.state)
            out["report"] = self.report
            return out
