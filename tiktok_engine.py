"""
TikTok downloading, driven by yt-dlp.

This module owns nothing about instaloader. It builds a yt-dlp command line and
runs it, the same way the Instagram side builds instaloader's. No scraping, no
private API: yt-dlp lists a public profile itself and downloads from it.

The one piece of state that matters is yt-dlp's own --download-archive file,
kept per profile. It records the IDs already fetched, so "grab more later" only
downloads what is new instead of re-fetching the whole account.

Every option here maps onto a real yt-dlp flag -- see `TikTokSettings`. Anything
not listed is not configurable, rather than silently ignored.
"""

import shutil
import subprocess
from dataclasses import dataclass, asdict, field
from pathlib import Path

YTDLP = shutil.which("yt-dlp") or "yt-dlp"

# Real counts the UI offers. "all" means no --playlist-end.
COUNT_CHOICES = (5, 10, 25, 50, 0)  # 0 == all
DEFAULT_COUNT = 5

# Quality ceilings that actually exist in the extractor. 2160 is yt-dlp's
# ceiling for TikTok as of writing; asking higher just behaves as "best".
QUALITY_CHOICES = (360, 480, 720, 1080, 1440, 2160)


@dataclass
class TikTokSettings:
    """yt-dlp options, named the way the UI labels them.

    Defaults reproduce the previous hardcoded command exactly, so upgrading
    does not change what an existing queue does.
    """

    # -- format selection
    quality: int = 1080                  # height ceiling; 0 = best available
    merge_container: str = "mp4"         # --merge-output-format
    recode: str = ""                     # --recode-video (e.g. mp4), "" = never

    # -- what gets written
    write_info_json: bool = True         # --write-info-json
    write_description: bool = False      # --write-description
    write_thumbnail: bool = False        # --write-thumbnail
    embed_metadata: bool = False         # --embed-metadata
    embed_thumbnail: bool = False        # --embed-thumbnail

    # -- politeness / reliability
    sleep_requests: float = 0.0          # --sleep-requests
    sleep_interval: float = 0.0          # --sleep-interval
    retries: int = 10                    # --retries
    fragment_retries: int = 10           # --fragment-retries
    limit_rate: str = ""                 # --limit-rate, e.g. "5M"
    cookies_from_browser: str = ""       # --cookies-from-browser
    proxy: str = ""                      # --proxy

    # -- date window
    date_after: str = ""                 # --dateafter YYYYMMDD
    date_before: str = ""                # --datebefore YYYYMMDD

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "TikTokSettings":
        """Tolerant load: unknown keys ignored, bad types fall back to default."""
        base = cls()
        if not isinstance(d, dict):
            return base
        out = {}
        for f in base.__dataclass_fields__.values():
            if f.name not in d:
                continue
            val = d[f.name]
            if isinstance(f.default, bool):
                out[f.name] = bool(val)
            elif isinstance(f.default, int):
                try:
                    out[f.name] = int(val)
                except (TypeError, ValueError):
                    pass
            elif isinstance(f.default, float):
                try:
                    out[f.name] = float(val)
                except (TypeError, ValueError):
                    pass
            else:
                out[f.name] = "" if val is None else str(val)
        return cls(**out)


def available() -> bool:
    return shutil.which("yt-dlp") is not None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def archive_path(root: Path, handle: str) -> Path:
    """Per-profile record of already-downloaded video IDs."""
    return root / handle / ".yt-dlp-archive.txt"


def profile_url(handle: str) -> str:
    h = handle.strip().lstrip('@')
    if h.startswith('http'):
        return h
    if 'tiktok.com' in h:
        return h if h.startswith('http') else 'https://' + h
    return f'https://www.tiktok.com/@{h}'


def build_list_cmd(handle: str, count: int = DEFAULT_COUNT) -> list[str]:
    """Command that lists video IDs, newest first, without downloading."""
    cmd = [YTDLP, '--flat-playlist', '--no-warnings', '--print', '%(id)s']
    if count and count > 0:
        cmd += ['--playlist-end', str(count)]
    cmd.append(profile_url(handle))
    return cmd


# TikTok serves a short ladder of vertical renditions, and yt-dlp reports
# `height` as the LONG side (1080p is 1080x1920, so h=1920). A pixel ceiling is
# therefore confusing: "720" has to become 1280. The UI exposes the rung name
# instead, and this maps it to the height filter yt-dlp actually matches.
QUALITY_RUNGS = {
    540: 1024,
    720: 1280,
    1080: 1920,
    1440: 2560,
    2160: 3840,
}


def format_selector(quality: int) -> str:
    """Pick the best rendition no taller than the requested rung.

    `quality` is the familiar number (540/720/1080/...), not raw pixels. 0 means
    best available. Falls back to the smallest rendition when nothing fits, so
    an unusual ladder still yields a file instead of failing the whole profile.
    """
    if not quality or quality <= 0:
        return "bv*+ba/b"
    h = QUALITY_RUNGS.get(int(quality), int(quality))
    return f"bv*[height<={h}]+ba/b[height<={h}]"


def _base_download_cmd(handle: str, dest: Path, archive: Path, count: int,
                       s: TikTokSettings) -> list[str]:
    cmd = [
        YTDLP,
        '--no-warnings',
        '--newline',
        # One file per video, named by ID so re-runs can't duplicate.
        '-o', str(dest / '%(id)s.%(ext)s'),
        # Remember what we already have.
        '--download-archive', str(archive),
        '-f', format_selector(s.quality),
        # A single broken video should not kill the whole profile.
        '--ignore-errors',
    ]

    if s.merge_container:
        cmd += ['--merge-output-format', s.merge_container]
    if s.recode:
        cmd += ['--recode-video', s.recode]

    if s.write_info_json:
        cmd += ['--write-info-json']
    if s.write_description:
        cmd += ['--write-description']
    if s.write_thumbnail:
        cmd += ['--write-thumbnail']
    if s.embed_metadata:
        cmd += ['--embed-metadata']
    if s.embed_thumbnail:
        cmd += ['--embed-thumbnail']

    if s.sleep_requests and s.sleep_requests > 0:
        cmd += ['--sleep-requests', f"{s.sleep_requests:g}"]
    if s.sleep_interval and s.sleep_interval > 0:
        cmd += ['--sleep-interval', f"{s.sleep_interval:g}"]
    if s.retries and s.retries > 0:
        cmd += ['--retries', str(int(s.retries))]
    if s.fragment_retries and s.fragment_retries > 0:
        cmd += ['--fragment-retries', str(int(s.fragment_retries))]
    if s.limit_rate:
        cmd += ['--limit-rate', s.limit_rate]
    if s.proxy:
        cmd += ['--proxy', normalise_proxy(s.proxy)]

    # yt-dlp accepts these without dashes, but takes them more reliably with.
    if s.date_after:
        cmd += ['--dateafter', ''.join(ch for ch in s.date_after if ch.isdigit())]
    if s.date_before:
        cmd += ['--datebefore', ''.join(ch for ch in s.date_before if ch.isdigit())]

    if count and count > 0:
        cmd += ['--playlist-end', str(count)]
    return cmd




def normalise_proxy(raw: str) -> str:
    """Turn what people actually paste into a URL yt-dlp will accept.

    A Cloudflare Worker or other basic proxy is usually copied as a bare
    hostname such as `my-proxy.example.workers.dev`, but yt-dlp needs a scheme.
    Anything without one is treated as HTTPS, because a Worker is HTTPS-only and
    an http:// request to it just fails. A bare host also gets the default port
    so an unreachable proxy reports a connection error rather than a malformed
    URL.
    """
    p = (raw or "").strip()
    if not p:
        return ""
    if "://" in p:
        return p
    p = "https://" + p
    host = p.split("://", 1)[1]
    if ":" not in host.split("/", 1)[0]:
        p = p.rstrip("/") + ":443" if "/" not in host else p
    return p


def build_download_cmd(handle: str, dest: Path, archive: Path,
                       count: int = DEFAULT_COUNT,
                       settings: TikTokSettings | None = None) -> list[str]:
    s = settings or TikTokSettings()
    cmd = _base_download_cmd(handle, dest, archive, count, s)
    if s.cookies_from_browser:
        # Same idea as the Instagram side: reuse a browser login rather than
        # asking for a password. Optional -- public profiles work without it.
        cmd += ['--cookies-from-browser', s.cookies_from_browser]
    cmd.append(profile_url(handle))
    return cmd


# Lines yt-dlp prints that mean something went wrong for one video but the run
# can continue.
_WARN = ('ERROR:', 'WARNING:', 'Unsupported URL', 'Unable to extract')


def classify(line: str) -> str | None:
    """Map a yt-dlp output line onto job progress. None means 'not interesting'."""
    s = line.strip()
    if not s:
        return None
    if s.startswith('[download]'):
        if 'Destination:' in s:
            return 'started-file'
        if s.endswith('%') or ' of ' in s:
            return 'progress'
        return None
    for w in _WARN:
        if w in s:
            return 'warn'
    if 'has already been recorded in the archive' in s:
        return 'already'
    return None


def count_existing(dest: Path) -> int:
    if not dest.is_dir():
        return 0
    return sum(1 for _ in dest.glob('*.mp4'))


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('usage: tiktok_engine.py <handle> [count]')
        raise SystemExit(2)
    h = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_COUNT
    print('url:      ', profile_url(h))
    print('yt-dlp:   ', available())
    print('ffmpeg:   ', ffmpeg_available())
    print('list cmd: ', ' '.join(build_list_cmd(h, n)))

# What yt-dlp prints when a profile cannot be listed because it is not visible,
# as opposed to not existing at all. Both end in the same "Unable to extract
# secondary user ID" error, so the warning is the only reliable signal.
_PRIVATE_MARK = 'private or has embedding disabled'


def probe_private(handle: str, timeout: int = 75) -> bool | None:
    """True if TikTok says the profile is private, False if it is listable.

    Returns None when the probe could not reach a conclusion -- a timeout, a
    network failure, or any error that is not the private marker. Unknown must
    never be reported as private: wrongly flagging a working profile as
    un-downloadable is worse than queueing one that turns out not to work,
    because the user would stop trying something that actually does work.
    """
    try:
        cmd = build_list_cmd(handle, 1)
        # The private marker arrives as a WARNING, which the normal download
        # command suppresses. Drop it here so the line is actually readable.
        cmd = [c for c in cmd if c != '--no-warnings']
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None

    blob = ((proc.stdout or '') + (proc.stderr or '')).lower()
    if _PRIVATE_MARK in blob:
        return True
    if proc.returncode == 0:
        return False
    # Any other failure is not something we can classify.
    return None
