#!/usr/bin/env python3
"""Find videos whose frames never change, and report them. Changes nothing.

Many Instagram stories are 30 seconds of a single still image. They are stored as
video, so the gallery plays a motionless clip, the shorts feed shows a frozen
frame, and each one takes as much room as real footage.

This finds them by measuring, not guessing: it samples each video's frames,
downscales them, and measures how much each differs from the first. A clip that
never changes scores near zero; real footage scores tens. It only prints what it
finds -- converting or deleting is a separate, deliberate step.

    python3 tools/static_videos.py                 # report on everything
    python3 tools/static_videos.py --stories-only  # only clips with a story sidecar
    python3 tools/static_videos.py --max-bytes 1000000
    python3 tools/static_videos.py --json out.json # machine-readable, for review

Design notes, all of them from measuring rather than assuming:

  * One ffmpeg pass per file, not one process per sampled frame. The per-frame
    approach measured 6x slower (2.4s vs 0.39s a file).
  * Frames are compared after downscaling to 16x16 grey. A static clip still has
    *different bytes* per frame -- compression noise -- so counting distinct
    hashes reports "motion" on a still image. The mean pixel difference does not.
  * The mean difference, not the maximum: one noisy frame at a scene cut should
    not veto a clip that is otherwise frozen.
  * 2 frames a second. A story that holds one image does so for its whole length,
    so a dense sample buys nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Below this mean pixel difference the clip is treated as a single still.
# Measured: static clips score 0.06-0.9, real footage 63-70. 2.0 sits in the
# empty gap between them, so the exact value is not delicate.
STATIC_THRESHOLD = 2.0
SAMPLE_FPS = 2
FRAME = 16                      # px, downscaled grey
FRAME_BYTES = FRAME * FRAME


def frame_stats(fp: Path, fps: int = SAMPLE_FPS):
    """(frames, mean_diff_from_first) for a video, or None when unreadable.

    One ffmpeg pass, raw grey frames out, compared in memory. ffmpeg decodes and
    scales in the same step, which is why this is much cheaper than seeking to
    each timestamp in turn.
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(fp), "-vf",
             f"fps={fps},scale={FRAME}:{FRAME},format=gray",
             "-f", "rawvideo", "-"],
            capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    raw = proc.stdout
    if len(raw) < FRAME_BYTES * 2:
        return None
    frames = [raw[i:i + FRAME_BYTES]
              for i in range(0, len(raw) - FRAME_BYTES + 1, FRAME_BYTES)]
    first = frames[0]
    diffs = [sum(abs(a - b) for a, b in zip(first, f)) / FRAME_BYTES
             for f in frames[1:]]
    if not diffs:
        return None
    return len(frames), sum(diffs) / len(diffs)


def duration_of(fp: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(fp)],
            capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def is_story(folder: Path, stem: str) -> bool:
    """Did Instagram serve this as a story?

    instaloader writes the post's node beside it, and a story carries
    story_view_count. A missing sidecar is 'not known', never 'not a story'.
    """
    for cand in (folder / f"{stem}.json",):
        if not cand.is_file():
            continue
        try:
            node = (json.loads(cand.read_bytes()) or {}).get("node") or {}
        except (OSError, ValueError):
            continue
        return any(k.startswith("story_") for k in node)
    return False


def videos_under(root: Path):
    """Every .mp4/.mov in the tree, with the story flag and size.

    Deliberately a plain walk rather than the app's cached scanner: this is a
    maintenance report, run by hand, and it should see the archive exactly as it
    is on disk.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() not in {".mp4", ".mov", ".m4v", ".webm"}:
                continue
            fp = Path(dirpath) / name
            try:
                st = fp.stat()
            except OSError:
                continue
            if st.st_size == 0:
                continue
            yield fp, Path(dirpath), st.st_size, is_story(Path(dirpath),
                                                          os.path.splitext(name)[0])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.environ.get("INSTA_OUTPUT") or "",
                    help="archive root (default: env INSTA_OUTPUT)")
    ap.add_argument("--stories-only", action="store_true",
                    help="only clips whose sidecar marks them a story")
    ap.add_argument("--max-bytes", type=int, default=0,
                    help="only clips at or below this size (0 = no limit)")
    ap.add_argument("--threshold", type=float, default=STATIC_THRESHOLD,
                    help=f"mean-difference cut-off (default {STATIC_THRESHOLD})")
    ap.add_argument("--json", default="", help="write findings here as JSON")
    ap.add_argument("--limit", type=int, default=0, help="stop after N clips")
    args = ap.parse_args()

    if not args.root:
        print("error: no --root and INSTA_OUTPUT is not set", file=sys.stderr)
        return 2
    root = Path(args.root)
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    checked = found = 0
    findings = []
    bytes_freed = 0
    for fp, folder, size, story in videos_under(root):
        if args.stories_only and not story:
            continue
        if args.max_bytes and size > args.max_bytes:
            continue
        if args.limit and checked >= args.limit:
            break
        checked += 1
        stats = frame_stats(fp)
        if stats is None:
            continue
        n, diff = stats
        if diff <= args.threshold:
            dur = duration_of(fp)
            found += 1
            bytes_freed += size
            findings.append({
                "path": str(fp), "profile": folder.name, "name": fp.name,
                "bytes": size, "frames": n, "mean_diff": round(diff, 3),
                "seconds": round(dur, 1), "story": story,
            })
            print(f"STATIC  {diff:6.3f}  {dur:5.1f}s  {size/1024:8.0f}KB  "
                  f"{'story' if story else '     '}  {folder.name}/{fp.name}")
        if checked % 200 == 0:
            print(f"  ...{checked} checked, {found} static", file=sys.stderr)

    print()
    print(f"checked {checked} videos, {found} have a single unchanging frame")
    if found:
        print(f"they occupy {bytes_freed/1e6:.1f} MB as video")
    print("Nothing was modified: this is a report. Converting is a separate step.")
    if args.json:
        Path(args.json).write_text(json.dumps({
            "root": str(root), "checked": checked, "found": found,
            "threshold": args.threshold, "bytes": bytes_freed,
            "videos": findings,
        }, indent=1))
        print(f"written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
