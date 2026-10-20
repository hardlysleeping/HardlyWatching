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


def scan(root: Path, limit_dirs: int = 5000) -> list[dict]:
    """Every playable video under `root`, newest first.

    Uses scandir rather than rglob so a large archive does not stall the feed.
    """
    root = Path(root)
    out: list[dict] = []
    if not root.is_dir():
        return out

    try:
        top = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return out

    # TikTok profiles live one level down, in <root>/tiktok/<handle>, so the
    # feed has to look inside that folder as well as at the top. Anything else
    # at the top level is an Instagram profile, as before.
    profile_dirs: list[tuple[str, str, str]] = []
    for e in top:
        if e.name.startswith('.'):
            continue
        if e.name == 'tiktok':
            try:
                for sub in os.scandir(e.path):
                    if sub.is_dir(follow_symlinks=False) and not sub.name.startswith('.'):
                        profile_dirs.append((f"tiktok/{sub.name}", sub.name, 'tiktok'))
            except OSError:
                continue
        else:
            profile_dirs.append((e.name, e.name, 'instagram'))

    for dir_rel, profile, platform in profile_dirs[:limit_dirs]:
        full = root / dir_rel
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
            # reel, which is read here rather than at play time.
            if platform == 'tiktok':
                reel = True
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
    """Map a feed path back to a real file, refusing anything that escapes root."""
    root = Path(root).resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    if not target.is_file():
        return None
    if target.suffix.lower() not in VIDEO_EXTS:
        return None
    return target
