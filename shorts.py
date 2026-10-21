"""
Find the short-form videos on disk for the /shorts feed.

Kept separate from the gallery walk because the needs differ: the gallery wants
every image and video with metadata sidecars, while the feed only wants things it
can play full-screen, and it wants them cheaply on every request.

Only files that actually exist are returned. That is what makes a clip that was
deleted from disk disappear from the feed without any bookkeeping.
"""

import json
import os
from pathlib import Path

VIDEO_EXTS = {'.mp4', '.mov', '.webm', '.m4v'}

# TikTok archives are kept in a sibling folder, always named this, and appear in
# feed paths under this prefix.
TIKTOK_DIR = "tiktok"


def _tiktok_candidates(root: Path) -> tuple[Path, ...]:
    """Where TikTok archives may live for an archive root, best first.

    The rule itself lives in engine.tiktok_roots so there is exactly one
    definition of it. It used to be written out in three separate places, and
    one of those copies (the gallery's profile scan) kept looking in the old
    location after the layout changed -- so TikTok profiles silently vanished
    from the gallery while everything else carried on working.

    Imported inside the function so this module stays importable on its own.
    """
    import engine
    return engine.tiktok_roots(root)


def _contained(base: Path, sub: str) -> Path | None:
    """`sub` inside `base`, or None if it escapes or is not a playable file."""
    try:
        anchor = Path(base).resolve()
        target = (anchor / sub).resolve()
    except OSError:
        return None
    try:
        target.relative_to(anchor)
    except ValueError:
        return None
    if not target.is_file():
        return None
    if target.suffix.lower() not in VIDEO_EXTS:
        return None
    return target


def directory_for(root: Path, dir_rel: str) -> Path | None:
    """Real folder for a feed-relative profile directory, or None."""
    rel = str(dir_rel or "").replace("\\", "/").strip("/")
    if rel == TIKTOK_DIR or rel.startswith(TIKTOK_DIR + "/"):
        name = rel.split("/", 1)[1] if "/" in rel else ""
        if not name:
            return None
        for candidate in _tiktok_candidates(root):
            d = candidate / name
            if d.is_dir():
                return d
        return None
    d = Path(root) / rel
    return d if d.is_dir() else None


def is_reel(json_path: Path) -> bool | None:
    """Did Instagram mark this post as a reel?

    instaloader writes the GraphQL node beside every post, and the reel flag is
    nested under node.iphone_struct.is_reel_media. It is only present when true,
    so a missing key means "not a reel".

    is_reel_media is also set on story items, which are images (media_type 1),
    so a video check is required too -- otherwise story images inflate the count.

    Returns None when there is no sidecar to read, which is different from "not
    a reel": the caller decides what to do with unknown, and it should not be
    silently treated as a reel.
    """
    try:
        with open(json_path, "rb") as fh:
            node = json.load(fh).get("node", {}).get("iphone_struct", {})
    except (OSError, ValueError):
        return None
    if node.get("is_reel_media") is not True:
        return False
    return node.get("media_type") == 2


def _clip_id(stem: str) -> str:
    """yt-dlp names files by video id; Instagram names them by timestamp."""
    return stem.rsplit('.', 1)[0] if '.' in stem else stem


def _reel_for(folder: Path, filename: str) -> bool | None:
    """Look up the reel flag for one video file.

    A carousel's second and later videos are named <stamp>_2.mp4, _3.mp4 and so
    on, while the sidecar is written once for the whole post as <stamp>.json.
    Try the file's own name first, then the post's.
    """
    stem = filename.rsplit('.', 1)[0]
    own = folder / f"{stem}.json"
    if own.is_file():
        return is_reel(own)
    head, _, tail = stem.rpartition('_')
    if head and tail.isdigit():
        return is_reel(folder / f"{head}.json")
    return None


def scan(root: Path, limit_dirs: int = 5000,
         known_reels: dict[str, bool] | None = None) -> list[dict]:
    """Every playable video under `root`, newest first.

    Uses scandir rather than rglob so a large archive does not stall the feed.
    `known_reels` carries the reel flag for clips already classified, so their
    sidecar is not re-read on every request -- reading thousands of JSON files
    per poll is what made a large feed take seconds to answer.
    """
    known_reels = known_reels or {}
    root = Path(root)
    out: list[dict] = []
    if not root.is_dir():
        return out

    try:
        top = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return out

    # TikTok profiles live BESIDE the archive root (<parent>/tiktok), not under
    # it. The feed path keeps the `tiktok/...` prefix regardless, so the clips
    # already recorded in the database stay valid across the layout change.
    profile_dirs: list[tuple[str, str, str]] = []
    for e in top:
        if e.name.startswith('.'):
            continue
        if e.name == TIKTOK_DIR:
            continue    # handled below, wherever it actually lives
        profile_dirs.append((e.name, e.name, 'instagram'))

    # First candidate that exists wins, so an archive left in the old nested
    # shape is still read but nothing is counted twice.
    for candidate in _tiktok_candidates(root):
        if not candidate.is_dir():
            continue
        try:
            subs = [s for s in os.scandir(candidate)
                    if s.is_dir(follow_symlinks=False) and not s.name.startswith('.')]
        except OSError:
            continue
        for sub in subs:
            profile_dirs.append((f"{TIKTOK_DIR}/{sub.name}", sub.name, 'tiktok'))
        break

    for dir_rel, profile, platform in profile_dirs[:limit_dirs]:
        full = directory_for(root, dir_rel)
        if full is None:
            continue
        try:
            entries = list(os.scandir(full))
        except OSError:
            continue
        for e in entries:
            if not e.is_file(follow_symlinks=False):
                continue
            ext = os.path.splitext(e.name)[1].lower()
            if ext not in VIDEO_EXTS:
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            rel = f"{dir_rel}/{e.name}"
            # TikTok is short-form by definition, so every clip belongs in the
            # feed. Instagram only does when its sidecar says the post was a
            # reel, which is read here rather than at play time -- except when
            # we already classified this clip, in which case the flag is reused
            # and the sidecar is not opened again.
            if platform == 'tiktok':
                reel = True
            elif rel in known_reels:
                reel = known_reels[rel]
            else:
                reel = _reel_for(full, e.name)
            out.append({
                'path': rel,
                'profile': profile,
                'platform': platform,
                'clip_id': _clip_id(e.name),
                'is_reel': reel,
                'mtime': st.st_mtime,
                'bytes': st.st_size,
                'url': '/api/shorts/file/' + rel,
            })

    out.sort(key=lambda r: r['mtime'], reverse=True)
    return out


def resolve(root: Path, rel: str) -> Path | None:
    """Map a feed path back to a real file, refusing anything that escapes.

    `tiktok/...` is a virtual prefix. Those clips live in the folder BESIDE the
    archive root, so the prefix is stripped and looked up there, with the old
    nested location as a fallback. Every candidate is containment-checked
    against its own base, so a crafted path cannot reach outside either folder.
    """
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    parts = rel.split("/")
    if parts and parts[0] == TIKTOK_DIR and len(parts) >= 2:
        sub = "/".join(parts[1:])
        for candidate in _tiktok_candidates(root):
            got = _contained(candidate, sub)
            if got:
                return got
        return None
    return _contained(root, rel)
