#!/usr/bin/env python3
"""Fetch the profile pictures a TikTok archive does not have.

yt-dlp is never asked for a profile picture, so a TikTok profile folder holds
nothing but videos and sidecars -- measured across this archive: 402 profiles, not
one picture. The account's own page carries it, in the rehydration blob, as
`avatarLarger`, and one plain GET returns that. No session, no cookies, no
signature; 12 of 12 sampled handles had one.

Pictures are cached outside the archive on purpose. Instaloader writes the
Instagram picture into the profile folder, and the gallery scans every picture in
a folder as media -- so a file put into a TikTok folder turns up as an extra post
against that profile. An avatar is derived from a URL rather than archive
content, so it lives in the cache and can be fetched again if it is ever lost.

Reports by default; writes nothing until --apply. Paced, because 400 requests in
a burst is how a temporary block starts. Already-cached profiles are skipped, so
running it again only picks up what is missing.

    tools/tiktok_avatars.py                 # what would be fetched
    tools/tiktok_avatars.py --apply         # fetch them
    tools/tiktok_avatars.py --apply --limit 20
    tools/tiktok_avatars.py --apply --json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def configured_roots() -> list[Path]:
    """The archive roots the app is using, read from its own state file.

    Read, not constructed: building an Engine to ask the same question loads the
    live state file and saves it again, and a tool that wants a list of folder
    names has no business touching the download queue.
    """
    import engine as eng
    out: list[Path] = []
    try:
        d = json.loads(eng.STATE_FILE.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        d = {}
    for p in [d.get("output_root"), *(d.get("roots") or [])]:
        if p:
            out.append(Path(p))
    if not out:
        out.append(Path(eng.OUTPUT_ROOT))
    return out


def tiktok_profiles(roots: list[Path]) -> list[str]:
    """Every TikTok account with a folder on disk, sorted."""
    import os
    import engine as eng
    names: set[str] = set()
    for r in roots:
        for c in eng.tiktok_roots(Path(r)):
            if not c.is_dir():
                continue
            try:
                with os.scandir(c) as it:
                    for entry in it:
                        try:
                            if entry.is_dir():
                                names.add(entry.name)
                        except OSError:
                            continue
            except OSError:
                continue
            break                    # first candidate that exists wins, as elsewhere
    return sorted(names)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually fetch and save (default: report only)")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many fetches (0 = no limit)")
    ap.add_argument("--pause", type=float, default=0.6,
                    help="seconds between accounts (default 0.6)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch even those already cached")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    args = ap.parse_args()

    import engine as eng
    import tiktok_engine as tt

    roots = configured_roots()
    names = tiktok_profiles(roots)
    have, missing = [], []
    for n in names:
        dest = eng.tiktok_avatar_file(n)
        if dest.is_file() and dest.stat().st_size > 0 and not args.refresh:
            have.append(n)
        else:
            missing.append(n)

    report = {"profiles": len(names), "cached": len(have), "to_fetch": len(missing),
              "fetched": 0, "no_avatar": 0, "failed": 0, "bytes": 0}
    if not args.apply:
        if args.json:
            print(json.dumps({**report, "sample": missing[:15]}, indent=2))
        else:
            print(f"  roots:           {', '.join(str(r) for r in roots)}")
            print(f"  tiktok profiles: {len(names)}")
            print(f"  already cached:  {len(have)}")
            print(f"  to fetch:        {len(missing)}")
            print(f"  would fetch (first {min(15, len(missing))}):")
            for n in missing[:15]:
                print(f"    {n}")
            print("\n  nothing written. re-run with --apply to fetch them.")
        return 0

    done = 0
    for n in missing:
        if args.limit and done >= args.limit:
            break
        dest = eng.tiktok_avatar_file(n)
        got = tt.fetch_avatar(n, dest)
        done += 1
        if got is None:
            # A handle with no TikTok account answers the same way, and that is
            # correct -- there is nothing to show. Counted separately from a
            # failure so the two are not confused in the report.
            report["no_avatar"] += 1
        else:
            report["fetched"] += 1
            try:
                report["bytes"] += got.stat().st_size
            except OSError:
                pass
        if not args.json and done % 25 == 0:
            print(f"  … {done}/{len(missing)}  "
                  f"fetched {report['fetched']}  none {report['no_avatar']}", flush=True)
        time.sleep(args.pause)

    report["remaining"] = len(missing) - done
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"\n  fetched:   {report['fetched']}  ({report['bytes']/1e6:.1f} MB)")
        print(f"  no avatar: {report['no_avatar']}  (no such account, or the page "
              f"does not carry one)")
        print(f"  remaining: {report['remaining']}")
        print(f"  cache:     {eng.AVATAR_CACHE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
