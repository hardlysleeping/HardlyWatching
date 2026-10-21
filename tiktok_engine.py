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
from dataclasses import dataclass, asdict
from pathlib import Path

YTDLP = shutil.which("yt-dlp") or "yt-dlp"

# Real counts the UI offers. "all" means no --playlist-end.
COUNT_CHOICES = (5, 10, 25, 50, 0)  # 0 == all
DEFAULT_COUNT = 5

# Quality ceilings that actually exist in the extractor. 2160 is yt-dlp's
# ceiling for TikTok as of writing; asking higher just behaves as "best".
QUALITY_CHOICES = (360, 480, 720, 1080, 1440, 2160)


# Browser names yt-dlp's --cookies-from-browser accepts. It exits 2 on anything
# else, so this list is a hard constraint, not a suggestion. Firefox forks (Zen,
# LibreWolf) are absent on purpose: they are read through the firefox extractor
# by profile path, "firefox:/path/to/profile".
YTDLP_BROWSERS = {"brave", "chrome", "chromium", "edge", "firefox",
                  "opera", "safari", "vivaldi", "whale"}


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
    cookies_file: str = ""               # --cookies, a session the app holds
    proxy: str = ""                      # --proxy

    # -- date window
    date_after: str = ""                 # --dateafter YYYYMMDD
    date_before: str = ""                # --datebefore YYYYMMDD

    # -- how "all" behaves in the queue (see build_download_cmd)
    chunk_all: bool = False              # all = a slice per turn, then requeue
    chunk_size: int = 25                 # --max-downloads, when the above is on

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
    elif s.chunk_all:
        # "All" -- a slice at a time.
        #
        # --playlist-end would be the wrong flag for this. It means "the first N of
        # the list", so a second turn would look at the same N, find every one of
        # them already in the archive and download nothing: the profile would sit
        # at the back of the queue accomplishing nothing, forever. --max-downloads
        # counts videos actually fetched, so with the archive recording what came
        # before, each turn carries on further down the list instead.
        cmd += ['--max-downloads', str(max(1, int(s.chunk_size or 25)))]
    return cmd




# ---------------------------------------------------------------------------
# Profile pictures
#
# yt-dlp is not asked for one, so a TikTok archive holds no avatar at all --
# measured across the whole thing: 402 profiles, not one picture. The account's
# own page carries it, in the rehydration blob, as `avatarLarger`, and a plain
# GET returns that blob -- no session, no cookies, no signature. 12 of 12 sampled
# handles had one.
#
# It is deliberately NOT written into the profile folder. Instaloader puts the
# Instagram picture there, and the gallery scans every picture in a folder as
# media -- so a file dropped into a TikTok folder turns up as an extra tile
# against that profile. Checked rather than assumed: an Instagram folder holding
# only an avatar reports exactly one post, and that post is the avatar. An avatar
# is derived from a URL, not archive content, so it belongs in the cache.
# ---------------------------------------------------------------------------

AVATAR_UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 "
             "Firefox/132.0")


def avatar_url(handle: str, timeout: int = 25) -> str | None:
    """The account's profile picture URL, from its own page.

    None when the page does not carry one -- a handle with no TikTok account at
    all gives the same answer, which is correct: there is nothing to show.
    """
    import re
    import urllib.request
    url = profile_url(handle)
    req = urllib.request.Request(url, headers={
        "User-Agent": AVATAR_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r'"avatarLarger":"(.*?)"', html)
    if not m:
        return None
    # The blob is JSON-escaped, so a URL arrives with its slashes escaped.
    return m.group(1).replace("\\u002F", "/").replace("\\/", "/")


def fetch_avatar(handle: str, dest: Path, timeout: int = 30) -> Path | None:
    """Save the account's picture to `dest`, or return None.

    Written to a temporary name and moved into place, so a half-downloaded file
    is never mistaken for a finished one.
    """
    import shutil
    import urllib.request
    url = avatar_url(handle, timeout=timeout)
    if not url:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": AVATAR_UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            head = r.read(4)
            if not head.startswith(b"\xff\xd8") and not head.startswith(b"\x89PNG"):
                return None            # an error page, not a picture
            with open(tmp, "wb") as f:
                f.write(head)
                shutil.copyfileobj(r, f)
        tmp.replace(dest)
        return dest
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None


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
    # A jar the app holds comes first, and the browser is only a fallback.
    #
    # Reading the browser every run is not a session the app has: it lasts exactly
    # as long as the browser stays signed in and its profile directory stays put,
    # and nothing in the app can say whether it is there. A stored jar is the
    # app's own, so a download does not depend on the browser at all.
    jar = (s.cookies_file or "").strip()
    if jar and Path(jar).is_file():
        cmd += ['--cookies', jar]
        cmd.append(profile_url(handle))
        return cmd
    spec = (s.cookies_from_browser or "").strip()
    if spec:
        # Same idea as the Instagram side: reuse a browser login rather than
        # asking for a password. Optional -- public profiles work without it.
        #
        # yt-dlp exits 2 on a browser name it does not know, so one bad stored
        # value fails every TikTok download instantly with an opaque "yt-dlp
        # exited 2" (this happened: "zen" was saved and nothing downloaded for
        # hours). Drop an unusable spec and run without cookies instead, which
        # still works for public profiles, and let the caller surface a warning.
        if spec.split(":", 1)[0].lower() in YTDLP_BROWSERS:
            cmd += ['--cookies-from-browser', spec]
    cmd.append(profile_url(handle))
    return cmd


# Cookie names that carry an actual TikTok login. A jar full of tracking cookies
# downloads nothing that needs a session, so these are what get counted and
# reported -- "30 cookies" says nothing on its own.
LOGIN_COOKIE_NAMES = {"sessionid", "sid_tt", "sessionid_ss", "sid_guard",
                      "uid_tt", "uid_tt_ss", "passport_csrf_token"}
COOKIE_DOMAIN = "tiktok.com"


def cookies_db_for_spec(spec: str) -> Path | None:
    """The cookies.sqlite behind a --cookies-from-browser spec, or None.

    Only the Firefox-family path form ("firefox:/path/to/profile") is readable
    here, which is what this app stores for Firefox, Zen and LibreWolf. The other
    browsers keep their cookies somewhere else entirely, so for those the browser
    is still read directly at run time.
    """
    head, sep, tail = (spec or "").partition(":")
    if not sep or head.strip().lower() != "firefox" or not tail:
        return None
    db = Path(tail) / "cookies.sqlite"
    return db if db.is_file() else None


def capture_cookies(spec: str, out: Path, domain: str = COOKIE_DOMAIN) -> dict:
    """Write `out` as a Netscape cookie jar holding the browser's cookies.

    Netscape format is what `yt-dlp --cookies` reads, and it is the point of the
    exercise: a copy the app holds, so a download does not depend on the browser
    still being signed in, still installed, or on the profile directory still
    being where it was.

    Session cookies (expiry 0) are written as 0, which is how the format says
    "no expiry" -- dropping them would drop the login these exist to keep.
    """
    import sqlite3
    db = cookies_db_for_spec(spec)
    if db is None:
        return {"ok": False,
                "error": "This browser's cookies cannot be copied. Use the "
                         "browser session instead, or sign in to TikTok in "
                         "Firefox or Zen."}
    try:
        conn = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(moz_cookies)")}
            if not cols:
                raise RuntimeError("not a Firefox cookie database")
            domain_col = "baseDomain" if "baseDomain" in cols else "host"
            rows = conn.execute(
                f"SELECT host, path, isSecure, expiry, name, value "
                f"FROM moz_cookies WHERE {domain_col} LIKE ?",
                (f"%{domain}%",)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Latest row wins for a repeated name, since an old copy of a session cookie
    # would otherwise overwrite the live one.
    jar: dict[tuple, tuple] = {}
    login = 0
    for host, path, secure, expiry, name, value in rows:
        if not value:
            continue
        host = host or f".{domain}"
        if not host.startswith("."):
            host = "." + host.lstrip(".")
        jar[(host, path or "/", name)] = (host, path or "/", bool(secure),
                                          int(expiry or 0), name, value)
        if name in LOGIN_COOKIE_NAMES:
            login += 1

    if not jar:
        return {"ok": False,
                "error": f"No {domain} cookies found in that browser. Sign in "
                          f"to TikTok there first."}

    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File",
             "# Written by HardlyWatching. Edit by hand and yt-dlp may refuse it.",
             ""]
    for host, path, secure, expiry, name, value in jar.values():
        lines.append("	".join([host, "TRUE", path, "TRUE" if secure else "FALSE",
                                str(expiry), name, value]))
    # Trailing newline required: yt-dlp rejects a jar whose last line is cut short.
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        out.chmod(0o600)      # a session cookie is a credential
    except OSError:
        pass
    return {"ok": True, "path": str(out), "cookies": len(jar), "login": login}


def cookie_jar_state(path: str) -> dict:
    """What a held cookie jar contains, for showing in the UI.

    Read back from the file rather than remembered, so the display cannot drift
    from what yt-dlp will actually be given.
    """
    p = Path(path) if path else None
    if not p or not p.is_file():
        return {"held": False}
    try:
        cookies = login = 0
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            cookies += 1
            if parts[5] in LOGIN_COOKIE_NAMES:
                login += 1
        st = p.stat()
        return {"held": True, "path": str(p), "cookies": cookies, "login": login,
                "saved": int(st.st_mtime), "bytes": st.st_size}
    except OSError as e:
        return {"held": False, "error": str(e)}


def cookie_spec_problem(spec: str) -> str:
    """Why this --cookies-from-browser value would be rejected, or '' if fine."""
    spec = (spec or "").strip()
    if not spec:
        return ""
    browser = spec.split(":", 1)[0].lower()
    if browser in YTDLP_BROWSERS:
        return ""
    return (f"yt-dlp has no browser called '{browser}'. Supported: "
            + ", ".join(sorted(YTDLP_BROWSERS))
            + ". Firefox forks such as Zen and LibreWolf are used as "
              "firefox:/path/to/their/profile.")


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

def profile_total(handle: str, timeout: int = 150) -> int:
    """How many videos the profile currently has, or 0 if unknown.

    A flat listing returns every id without resolving each video, which is one
    request rather than one per video. 0 means "could not tell" and must never
    be treated as "has no videos": the UI shows the count as unknown rather
    than claiming a maximum of zero.
    """
    try:
        proc = subprocess.run(
            build_list_cmd(handle, 0),
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return 0
    if proc.returncode != 0:
        return 0
    return sum(1 for ln in (proc.stdout or '').splitlines() if ln.strip())
