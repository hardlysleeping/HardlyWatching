#!/usr/bin/env python3
"""Save the photos in TikTok posts that yt-dlp cannot download.

A TikTok "photo post" (a slideshow) is served as an `imagePost` inside the same
item detail blob yt-dlp already fetches for every video. yt-dlp's TikTok
extractor only builds formats from `play_addr` and the music track, so such a
post yields one audio-only format, the app's `-f bv*+ba/b` selector skips it,
and the post disappears silently -- measured across this archive: 83,543 mp4 and
not one image file.

So this tool asks for the item detail directly, through yt-dlp's OWN web extractor
(the path that works on this box: web API + the saved cookies + curl_cffi
impersonation), reads `imagePost`, and writes the slides beside where the video
would have gone:

    <out>/<id>.jpg  <out>/<id>_2.jpg  ...  <out>/<id>.info.json

The names are chosen to be what the gallery already understands: a bare file
plus `_2`, `_3` are one post with parts (see group_into_posts), and the
`<id>.info.json` sidecar is the same shape yt-dlp writes, so the post is dated
by when it was posted instead of by when it was fetched.

It runs on the interpreter that has yt-dlp installed, not the app's venv:

    python tools/tiktok_photos.py --cookies <jar> --out <dir> <id> [<id> ...]
    python tools/tiktok_photos.py --cookies <jar> --out <dir> --ids-file ids.txt

One JSON line per id on stdout, so the caller can record what happened:

    {"id": "...", "status": 0, "author": "...", "create": 1791307996,
     "images": 2, "files": ["/out/123.jpg", "/out/123_2.jpg"],
     "desc": "...", "error": ""}

`status` is TikTok's own code, passed through rather than flattened: 10204 means
the post is blocked for this IP (the post is gone or not for this region), 10222
means a private account. The caller decides what to do with those; this tool
never guesses that a missing post is "unavailable" on its own.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
from pathlib import Path

EXTS = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/png": ".png",
        "image/heic": ".heic", "image/avif": ".avif"}


def _ext_for(url: str, ctype: str) -> str:
    """Extension from the response type, then the URL, then jpg."""
    ctype = (ctype or "").split(";")[0].strip().lower()
    if ctype in EXTS:
        return EXTS[ctype]
    guess = mimetypes.guess_extension(ctype or "") or ""
    if guess in (".jpe", ".jpeg"):
        return ".jpg"
    if guess in EXTS.values():
        return guess
    tail = url.split("?", 1)[0].rsplit(".", 1)[-1].lower()
    if "." + tail in EXTS.values():
        return "." + tail
    return ".jpg"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ids", nargs="*", help="TikTok video ids")
    ap.add_argument("--ids-file", default="", help="file with one id per line")
    ap.add_argument("--cookies", default="", help="Netscape cookie jar for tiktok.com")
    ap.add_argument("--out", required=True, help="folder to write the images into")
    ap.add_argument("--delay", type=float, default=2.5,
                    help="seconds between posts (default 2.5: this is a rate-limited site)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N posts")
    ap.add_argument("--browser", default="", help="fallback: read cookies from this browser")
    args = ap.parse_args()

    ids = list(args.ids)
    if args.ids_file:
        try:
            ids += [ln.strip() for ln in Path(args.ids_file).read_text().splitlines()
                    if ln.strip()]
        except OSError as e:
            print(json.dumps({"error": f"cannot read ids file: {e}"}), flush=True)
            return 2
    seen, todo = set(), []
    for i in ids:
        if i.isdigit() and i not in seen:
            seen.add(i)
            todo.append(i)
    if args.limit > 0:
        todo = todo[:args.limit]
    if not todo:
        return 0

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.extractor.tiktok import TikTokIE
        from yt_dlp.networking.impersonate import ImpersonateTarget
    except ImportError as e:  # pragma: no cover - environment problem
        print(json.dumps({"error": f"yt-dlp is not importable here: {e}"}), flush=True)
        return 3

    from curl_cffi import requests as cffi

    opts = {"quiet": True, "no_warnings": True,
            "impersonate": ImpersonateTarget("chrome")}
    if args.cookies:
        opts["cookiefile"] = args.cookies
    elif args.browser:
        opts["cookiesfrombrowser"] = (args.browser,)
    ydl = YoutubeDL(opts)
    ie = TikTokIE(ydl)

    jar = {}
    if args.cookies:
        try:
            for line in Path(args.cookies).read_text().splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                p = line.split("\t")
                if len(p) >= 7:
                    jar[p[5]] = p[6]
        except OSError:
            pass
    sess = cffi.Session(impersonate="chrome")
    for k, v in jar.items():
        sess.cookies.set(k, v, domain=".tiktok.com")

    for n, vid in enumerate(todo, 1):
        row = {"id": vid, "status": None, "author": "", "create": 0,
               "images": 0, "files": [], "desc": "", "error": ""}
        try:
            url = ie._create_url(None, vid)
            data, status = ie._extract_web_data_and_status(url, vid)
            row["status"] = status
            if data:
                row["author"] = (data.get("author") or {}).get("uniqueId") or ""
                row["desc"] = (data.get("desc") or "").strip()
                row["create"] = int(data.get("createTime") or 0)
                imgs = (data.get("imagePost") or {}).get("images") or []
                row["images"] = len(imgs)
                for i, img in enumerate(imgs, 1):
                    urls = (img.get("imageURL") or {}).get("urlList") or []
                    if not urls:
                        continue
                    got = None
                    for candidate in urls[:3]:
                        try:
                            r = sess.get(candidate, timeout=60,
                                         headers={"Referer": "https://www.tiktok.com/"})
                            if r.status_code == 200 and r.content:
                                got = (candidate, r)
                                break
                        except Exception:
                            continue
                    if got is None:
                        continue
                    candidate, r = got
                    ext = _ext_for(candidate, r.headers.get("content-type", ""))
                    name = f"{vid}{ext}" if i == 1 else f"{vid}_{i}{ext}"
                    target = out / name
                    target.write_bytes(r.content)
                    row["files"].append(str(target))
                if row["files"]:
                    # Same sidecar yt-dlp writes, so the gallery dates and
                    # captions the post instead of falling back to the file's
                    # mtime. Only the fields the archive reads are included.
                    side = {"id": vid, "extractor": "tiktok",
                            "uploader": row["author"], "uploader_id": "",
                            "title": row["desc"], "description": row["desc"],
                            "timestamp": row["create"], "webpage_url": url}
                    (out / f"{vid}.info.json").write_text(
                        json.dumps(side, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if n < len(todo):
            time.sleep(max(0.0, args.delay))
    return 0


if __name__ == "__main__":
    sys.exit(main())
