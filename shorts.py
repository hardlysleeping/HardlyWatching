"""
Find the short-form videos on disk for the /shorts feed.

Kept separate from the gallery walk because the needs differ: the gallery wants
every image and video with metadata sidecars, while the feed only wants things it
can play full-screen, and it wants them cheaply on every request.

Only files that actually exist are returned. That is what makes a clip that was
deleted from disk disappear from the feed without any bookkeeping.
"""

import os
from pathlib import Path

VIDEO_EXTS = {'.mp4', '.mov', '.webm', '.m4v'}


def _clip_id(stem: str) -> str:
    """yt-dlp names files by video id; Instagram names them by timestamp."""
    return stem.rsplit('.', 1)[0] if '.' in stem else stem


def scan(root: Path, limit_dirs: int = 5000) -> list[dict]:
    """Every playable video under `root`, newest first.

    Uses scandir rather than rglob so a large archive does not stall the feed.
    """
    root = Path(root)
    out: list[dict] = []
    if not root.is_dir():
        return out

    try:
        profile_dirs = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return out

    for pd in profile_dirs[:limit_dirs]:
        if pd.name.startswith('.'):
            continue
        try:
            entries = list(os.scandir(pd.path))
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
            rel = f"{pd.name}/{e.name}"
            out.append({
                'path': rel,
                'profile': pd.name,
                'platform': 'tiktok' if e.name.split('.')[0].isdigit() else 'instagram',
                'clip_id': _clip_id(e.name),
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
