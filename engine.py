"""Instaloader job engine: single-worker queue with full job control.

Behaviour (locked in with the user):
  - exactly one profile at a time, delay between profiles
  - exponential backoff + auto-resume on blocks / rate limits
  - stories + highlights on by default
  - re-run fetches only new posts by default (fast-update)
  - output: <OUTPUT_ROOT>/<profile>/  (instaloader standard layout)

Everything instaloader can toggle is exposed as a Setting; the UI shows the
common ones and hides the rest behind "Advanced".
"""

from __future__ import annotations

import calendar
import datetime
import filecmp
import json
import os
import queue
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Where downloads and app state live. Overridable so this works on any machine
# (and so no personal path is baked into the source).
from profile_db import _shared_db


def config_dir() -> Path:
    """Standard per-user config folder for this OS.

    Linux/macOS: ~/.config   Windows: %APPDATA%
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base)


def tiktok_avatar_file(username: str) -> Path:
    """Where an account's TikTok picture is cached."""
    return AVATAR_CACHE / "tiktok" / (username.strip().lower() + ".jpg")


def tiktok_avatar(self, username: str, fetch: bool = False) -> Path | None:
    """The account's TikTok picture from the cache, fetching it once if asked.

    `fetch` is kept separate from reading on purpose: the gallery asks for one of
    these per card, and a first load has 400 of them, so fetching on request
    would be 400 page loads in a burst. The cache is filled in a paced pass and
    by each profile's own download instead.
    """
    if not username or not username.strip():
        return None
    dest = tiktok_avatar_file(username)
    try:
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
    except OSError:
        return None
    if not fetch:
        return None
    import tiktok_engine as _tt
    try:
        return _tt.fetch_avatar(username, dest)
    except Exception:
        return None


def cache_dir() -> Path:
    """Standard per-user cache folder for this OS."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "hardlywatching" / "cache"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "hardlywatching"


def instaloader_config_dir() -> Path:
    """Where instaloader keeps its sessions and stamps.

    Mirrors instaloader's own rule so we look in the right place on every OS:
    %LOCALAPPDATA%\\Instaloader on Windows, ~/.config/instaloader elsewhere.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Instaloader"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "instaloader"


OUTPUT_ROOT = Path(os.environ.get("INSTA_OUTPUT", Path.home() / "instagram"))
STATE_FILE = Path(os.environ.get(
    "INSTA_STATE", config_dir() / "hardlywatching" / "state.json"))
INSTALOADER = shutil.which("instaloader") or str(Path.home() / ".local/bin/instaloader")


# ---------------------------------------------------------------------------
# Disk space guardrails
#
# Downloading fills a drive and, left alone, keeps going until the volume is
# full -- which on a data drive means a failed write rather than a tidy stop.
# These are percentages of the volume, checked per root, so a profile pinned to
# another drive is judged against its own drive instead of the main one.
#
#   WARN  tell the user, keep downloading
#   STOP  start nothing else, and halt a run that crosses it
#
# STOP is also the reserve, expressed as a check made BEFORE the disk fills
# rather than after: at 95%, no job may start unless at least 5% of the volume
# is free, which is the same rule reached earlier. The GiB floor keeps small
# volumes from having a trivially tiny reserve.
DISK_WARN_PCT = 90.0
DISK_STOP_PCT = 95.0
DISK_RESERVE_MIN_GB = 2.0


def disk_usage(path) -> dict | None:
    """Usage of the volume holding `path`, or None when it is not reachable.

    A root on an unplugged drive is not an error, it is simply absent, and the
    caller must never read that as "0 bytes free" -- that would block every
    download because someone unplugged a backup disk.
    """
    try:
        u = shutil.disk_usage(str(path))
    except OSError:
        return None
    total = u.total or 0
    if not total:
        return None
    pct = u.used / total * 100.0
    reserve = max(DISK_RESERVE_MIN_GB * 2 ** 30, total * (100.0 - DISK_STOP_PCT) / 100.0)
    if pct >= DISK_STOP_PCT or u.free < reserve:
        level = "stop"
    elif pct >= DISK_WARN_PCT:
        level = "warn"
    else:
        level = "ok"
    return {
        "path": str(path),
        "total": total,
        "used": u.used,
        "free": u.free,
        "used_pct": round(pct, 1),
        "free_gb": round(u.free / 2 ** 30, 1),
        "total_gb": round(total / 2 ** 30, 1),
        "reserve_gb": round(reserve / 2 ** 30, 1),
        "level": level,
    }


def instaloader_available() -> bool:
    """True when an instaloader binary can actually be run.

    `shutil.which` may have failed at import time; re-check here so a freshly
    installed instaloader is picked up without restarting the app.
    """
    return bool(shutil.which("instaloader")) or Path(INSTALOADER).is_file()


def instaloader_path() -> str:
    return shutil.which("instaloader") or INSTALOADER

BETWEEN_PROFILES_SECONDS = 240

# Spreading the waits out.
#
# Every gap used to be the same number every single time: 120.0s before each
# Instagram profile, 15.0s before each TikTok one, identical for days. A fixed
# metronome is the one pattern a rate limiter can be sure about, and it also makes
# the countdown look frozen. A spread costs nothing and removes both.
PACING_JITTER = 0.25      # +/- 25% on a voluntary between-profiles gap
RETRY_JITTER = 0.20       # +/- 20% on a scheduled retry
COOLDOWN_JITTER = 0.15    # 0 to +15% on a safety cooldown


def jitter(seconds: float, spread: float = PACING_JITTER, *,
           up_only: bool = False) -> float:
    """`seconds` with a random spread, so repeated waits do not line up.

    `up_only` is for waits that exist for safety rather than pacing -- a rate
    limit cooldown, an IP block cooldown. Those may be lengthened at random but
    never shortened, because coming back early is exactly what turns a short block
    into a long one. Voluntary pacing is free to jitter both ways.
    """
    if seconds is None or seconds <= 0 or spread <= 0:
        return float(seconds or 0)
    delta = random.uniform(0.0, spread) if up_only else random.uniform(-spread, spread)
    return max(1.0, float(seconds) * (1.0 + delta))

# Automatic retry of throttled jobs. Instagram refuses a profile for a while and
# then serves it again, so a refusal is not an answer: the sweep re-queues it on
# a widening delay. Eight hours on the last try means a genuinely stuck profile
# is still tried once more after the longest plausible block, and then left
# alone rather than churning forever.
AUTO_RETRY_MAX = 4
AUTO_RETRY_DELAYS = (15 * 60, 60 * 60, 3 * 60 * 60, 8 * 60 * 60)  # 4 min. instaloader's own RateController already throttles
                                # inside each run (275 gql reqs/10 min), so this gap only
                                # spaces out back-to-back full-profile bursts.

# When Instagram hands us a 429, stop the whole queue (not just this job).
# Instagram itself asks for "a few minutes" / ~664 s, so ~15 min is a sane
# multiple of what the platform requests rather than an invented 5x margin.
DEFAULT_RATE_LIMIT_COOLDOWN = 900  # 15 min

# Consecutive rate-limited jobs before the queue pauses itself for a long break.
RATE_LIMIT_TRIP = 1  # any 429 stops the queue; resume only when the user says so

# A 401 alongside an empty sessionid means Instagram is withholding auth while
# rate-limiting the IP. Retrying only deepens the block. Instagram's IP-level
# limits clear in hours, not days.
IP_BLOCK_COOLDOWN = 2 * 3600  # 2 h

RETRYABLE = re.compile(
    r"(401 Unauthorized|429 Too Many Requests|Too Many Requests|"
    r"Please wait a few minutes|login required|checkpoint_required|"
    r"503 Service Unavailable|500 Internal Server Error|"
    r"Connection reset|timed out|Temporary failure|"
    r"JSON Query to .* failed|Unable to retrieve|Too many requests)",
    re.IGNORECASE,
)

# An unhandled Python exception in instaloader (e.g. KeyError on an unexpected
# API response). Retrying identical arguments will crash identically, so this
# must NOT be treated as a rate limit.
CRASH = re.compile(r"(Traceback \(most recent call last\)|^\w+Error:|^\w+Exception:)",
                   re.IGNORECASE | re.MULTILINE)

# A crash specifically raised while fetching stories/highlights: retry once
# without the optional extras rather than losing the whole archive.
CRASH_IN_OPTIONAL = re.compile(
    r"(download_stories|download_highlights|get_items|_fetch_iphone_struct)",
    re.IGNORECASE,
)

FATAL = re.compile(
    r"(Login error|not logged in|Session file does not exist|"
    r"Profile does not exist|Invalid username|"
    # Require an HTTP-ish context for 404 so the progress counter "[404/406]"
    # is not mistaken for a missing profile. (That bug marked any profile with
    # 400+ posts as unrecoverable after downloading all of it.)
    r"(?:HTTP|Error|error|status|code|got)\D{0,12}404\b|"
    r"404\s+Not\s+Found|"
    r"does not exist|"
    r"is private|Private profile|"
    r"Wrong login|HTTPError 401)",
    re.IGNORECASE,
)

# Instagram telling us how long to wait: "retried in 664 seconds".
WAIT_HINT = re.compile(r"retried in (\d+) seconds", re.IGNORECASE)

# Instagram answers a throttled or bot-flagged client with the SAME message it
# uses for a deleted account: "Profile X does not exist." A profile that really
# is gone does not also produce an HTTP 200 on its public page, and instaloader
# itself prints this hint when it suspects a block rather than a deletion. When
# that hint is present the message must NOT be treated as fatal -- doing so
# marked real, followable profiles as permanently failed.
BLOCKED_HINT = re.compile(
    r"Trying again anonymously, helps in case you are just blocked",
    re.IGNORECASE)

# instaloader's per-post progress: "[ 404/764]". The only honest progress figure.
PROGRESS_RE = re.compile(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]")
# instaloader's aside when the higher-quality copy of the picture cannot be
# fetched. It logs this and then exits non-zero for "Errors or warnings
# occurred", which says nothing about whether the archive itself is complete.
PROFILE_PIC_WARN = re.compile(r"Unable to fetch high quality profile pic", re.IGNORECASE)

# When instaloader cannot verify the saved session it falls back to
# interactive_login() and asks for a password. In a headless worker stdin is
# /dev/null, so getpass dies with EOFError and the whole run looks like a
# Python crash -- which failed the job and burned an attempt, when the real
# cause was Instagram answering the session check with 401 "please wait a few
# minutes". Recognise the prompt itself, before CRASH sees the traceback.
SESSION_PROMPT = re.compile(
    r"(Enter Instagram password|GetPassWarning|Can not control echo)",
    re.IGNORECASE,
)

PROFILE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def list_sessions() -> list[dict]:
    """Every saved instaloader session, newest first, each with a validity check."""
    cfg = instaloader_config_dir()
    out: list[dict] = []
    if not cfg.is_dir():
        return out
    for f in sorted(cfg.glob("session-*"), key=lambda p: p.stat().st_mtime, reverse=True):
        username = f.name[len("session-"):]
        d = _diagnose_file(f, username)
        try:
            mtime = f.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append({
            "username": username,
            "valid": d["valid"],
            "problem": d["problem"],
            "mtime": mtime,
            "path": str(f),
        })
    return out


def _diagnose_file(f: Path, username: str) -> dict:
    try:
        import pickle
        data = pickle.loads(f.read_bytes())
    except Exception as e:
        return {"valid": False, "problem": f"unreadable ({e})"}
    sid = data.get("sessionid")
    sid = sid if isinstance(sid, str) else str(sid or "")
    if not sid.strip():
        return {"valid": False,
                "problem": "incomplete login — Instagram issued no session token"}
    return {"valid": True, "problem": ""}


def session_diagnosis(active: str | None = None, locked=None) -> dict:
    """Inspect instaloader sessions without network access.

    `active` selects which session to treat as current (by username). When
    None, the chosen session from config, else the newest, is used.

    A session file can exist yet be useless: instaloader writes one after a
    login that Instagram accepted but never issued a sessionid for (an
    unfinished checkpoint), which then fails every request with 401.

    `locked` is the set of usernames the user has locked out (a flagged
    account cooling off). A locked session is never auto-chosen and never
    reported as usable, so the account cannot be put back to work by
    restarting the app -- only by unlocking it.
    """
    locked_set = {str(u).lower() for u in (locked or []) if u}
    sessions = list_sessions()
    for s in sessions:
        s["locked"] = s["username"].lower() in locked_set
    if not sessions:
        return {"present": False, "username": None, "valid": False,
                "problem": "No instaloader session found. Log in with the button above.",
                "sessions": []}

    chosen = None
    if active:
        chosen = next((s for s in sessions if s["username"] == active), None)
    if chosen is None:
        # Prefer a valid unlocked session; failing that, the best unlocked one,
        # so an incomplete login is reported as such rather than hidden behind a
        # locked account. Only when every session is locked do we report a lock.
        unlocked = [s for s in sessions if not s["locked"]]
        chosen = next((s for s in unlocked if s["valid"]), None)
        if chosen is None:
            chosen = unlocked[0] if unlocked else sessions[0]

    problem = chosen["problem"]
    if chosen["locked"]:
        problem = (f"Locked for a cooldown: {chosen['username']} is not used for "
                   f"downloads. Unlock it in Sessions to use it again.")
    elif not chosen["valid"]:
        problem = (f"Login for {chosen['username']} is incomplete — Instagram issued no "
                   f"session token. Log in again with the button above.")

    return {
        "present": True,
        "username": chosen["username"],
        "valid": chosen["valid"] and not chosen["locked"],
        "problem": problem,
        "sessions": sessions,
    }


# ---------- browser session discovery ----------
#
# instaloader's own --login can "succeed" yet save an EMPTY sessionid, which
# then 401s forever. Its docs recommend bypassing --login by importing the
# cookie from a browser where you are already logged in. We discover those
# cookies ourselves so the UI can offer them with one click.

BROWSER_GLOBS = [
    # Linux (native + Flatpak)
    ("Firefox",        ".mozilla/firefox/*/cookies.sqlite"),
    ("Firefox",        ".config/mozilla/firefox/*/cookies.sqlite"),
    ("Firefox",        ".var/app/org.mozilla.firefox/.mozilla/firefox/*/cookies.sqlite"),
    ("Zen",            ".zen/*/cookies.sqlite"),
    ("Zen",            ".var/app/app.zen_browser.zen/.zen/*/cookies.sqlite"),
    ("LibreWolf",      ".librewolf/*/cookies.sqlite"),
    ("LibreWolf",      ".var/app/io.gitlab.librewolf-community/.librewolf/*/cookies.sqlite"),
    # macOS
    ("Firefox",        "Library/Application Support/Firefox/Profiles/*/cookies.sqlite"),
    ("Zen",            "Library/Application Support/zen/Profiles/*/cookies.sqlite"),
    ("LibreWolf",      "Library/Application Support/LibreWolf/Profiles/*/cookies.sqlite"),
    # Windows (%APPDATA% is resolved below)
    ("Firefox",        "Mozilla/Firefox/Profiles/*/cookies.sqlite"),
    ("Zen",            "zen/Profiles/*/cookies.sqlite"),
    ("LibreWolf",      "LibreWolf/Profiles/*/cookies.sqlite"),
]


# Browsers that store cookies in Firefox's format. They are NOT valid yt-dlp
# browser names, so each is reached as firefox:<profile directory> instead.
FIREFOX_FAMILY = {"Firefox", "Zen", "LibreWolf"}


def _browser_roots() -> list[Path]:
    """Base folders to look under for browser profiles, per OS."""
    home = Path.home()
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return [Path(appdata)]
    if sys.platform == "darwin":
        return [home, home / "Library" / "Application Support"]
    return [home]


def read_browser_cookies(cookiefile: Path) -> dict[str, str]:
    """instagram.com cookies from a Firefox-family cookies.sqlite."""
    import sqlite3
    conn = sqlite3.connect(f"file:{cookiefile}?immutable=1", uri=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(moz_cookies)")}
        if not cols:
            raise RuntimeError("not a Firefox cookie database")
        domain_col = "baseDomain" if "baseDomain" in cols else "host"
        rows = conn.execute(
            f"SELECT name, value FROM moz_cookies WHERE {domain_col} LIKE '%instagram.com'"
        ).fetchall()
    finally:
        conn.close()
    cookies: dict[str, str] = {}
    for name, value in rows:
        if value:
            cookies[name] = value  # later rows win (most recently written)
    return cookies


def find_browser_sessions() -> list[dict]:
    """Instagram logins available in installed browsers, best candidate first.

    `account` is the username whose sessionid is in that store, when it can be
    determined from the cookies themselves (ds_user_id is not a name, so the
    name is resolved by importing and calling test_login, which the UI does
    only on an explicit click).
    """
    out: list[dict] = []
    seen: set[str] = set()
    roots = _browser_roots()
    for label, pattern in BROWSER_GLOBS:
        for root in roots:
            for p in root.glob(pattern):
                rp = str(p.resolve())
                if rp in seen or not p.is_file():
                    continue
                seen.add(rp)
                entry = {"browser": label, "path": str(p), "cookies": 0,
                         "has_sessionid": False, "mtime": 0.0, "account": None,
                         "user_id": None}
                try:
                    entry["mtime"] = p.stat().st_mtime
                except OSError:
                    pass
                try:
                    c = read_browser_cookies(p)
                    entry["cookies"] = len(c)
                    entry["has_sessionid"] = bool(c.get("sessionid"))
                    # ds_user_id identifies the account even without the name.
                    entry["user_id"] = c.get("ds_user_id") or None
                except Exception:
                    pass
                out.append(entry)
    out.sort(key=lambda e: (not e["has_sessionid"], -e["mtime"]))
    return out


def browser_name_for_spec(spec: str) -> str:
    """A readable name for a cookie spec, judged from the profile path.

    yt-dlp calls every Firefox-family browser "firefox", so the spec on its own
    cannot say which one it is -- the profile path usually can, because the
    flatpak app id or the profile folder carries the browser's name.
    """
    s = (spec or "").lower()
    if not s:
        return ""
    for needle, name in (("zen_browser", "Zen"), ("zen/", "Zen"),
                         ("librewolf", "LibreWolf"), ("waterfox", "Waterfox"),
                         ("mullvad", "Mullvad"), ("firefox", "Firefox")):
        if needle in s:
            return name
    return "Browser"


def tiktok_source_label(spec: str) -> str:
    """A readable name for where a login came from: "Zen (Default)".

    The profile folder is kept because two profiles of one browser are two
    different logins, and "(release)" and friends are noise in a label.
    """
    spec = str(spec or "").strip()
    if not spec:
        return ""
    browser = browser_name_for_spec(spec)
    profile = spec.split(":")[-1].rstrip("/").split("/")[-1]
    profile = re.sub(r"\s*\([^)]*\)\s*$", "", profile).strip()
    if not profile:
        return browser or "Browser"
    return f"{browser or 'Browser'} ({profile})"


def tiktok_session_label(spec: str, taken: set[str] | None = None) -> str:
    """Default label for a held TikTok session.

    TikTok's cookie jar holds no user name, so nothing here can name a session
    after the account it belongs to. It is labelled by where the login came
    from, and the name can be changed in Sessions -- a list of sessions you
    cannot tell apart is not one you can swap between.
    """
    base = browser_name_for_spec(spec) or "TikTok session"
    taken = taken or set()
    label, n = base, 2
    while label in taken:
        label = f"{base} {n}"
        n += 1
    return label


def find_tiktok_browsers() -> list[str]:
    """yt-dlp cookie specs for browsers that have a TikTok cookie store.

    yt-dlp takes `--cookies-from-browser BROWSER[:PROFILE]` and **rejects any
    browser name it does not know** with an immediate exit 2, so a wrong label
    here does not degrade -- it fails every TikTok download instantly. Its
    accepted names are a fixed list, and the Firefox forks are not on it: Zen
    and LibreWolf are read through the firefox extractor by pointing it at their
    profile directory, `firefox:/path/to/profile`, which is the same cookie
    format. So the spec returned here is the one that goes straight onto the
    command line, not a bare label.
    """
    out: list[str] = []
    seen: set[str] = set()
    # Map our internal labels to yt-dlp's expected names.
    label_to_ytdlp = {
        "Chrome": "chrome", "Chromium": "chromium", "Edge": "edge",
        "Brave": "brave", "Vivaldi": "vivaldi", "Opera": "opera",
        "Safari": "safari",
    }
    roots = _browser_roots()
    for label, pattern in BROWSER_GLOBS:
        for root in roots:
            for p in root.glob(pattern):
                try:
                    rp = str(p.resolve())
                except OSError:
                    continue
                if rp in seen or not p.is_file():
                    continue
                seen.add(rp)
                try:
                    import sqlite3
                    conn = sqlite3.connect(f"file:{p}?immutable=1", uri=True)
                    try:
                        cols = {r[1] for r in conn.execute("PRAGMA table_info(moz_cookies)")}
                        if not cols:
                            continue
                        domain_col = "baseDomain" if "baseDomain" in cols else "host"
                        rows = conn.execute(
                            f"SELECT 1 FROM moz_cookies WHERE {domain_col} LIKE '%tiktok.com' LIMIT 1"
                        ).fetchall()
                    finally:
                        conn.close()
                    if not rows:
                        continue
                    if label in FIREFOX_FAMILY:
                        # Firefox, Zen and LibreWolf all store the same
                        # cookies.sqlite; name the directory so yt-dlp reads the
                        # right one and never complains about the fork's name.
                        name = f"firefox:{p.parent}"
                    else:
                        name = label_to_ytdlp.get(label, "")
                    # Belt and braces: never hand yt-dlp a name it will reject.
                    import tiktok_engine as _tt
                    if name.split(":", 1)[0] not in _tt.YTDLP_BROWSERS:
                        continue
                    if name not in out:
                        out.append(name)
                except Exception:
                    pass
    return out


def import_browser_cookie(cookiefile: Path, username: str | None = None) -> dict:
    """Write an instaloader session file from a browser's Instagram cookie.

    Mirrors instaloader's documented 615_import_firefox_session.py workaround,
    including its `test_login()` verification.
    """
    try:
        from instaloader import Instaloader
    except ModuleNotFoundError:
        import glob as _glob
        for _c in _glob.glob(str(Path.home() /
                                ".local/share/pipx/venvs/instaloader/lib/python*/site-packages")):
            import sys as _sys
            if _c not in _sys.path:
                _sys.path.insert(0, _c)
        try:
            from instaloader import Instaloader
        except ModuleNotFoundError:
            return {"ok": False, "error": "instaloader not importable from this interpreter"}

    try:
        cookies = read_browser_cookies(cookiefile)
    except Exception as e:
        return {"ok": False, "error": f"could not read cookies: {e}"}
    if not cookies.get("sessionid"):
        return {"ok": False, "error": "no sessionid in that browser — log in there first"}

    loader = Instaloader(max_connection_attempts=1)
    loader.context._session.cookies.update(cookies)
    try:
        who = loader.test_login()
    except Exception as e:
        return {"ok": False, "error": f"Instagram rejected the cookie: {e}"}
    if not who:
        return {"ok": False, "error": "Instagram rejected the cookie — log in again in the browser"}

    loader.context.username = who
    try:
        loader.save_session_to_file()
    except Exception as e:
        return {"ok": False, "error": f"could not save session: {e}"}
    return {"ok": True, "username": who}


def probe_ip_block() -> bool:
    """Check whether Instagram is rate-limiting this IP, using ONE cheap request.

    Returns True when Instagram answers a plain anonymous API call with 429.
    Only called on an explicit user action, never on a timer.
    """
    try:
        import requests
        s = requests.Session()
        s.headers.update({
            "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"),
            "X-IG-App-ID": "936619743392459",
            "X-Requested-With": "XMLHttpRequest",
        })
        r = s.get("https://www.instagram.com/api/v1/users/web_profile_info/",
                  params={"username": "instagram"}, timeout=20)
        return r.status_code == 429
    except Exception:
        return False


def clean_profile_name(raw: str) -> str | None:
    """Accept 'name', '@name', or an instagram.com URL. Return bare username or None."""
    s = raw.strip()
    if not s:
        return None
    if s.startswith(("http://", "https://")):
        m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", s)
        if not m:
            return None
        s = m.group(1)
    s = s.lstrip("@").strip().rstrip("/")
    s = s.split("?")[0].split("#")[0]
    if not PROFILE_RE.match(s):
        return None
    return s


# ---------- gallery ----------

# Media instaloader writes that are worth showing in a browser grid. `.temp`
# and `.json` are deliberately absent: a killed download leaves 0-byte temps.
# .heic/.heif are what iPhones write. Browsers other than Safari cannot display
# them, so the grid never serves the original -- it serves the JPEG thumbnail,
# which is why adding the extension is enough for them to appear.
GALLERY_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".heic", ".heif"}
GALLERY_VIDEO_EXTS = {".mp4", ".mov"}
# What a TikTok archive holds. yt-dlp writes .mp4, and .webm/.m4v turn up when a
# clip had no H.264 version; .jpg only when thumbnails were asked for. Used to
# recognise an archive folder, so a directory that merely shares the name is not
# mistaken for one.
TIKTOK_EXTS = {".mp4", ".webm", ".m4v", ".mov", ".mkv", ".jpg", ".jpeg", ".webp"}
# What an upload is allowed to write. Deliberately the same set the gallery can
# display: storing something it would then refuse to show is just a confusing
# way to fill the disk. .heic is absent on purpose -- see the note in the docs;
# a phone HEIC would be skipped, not silently mangled.
UPLOAD_EXTS = GALLERY_EXTS


# Listing a profile folder means touching every file in it: the biggest
# archives here hold ~6,700 posts, and /api/gallery lists all of them on every
# request, which measured at ~0.7s of pure syscall overhead to produce 5KB of
# JSON. The list only changes when a file is added or removed, and a directory's
# mtime changes on exactly those events, so the result is cached against it.
# Keyed by (folder, dir mtime, dir size) so a rename that preserves mtime still
# misses. Entries are dropped when a folder disappears.
#
# The same rows also live in a SQLite file (see _Cache), because an in-memory
# cache is empty after every restart and the first load of each profile then
# pays the full scan again. The SQLite copy makes a cold start as fast as a
# warm one. The filesystem is always the source of truth: a row is only ever
# used when its stamp still matches the directory on disk, so a stale or
# corrupt cache degrades to a rescan and can never show a wrong list.
_MEDIA_CACHE: dict[str, tuple[tuple, list[str]]] = {}
_MEDIA_CACHE_MAX = 512

CACHE_DB = Path(os.environ.get(
    "INSTA_CACHE_DB", config_dir() / "hardlywatching" / "gallery-cache.db"))


# Bumped whenever the way a folder's files become posts changes. Cached posts
# are only right for the parser that produced them, and the stamp is built from
# file metadata -- so a parser fix would otherwise leave every old row valid and
# in service forever. 2: TikTok dates come from the video's metadata.
# 3: a photo with no other date uses its EXIF capture time.
POSTS_RECIPE = 3


class _Cache:
    """SQLite-backed mirror of the folder listing cache.

    One row per folder: the stamp it was computed from and the resulting list
    of names, as newline-joined text (the names are instaloader's filenames and
    a line-separated blob is far cheaper to store and read than JSON per key).
    """

    def __init__(self, path: Path):
        self.path = path
        self.conn = None
        self._lock = threading.Lock()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(path), check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS media ("
                " folder TEXT PRIMARY KEY,"
                " mtime_ns INTEGER, size INTEGER, inode INTEGER,"
                " names TEXT,"
                " posts TEXT,"
                " newest_ns INTEGER,"
                " nfiles INTEGER,"
                " updated REAL)")
            # The post cache needs two columns that the first version of this
            # table did not have. Adding them in place keeps existing listings
            # usable, so an upgrade does not force a full rescan.
            have = {r[1] for r in
                    self.conn.execute("PRAGMA table_info(media)").fetchall()}
            for col in ("newest_ns", "nfiles"):
                if col not in have:
                    self.conn.execute(f"ALTER TABLE media ADD COLUMN {col} INTEGER")
            # Drop cached posts built by an older parser. One UPDATE, once.
            have_ver = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if have_ver != POSTS_RECIPE:
                self.conn.execute("UPDATE media SET posts=NULL")
                self.conn.execute(f"PRAGMA user_version={int(POSTS_RECIPE)}")
            self.conn.commit()
        except Exception:
            # A cache that cannot be opened is not an error: the app simply
            # scans the disk as it did before.
            self.conn = None

    def get(self, folder: str, stamp: tuple) -> list[str] | None:
        """Cached names for a folder. `stamp` is (mtime_ns, size, inode, newest).

        The four values must all match what is on disk right now. `newest` is
        the newest file mtime, which is what catches a file being emptied (the
        trash flow) -- the directory's own metadata does not change for that.
        """
        if self.conn is None:
            return None
        try:
            with self._lock:
                row = self.conn.execute(
                    "SELECT mtime_ns, size, inode, newest_ns, names"
                    " FROM media WHERE folder=?",
                    (folder,)).fetchone()
        except Exception:
            return None
        if not row:
            return None
        have = (row[0], row[1], row[2], row[3])
        want = tuple(stamp)
        if len(want) == 3:                 # older callers: no newest component
            have, want = have[:3], want
        if have != want:
            return None                    # folder changed -> rescan
        return row[4].split("\n") if row[4] else []

    def put(self, folder: str, stamp: tuple, names: list[str]) -> None:
        if self.conn is None:
            return
        s = tuple(stamp) + (0, 0, 0, 0)
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO media (folder, mtime_ns, size, inode, newest_ns,"
                    " names, updated) VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(folder) DO UPDATE SET"
                    " mtime_ns=excluded.mtime_ns, size=excluded.size,"
                    " inode=excluded.inode, newest_ns=excluded.newest_ns,"
                    " names=excluded.names, updated=excluded.updated",
                    (folder, s[0], s[1], s[2], s[3], "\n".join(names),
                     time.time()))
                self.conn.commit()
        except Exception:
            pass

    def get_posts(self, folder: str, stamp: tuple) -> list[dict] | None:
        """Cached grouped posts for a folder, if the stamp still matches.

        The stamp is (size, inode, newest_file_mtime, file_count) -- it must be
        compared against exactly those four columns. The directory's own mtime
        is not used at all: on this mount merely reading a directory updates it,
        so every request looked like a change and the cache never hit.
        """
        if self.conn is None:
            return None
        try:
            with self._lock:
                row = self.conn.execute(
                    "SELECT size, inode, newest_ns, nfiles, posts"
                    " FROM media WHERE folder=?",
                    (folder,)).fetchone()
        except Exception:
            return None
        if not row or not row[4]:
            return None
        if (row[0], row[1], row[2], row[3]) != tuple(stamp):
            return None
        try:
            return json.loads(row[4])
        except (ValueError, TypeError):
            return None                    # unreadable row -> rebuild

    def put_posts(self, folder: str, stamp: tuple, posts: list[dict]) -> None:
        if self.conn is None:
            return
        try:
            blob = json.dumps(posts, separators=(",", ":"))
        except (TypeError, ValueError):
            return
        try:
            with self._lock:
                # The row may already exist from the listing cache, so update
                # the posts columns without disturbing the names column.
                self.conn.execute(
                    "INSERT INTO media (folder, size, inode, newest_ns, nfiles,"
                    " posts, updated)"
                    " VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(folder) DO UPDATE SET"
                    " size=excluded.size, inode=excluded.inode,"
                    " newest_ns=excluded.newest_ns, nfiles=excluded.nfiles,"
                    " posts=excluded.posts, updated=excluded.updated",
                    (folder, stamp[0], stamp[1], stamp[2], stamp[3], blob,
                     time.time()))
                self.conn.commit()
        except Exception:
            pass

    def drop(self, folder: str) -> None:
        if self.conn is None:
            return
        try:
            with self._lock:
                self.conn.execute("DELETE FROM media WHERE folder=?", (folder,))
                self.conn.commit()
        except Exception:
            pass

    def stats(self) -> dict:
        if self.conn is None:
            return {"enabled": False}
        try:
            with self._lock:
                n = self.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
            return {"enabled": True, "folders": n, "path": str(self.path)}
        except Exception:
            return {"enabled": False}


_cache = _Cache(CACHE_DB)


def _media_cache_forget(folder: Path) -> None:
    """Drop a folder's cached listing after something wrote into it.

    Uploads add files without going through _gallery_media(), so the memory and
    SQLite rows for that folder are stale the moment a file lands. Both are
    cleared here rather than waiting for the directory stamp to be noticed,
    because the next request may well be the one that lists the new file.
    """
    key = str(folder)
    _MEDIA_CACHE.pop(key, None)
    _cache.drop(key)


def _posts_stamp(folder: Path, names: list[str]) -> tuple | None:
    """Invalidation key for cached posts.

    The directory's own mtime is deliberately NOT part of this. On this mount
    simply reading a directory bumps it (atime/relatime updates), so including
    it made the stamp differ on every request and the cache never hit -- it
    measured as a 1.0x speedup, i.e. no cache at all.

    What is used instead:
      * the newest file mtime, because a post's date and caption come from its
        sidecar JSON -- editing or replacing that file has to invalidate the
        cache, and the directory mtime would not notice;
      * the file count, so an added or removed file also invalidates;
      * size and inode of the directory, which are stable and catch a folder
        being swapped for another one at the same path.

    A brand-new file necessarily carries the newest mtime, so additions are
    still caught even without the directory timestamp.
    """
    st = _dir_stamp(folder)
    if st is None:
        return None
    # Every media file is stat'ed here, and so is every sidecar.
    #
    # The sidecars matter and are easy to miss: GALLERY_EXTS only covers media,
    # so a `.json` never appears in `names`. But a post's date and caption come
    # FROM that sidecar, which means editing one changes the answer while the
    # directory's size, inode and file count all stay identical. Skipping them
    # served a stale caption after an edit. So the sidecars are stat'ed via a
    # single scandir of the folder, which is far cheaper than a stat per name.
    newest = 0
    sidecars = 0
    try:
        stem_set = {os.path.splitext(n)[0] for n in names}
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    # Two sidecar shapes date a post: instaloader's
                    # `<name>.json`, and yt-dlp's `<id>.info.json` for TikTok.
                    # The second was invisible here, so a TikTok video's date
                    # came from its mtime and never noticed its metadata change.
                    if entry.name.endswith(".info.json"):
                        key = entry.name[:-len(".info.json")]
                    elif entry.name.endswith(".json"):
                        key = entry.name[:-len(".json")]
                    else:
                        continue
                    if key not in stem_set:
                        continue
                    est = entry.stat()
                except OSError:
                    continue
                sidecars += 1
                if est.st_mtime_ns > newest:
                    newest = est.st_mtime_ns
    except OSError:
        return None
    for name in names:
        try:
            m = (folder / name).stat().st_mtime_ns
        except OSError:
            continue
        if m > newest:
            newest = m
    return (st[1], st[2], newest, len(names) + sidecars)


def _dir_stamp(folder: Path) -> tuple | None:
    try:
        st = folder.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _scan_dir(folder: Path) -> tuple[list[str], int] | None:
    """One pass over the folder: media names and the newest entry mtime.

    Everything the listing needs comes from this single scandir, so the cache
    key and the listing are never paid for twice.
    """
    names = []
    newest = 0
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if entry.name == INDEX_FILE:
                    continue          # our own bookkeeping, not archive content
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if st.st_mtime_ns > newest:
                    newest = st.st_mtime_ns
                if not entry.is_file():
                    continue
                if os.path.splitext(entry.name)[1].lower() not in GALLERY_EXTS:
                    continue
                if st.st_size == 0:   # truncated, or emptied by the trash flow
                    continue
                names.append(entry.name)
    except OSError:
        return None
    names.sort()
    return names, newest


def _gallery_media(folder: Path) -> list[str]:
    """Names of displayable media directly inside `folder` (sorted).

    Cached in three layers, cheapest first: memory, the SQLite mirror, then a
    real scan. The question the cache has to answer is "could anything have
    changed since I looked", and the cheap, correct way to ask that is the
    directory's own mtime: adding, deleting, renaming or replacing a file
    updates it. Rewriting a file's CONTENTS does not, which is why the cached
    entry also remembers the newest entry mtime and the entry count -- but that
    is only re-read when the directory itself looks unchanged but old enough to
    be worth confirming.
    """
    key = str(folder)
    stamp = _dir_stamp(folder)
    if stamp is None:
        _MEDIA_CACHE.pop(key, None)
        _cache.drop(key)
        return []

    hit = _MEDIA_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]

    # Memory is empty on the first request after a restart, so ask the SQLite
    # mirror before rescanning. It is accepted only when the directory metadata
    # still matches; the looser newest-mtime check is applied after a rescan.
    disk = _cache.get(key, stamp)
    if disk is not None:
        if len(_MEDIA_CACHE) >= _MEDIA_CACHE_MAX:
            _MEDIA_CACHE.clear()
        _MEDIA_CACHE[key] = (stamp, disk)
        return disk

    scanned = _scan_dir(folder)
    if scanned is None:
        return []
    names, _newest = scanned

    if len(_MEDIA_CACHE) >= _MEDIA_CACHE_MAX:
        _MEDIA_CACHE.clear()
    _MEDIA_CACHE[key] = (stamp, names)
    _cache.put(key, stamp, names)
    return names


def _parse_filename_date(name: str) -> float:
    """Epoch seconds from instaloader's `<YYYY-MM-DD>_<HH-MM-SS>_UTC.ext`."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})", name)
    if not m:
        return 0.0
    try:
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
        return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
    except ValueError:
        return 0.0


def _exif_date(path: Path) -> tuple[float, str]:
    """Epoch seconds from a photo's EXIF capture time, or (0.0, "").

    This is the date the picture was *taken*, which is not the posting date and
    is much better than the date the file was copied here: a photo added by hand
    keeps its EXIF, while the copy carries only today's timestamp.

    EXIF stores no timezone, so the value is read as UTC -- the same clock the
    rest of the app prints. A camera whose clock was wrong will be wrong here
    too; this reports what the file says rather than guessing.

    Called only when nothing better exists (no sidecar, no filename stamp), so
    it costs nothing on the thousands of downloaded posts that never reach it.
    """
    try:
        from PIL import Image
    except ImportError:
        return 0.0, ""
    raw = ""
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            # DateTimeOriginal is when the shutter fired; the others are
            # fallbacks for editors that only write those.
            for tag in (36867, 36868, 306):
                val = exif.get(tag)
                if val:
                    raw = str(val).strip()
                    break
    except (OSError, ValueError, TypeError):
        return 0.0, ""
    m = re.match(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", raw)
    if not m:
        return 0.0, ""
    try:
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
        ts = float(calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)))
    except ValueError:
        return 0.0, ""
    # A date before 2000 or after tomorrow is a wrong clock, not evidence.
    if not (946684800 < ts < time.time() + 86400):
        return 0.0, ""
    return ts, "exif"


def _tiktok_info(folder: Path, name: str) -> tuple[float, str, str, str]:
    """Date, caption and title for one TikTok file downloaded by yt-dlp.

    A TikTok filename is the numeric video id, so there is no date in the name
    and the generic parser below used to fall straight through to the file's
    mtime -- which stamps every video with the day it was downloaded rather
    than the day it was posted. Two real sources exist:

      1. `<id>.info.json`, the metadata yt-dlp writes beside the video. It
         carries `timestamp` (epoch seconds) and `upload_date` (YYYYMMDD), both
         the posting time; `upload_date` is date-only, so it lands at midnight.
      2. the video id itself. TikTok ids are snowflake ids, the high bits are
         the creation time in seconds, so `id >> 32` recovers it. Measured
         against 21 sampled sidecars with no disagreement over a day, which
         makes it a sound fallback when the sidecar is missing.

    Returns (ts, ts_src, caption, title). ts is 0.0 when neither source works,
    leaving the caller's mtime fallback to apply.
    """
    stem = os.path.splitext(name)[0]
    ts = 0.0
    src = ""
    caption = ""
    title = ""
    try:
        node = json.loads((folder / (stem + ".info.json")).read_bytes()) or {}
    except (OSError, ValueError, TypeError):
        node = {}
    if isinstance(node, dict):
        try:
            ts = float(node["timestamp"])
        except (KeyError, TypeError, ValueError):
            ts = 0.0
        if ts <= 0:
            day = str(node.get("upload_date") or "")
            if len(day) == 8 and day.isdigit():
                try:
                    ts = float(calendar.timegm((int(day[:4]), int(day[4:6]),
                                                int(day[6:]), 0, 0, 0, 0, 0, 0)))
                except ValueError:
                    ts = 0.0
        if ts > 0:
            src = "json"
        cap = node.get("description") or node.get("title") or ""
        if isinstance(cap, str):
            caption = cap.strip()
        ttl = node.get("title") or ""
        if isinstance(ttl, str):
            title = ttl.strip()
    if not ts and stem.isdigit():
        # Snowflake fallback. Bounded to 2016-01-01 .. a day from now so that a
        # numeric filename which is not a TikTok id cannot invent a date.
        guess = int(stem) >> 32
        if 1451606400 <= guess <= time.time() + 86400:
            ts, src = float(guess), "id"
    return ts, src, caption, title


def _media_info(folder: Path, name: str, platform: str = "instagram") -> dict:
    """One gallery item: name, kind, timestamp and optional caption.

    Three sources, best first, and each one is recorded so the UI can be honest
    about what it is showing:

      1. the sidecar JSON beside each post -- the true posting date and the
         caption. Instagram's is `<name>.json` with the fields under `node`;
         TikTok's is yt-dlp's `<id>.info.json`, read by _tiktok_info;
      2. instaloader's filename stamp, `<YYYY-MM-DD>_<HH-MM-SS>_UTC.ext`;
      3. the file's own mtime.

    Step 3 exists because a folder of your own photos has neither of the first
    two: without it every such file was ts=0, which sorted as "no date" and
    showed a blank label. Before the file's mtime is used, a photo's EXIF
    capture time is tried (see _exif_date) -- that is the date taken, which is
    what a hand-added picture should show. What is left is marked
    `ts_src: "mtime"` and the UI writes it with a `~`, rather than passing a
    guess off as a fact.
    """
    path = folder / name
    kind = "video" if path.suffix.lower() in GALLERY_VIDEO_EXTS else "image"
    caption = ""
    title = ""
    is_video = None
    owner = ""
    if platform == "tiktok":
        ts, ts_src, caption, title = _tiktok_info(folder, name)
    else:
        ts = _parse_filename_date(name)
        ts_src = "name" if ts else ""
        try:
            raw = path.with_suffix(".json").read_bytes()
            node = (json.loads(raw) or {}).get("node") or {}
            if node.get("date"):
                ts = float(node["date"])
                ts_src = "json"
            cap = node.get("caption")
            if isinstance(cap, str):
                caption = cap.strip()
            title = (node.get("title") or "").strip()
            if node.get("is_video") is not None:
                is_video = bool(node["is_video"])
        except (OSError, ValueError, TypeError):
            pass
    if not ts and kind == "image":
        # Date taken beats date copied: a hand-added photo keeps its EXIF but
        # carries today's timestamp on the copy.
        ts, ts_src = _exif_date(path)
    if not ts:
        # No sidecar, no instaloader stamp: fall back to the file's mtime so the
        # item still sorts into the timeline instead of landing in a "no date"
        # pile. A directory read failure here leaves it undated, as before.
        try:
            ts = path.stat().st_mtime
            ts_src = "mtime"
        except OSError:
            ts = 0.0
            ts_src = ""
    if is_video is not None:
        kind = "video" if is_video else "image"
    post_id = name.split("_UTC")[0]
    return {
        "file": name,
        "kind": kind,
        "ts": ts,
        "ts_src": ts_src,
        "date": (datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
                 .strftime("%Y-%m-%d %H:%M UTC") if ts else ""),
        "caption": caption[:300],
        "title": title[:200],
        "post": post_id,
    }


# instaloader names a carousel's parts `<stamp>_UTC_1.jpg`, `_2.jpg`, ...
# A single-item post has no numeric suffix at all. This matches both.
#
# The stamp is also matched WITHOUT the extension on purpose. A carousel can mix
# photos and videos, and instaloader numbers every part in one sequence whatever
# its type: a 20-part post is `_1.jpg ... _16.jpg, _17.mp4 ... _20.mp4`. Grouping
# on (stamp, extension) split those into two posts -- the photos and the videos --
# which is why a carousel looked like one item, or like "the rest of" a post
# appearing separately. 1,939 posts in a real archive were split this way.
POST_RE = re.compile(r"^(?P<stamp>.+?_UTC)(?:_(?P<part>\d+))?\.(?P<ext>[A-Za-z0-9]+)$")

# A file with no instaloader stamp: a phone upload, a saved image, a profile
# picture. `base_2.jpg` may be the second part of `base.jpg`, but `base` alone
# cannot join it on the strength of a shared prefix -- `IMG_0860.JPG` and
# `IMG_0885.JPG` are two different photos that happen to start with IMG. So a
# run only counts as a carousel when the bare file exists and the numbered ones
# are exactly 2, 3, 4 ... with nothing missing and nothing extra.
NUMTAIL_RE = re.compile(r"^(?P<base>.+?)_(?P<part>\d+)\.(?P<ext>[A-Za-z0-9]+)$")
PLAIN_RE = re.compile(r"^(?P<base>.+?)\.(?P<ext>[A-Za-z0-9]+)$")


def _post_key(name: str) -> tuple[str, int]:
    """(post stamp, part number) for a media filename."""
    m = POST_RE.match(name)
    if not m:
        return (name, 1)
    return (m.group("stamp"), int(m.group("part") or 1))


def group_into_posts(names: list[str]) -> dict[str, list[tuple[int, str]]]:
    """Group media filenames into posts: {title: [(part, filename), ...]}.

    Two rules, and the order matters.

    1. An instaloader stamp is authoritative. Every file sharing `<...>_UTC` is
       one post, even when its parts mix `.jpg` and `.mp4`, because instaloader
       numbers them in a single sequence across both.

    2. Anything unstamped is grouped only when the parts really are a run: the
       bare `base.ext` must exist and the numbered files must be 2, 3, 4 ...
       with no gap. Files that only share a prefix (`IMG_0860`, `IMG_0885`)
       deliberately stay separate -- merging those would invent posts that were
       never posts.
    """
    posts: dict[str, list[tuple[int, str]]] = {}
    unstamped: list[str] = []

    for name in names:
        m = POST_RE.match(name)
        if m:
            posts.setdefault(m.group("stamp"), []).append(
                (int(m.group("part") or 1), name))
        else:
            unstamped.append(name)

    # base -> {"bare": filename|None, "parts": {n: filename}}
    cand: dict[tuple[str, str], dict] = {}
    for name in unstamped:
        m = NUMTAIL_RE.match(name)
        if m:
            key = (m.group("base"), m.group("ext").lower())
            cand.setdefault(key, {"bare": None, "parts": {}})
            cand[key]["parts"][int(m.group("part"))] = name
        else:
            m2 = PLAIN_RE.match(name)
            key = ((m2.group("base"), m2.group("ext").lower()) if m2 else (name, ""))
            cand.setdefault(key, {"bare": None, "parts": {}})
            if cand[key]["bare"] is None:
                cand[key]["bare"] = name

    for (base, _ext), d in cand.items():
        nums = sorted(d["parts"])
        complete = (d["bare"] is not None
                    and nums == list(range(2, len(nums) + 2)))
        if complete:
            posts[base] = [(1, d["bare"])] + [(n, d["parts"][n]) for n in nums]
        else:
            # Not a run: every file stands on its own, titled by its own name so
            # the grid never shows thirty tiles all labelled "IMG".
            if d["bare"] is not None:
                posts[base] = [(1, d["bare"])]
            for n in nums:
                posts[d["parts"][n]] = [(1, d["parts"][n])]

    return posts


def _resolve(p: Path) -> Path | None:
    """Resolved path, or None when it cannot be resolved (unmounted drive)."""
    try:
        return p.resolve()
    except OSError:
        return None


def _same(a: Path, b: Path) -> bool:
    """True when two paths name the same place.

    Compared RESOLVED, because one directory can be reached under more than one
    spelling (a symlinked home, for instance), and a textual compare would let
    the same folder into the list twice and then refuse to remove it.
    Falls back to a textual compare when either path cannot be resolved.
    """
    ra, rb = _resolve(a), _resolve(b)
    if ra is None or rb is None:
        return a == b
    return ra == rb


def tiktok_roots(root: Path) -> tuple[Path, ...]:
    """Candidate folders holding TikTok archives for one root, best first.

    TikTok lives BESIDE the Instagram folder, not inside it: `<parent>/tiktok`
    next to `<parent>/instagram`. It used to be `<root>/tiktok`, which made
    TikTok read as a sub-folder of the Instagram archive rather than a peer of
    it -- the root is named after instaloader, so nesting TikTok inside it
    wrongly suggested TikTok was part of Instagram.

    Unless the root IS a mount point. Then `<parent>` is a different disk, and a
    sibling `tiktok` folder would put TikTok on the system drive while the
    Instagram archives it belongs with sit on the mounted one. A mounted drive
    keeps its TikTok folder inside itself instead. This is checked rather than
    hardcoded, so the same code is correct whether someone points downloads at
    `/mnt/bigdrive/instagram` (sibling, same disk) or straight at `/mnt/bigdrive`
    (nested, because the sibling would land elsewhere).

    The old nested location is kept as a second candidate so an archive still in
    the old shape -- a backup, a second drive, or a move that was interrupted --
    is still read instead of silently disappearing from the gallery.

    This is the ONE definition of the rule. It used to be written out separately
    in the gallery's profile scan, and that copy kept looking in the old spot
    after the layout changed, so TikTok profiles vanished from the gallery while
    everything else worked.

    A sibling folder is only used when this app can tell it belongs here: it must
    look like a TikTok archive, meaning it holds the marker this app drops when
    it creates the folder, or at least one child that contains a media file or a
    `.yt-dlp-archive.txt`. An unrelated directory that merely happens to be named
    `tiktok` -- a game's cache, a dataset, someone's notes -- is therefore left
    alone, and the archive stays inside the root rather than scattering into it.
    Without that check, "there is a folder called tiktok next to Downloads" was
    enough to have it adopted and listed in the gallery.
    """
    root = Path(root)
    nested = root / "tiktok"
    try:
        same_disk = os.stat(root).st_dev == os.stat(root.parent).st_dev
    except OSError:
        # Root missing, or unreadable: prefer the location inside it, which is
        # the one that cannot escape onto a different volume.
        same_disk = False
    if not same_disk:
        return (nested,)

    sibling = root.parent / "tiktok"
    if _looks_like_tiktok_archive(sibling):
        return (sibling, nested)
    # Either it is not there yet (nothing to decide), or it is there and is not
    # ours. Both end up inside the root, so a first download still lands next to
    # the Instagram folder while a stranger's directory is never touched.
    if not sibling.exists():
        return (sibling, nested)
    return (nested,)


TIKTOK_MARKER = ".hardlywatching-tiktok"


def _looks_like_tiktok_archive(folder: Path) -> bool:
    """True when `folder` is a TikTok archive this app made, or clearly grew into.

    Deliberately shallow and bounded: the marker file, or one child folder that
    holds something recognisably downloaded. It never walks deeper than that, so
    pointing downloads at a big drive cannot turn into a slow scan on every
    gallery request.
    """
    try:
        if not folder.is_dir():
            return False
        if (folder / TIKTOK_MARKER).exists():
            return True
        for entry in os.scandir(folder):
            if not entry.is_dir():
                continue
            if (Path(entry.path) / ".yt-dlp-archive.txt").exists():
                return True
            try:
                with os.scandir(entry.path) as kids:
                    for kid in kids:
                        if not kid.is_file():
                            continue
                        if os.path.splitext(kid.name)[1].lower() in TIKTOK_EXTS:
                            return True
            except OSError:
                continue
        return False
    except OSError:
        return False


def _platform_of_folder(folder: Path) -> str:
    """Which platform a profile folder belongs to, from where it sits.

    TikTok archives live under a `tiktok` directory, so the name of the folder
    one (or two, for its own trash/ and favorites/ subfolders) levels up is the
    whole answer. Deliberately bounded: matching "tiktok" anywhere in the path
    would misfire on a drive that happens to be called tiktok-something.

    Derived rather than threaded in, because the callers that record tombstones
    only ever have the folder.
    """
    try:
        f = Path(folder)
    except Exception:
        return "instagram"
    for up in (f.parent, f.parent.parent):
        if up.name == "tiktok":
            return "tiktok"
    return "instagram"


def _existing_root(self, username: str, platform: str = "instagram") -> Path | None:
    """The archive root `username` already lives on, or None.

    Same scan as root_for(), but returns the root directory rather than the
    profile's folder, so a job can be pinned to the drive its archive is on.
    """
    for root in self.roots:
        if platform == "tiktok":
            if any((c / username).is_dir() for c in tiktok_roots(root)):
                return root
        elif (root / username).is_dir():
            return root
    return None


def root_for(self, username: str, platform: str = "instagram") -> Path | None:
    """The folder an archive for `username` actually lives in, or None.

    Scans the roots in order and returns the first one holding a `username/`
    directory. An unmounted root simply does not match, so a profile on a
    drive that is unplugged disappears from the gallery and comes back on its
    own once the drive is mounted again -- no rescan, no stored state.

    Deterministic on purpose: if the same profile exists in two roots the
    first-listed root wins, so a stray copy can never make the archive
    silently change under you.

    TikTok archives live beside the Instagram folder, in `<parent>/tiktok`, so
    they are looked up there. Keeping the platforms in separate sibling folders
    means a TikTok handle and an Instagram profile of the same name cannot
    resolve to the same directory.
    """
    for root in self.roots:
        if platform == "tiktok":
            for candidate in tiktok_roots(root):
                folder = candidate / username
                if folder.is_dir():
                    return folder
        else:
            folder = root / username
            if folder.is_dir():
                return folder
    return None


def gallery_posts(self, username: str, section: str = "main",
                  platform: str = "instagram") -> list[dict]:
    """Group one section's media into posts, carousel parts in order.

    Each post is one Instagram post: a single image/video, or a carousel of
    up to 20. Parts are ordered by their numeric suffix, not by string --
    otherwise `_10` would sort before `_2`. A TikTok profile has no carousels
    or sidecars, so every video simply becomes its own post.
    """
    base = self.root_for(username, platform)
    if base is None:
        return []
    folder = base
    if section in SUBDIRS:
        folder = folder / section
    if not folder.is_dir():
        return []

    # Catch anything deleted from the file manager since we last looked, so a
    # manual delete also leaves a placeholder. Only for the main folder.
    if section not in SUBDIRS:
        try:
            self.reconcile_deletions(username)
        except Exception:
            pass

    grouped: dict[str, list[dict]] = {}
    names = _gallery_media(folder)

    # Grouped posts are cached here because building them is the expensive part:
    # one sidecar JSON read per file, measured at 1.27s of 1.63s for 20 profiles
    # (21,016 files). The all-profiles timeline would otherwise re-read 30k
    # sidecars on every request. Keyed on a stamp that includes the newest file
    # mtime, so an edited sidecar invalidates it.
    stamp = _posts_stamp(folder, names)
    if stamp is not None:
        hit = _cache.get_posts(str(folder), stamp)
        if hit is not None:
            hit.sort(key=lambda p: p["ts"])
            return hit

    # Group filenames first (see group_into_posts), then describe each file. The
    # grouping is by post, not by extension, so a carousel that mixes photos and
    # videos stays one post -- it used to split into two.
    for stamp_name, parts in group_into_posts(names).items():
        for part, name in parts:
            # A zero-byte file is not a part. These come from an interrupted
            # download, or from reconcile_deletions writing a placeholder so the
            # post is not re-fetched. The tile already hid them (see _scan_dir),
            # but the grouping counted them, so a carousel claimed more parts
            # than it had and the viewer walked dead slots -- a blank box, or a
            # photo silently skipped between two others.
            try:
                if (folder / name).stat().st_size == 0:
                    continue
            except OSError:
                continue
            info = _media_info(folder, name, platform)
            info["part"] = part
            grouped.setdefault(stamp_name, []).append(info)

    posts = []
    for stamp_name, items in grouped.items():
        items.sort(key=lambda i: i["part"])
        first = items[0]
        # Only the fields the grid and viewer actually read are kept per item.
        # _media_info() also produces ts/date/caption/title/post, but every one
        # of those is already on the post below and measured identical to it
        # across 10,648 items -- repeating them on each part was 30% of the
        # payload for a large profile (1.8MB of JSON to draw one screen).
        # Which part numbers are absent. A carousel that lost part 3 should say
        # so: the viewer numbers what it has, and this lets it mention the hole
        # instead of appearing to skip a photo for no reason.
        present = sorted(i["part"] for i in items)
        missing_parts = [n for n in range(1, (max(present) if present else 0) + 1)
                         if n not in present]
        posts.append({
            "post": stamp_name,
            "missing_parts": missing_parts,
            "ts": first["ts"],
            # Where the date came from: json | name | mtime | "". The UI marks
            # mtime-dated items, because that is the file's timestamp rather
            # than when the photo was actually taken.
            "ts_src": first.get("ts_src", ""),
            "date": first["date"],
            "caption": first["caption"],
            "title": first["title"],
            "items": [{"file": i["file"], "kind": i["kind"], "part": i["part"]}
                      for i in items],
            "count": len(items),
        })
    posts.sort(key=lambda p: p["ts"])
    if stamp is not None:
        _cache.put_posts(str(folder), stamp, posts)
    return posts


def timeline(self, months: int | None = None,
             only_favorites: bool = False,
             profile: str | None = None) -> dict:
    """Every profile's posts in one chronological list, month-grouped.

    Returns the months present with their counts, and the posts, so the UI
    can build a slider over the months and ask for one month at a time
    instead of holding 30,000 posts in the browser. A post carries its
    profile so a tile can still say who it belongs to.

    This is the all-profiles view: it reads only the main folder of each
    profile, because merging favourites and trash into "everything" would
    double-count posts that are also in main.

    `profile` narrows the walk to one profile, which is what the timeline
    shows when it is opened from inside a profile's own grid. Matching is
    case-insensitive and ignores surrounding space, because the name arrives
    from a URL.
    """
    raw = (profile or "").strip()
    want = raw.lower() or None
    if want:
        # One name is one card, and a card can stand for more than one folder:
        # a merged account keeps its other handles' posts in their own folders,
        # which is what the grid draws. Walk the family so the timeline of a
        # card matches the card. Only the Instagram members: this view reads
        # post folders on the Instagram root, exactly as the all-profiles view
        # does, so the TikTok side of a merged card is not part of it.
        sources = []
        seen: set[str] = set()
        for who, plat in self.profile_family(raw, "instagram"):
            # Only the Instagram members: the same-name rule pairs a handle with
            # its twin on the other site, and reading that twin's folder as an
            # Instagram profile would count the card's posts twice.
            if not who or (plat or "").lower() != "instagram":
                continue
            key = who.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            sources.append((who, "instagram"))
    else:
        sources = [(p.get("username"), "instagram")
                   for p in self.gallery_profiles() if p.get("username")]
    out: list[dict] = []
    if only_favorites:
        # Favourites-only timeline. These live in each profile's favorites/
        # subfolder rather than main/, so they are read from there; the same
        # post is never listed twice because only one folder is walked.
        for username, plat in sources:
            try:
                favs = self.gallery_posts(username, "favorites", plat)
            except Exception:
                continue
            for p in favs:
                out.append({
                    "username": username,
                    "post": p["post"],
                    "ts": p["ts"],
                    "ts_src": p.get("ts_src", ""),
                    "date": p["date"],
                    "caption": p["caption"][:160],
                    "kind": "video" if any(i["kind"] == "video"
                                           for i in p["items"]) else "image",
                    "file": p["items"][0]["file"] if p["items"] else "",
                    "count": p["count"],
                    "favorite": True,
                    "section": "favorites",
                })
    else:
        for username, plat in sources:
            try:
                posts = self.gallery_posts(username, "main", plat)
            except Exception:
                continue
            # Which posts are ALSO favourited, so a timeline tile can show the
            # heart already filled and toggle it without a second request.
            try:
                fav_ids = {p["post"] for p in self.gallery_posts(username, "favorites", plat)}
            except Exception:
                fav_ids = set()
            for p in posts:
                # Only what a timeline tile needs; the viewer fetches the rest
                # when a post is actually opened.
                out.append({
                    "username": username,
                    "post": p["post"],
                    "ts": p["ts"],
                    "ts_src": p.get("ts_src", ""),
                    "date": p["date"],
                    "caption": p["caption"][:160],
                    "kind": "video" if any(i["kind"] == "video"
                                           for i in p["items"]) else "image",
                    "file": p["items"][0]["file"] if p["items"] else "",
                    "count": p["count"],
                    "favorite": p["post"] in fav_ids,
                    "section": "main",
                })
    out.sort(key=lambda p: p["ts"])
    groups: dict[str, int] = {}
    for p in out:
        if not p["ts"]:
            key = "0000-00"
        else:
            key = datetime.datetime.fromtimestamp(
                p["ts"], datetime.timezone.utc).strftime("%Y-%m")
        groups[key] = groups.get(key, 0) + 1
    # Keep the built list so a request for one month does not walk every
    # profile again. This is the same data the post cache already holds, so
    # it costs one list reference rather than 7.6MB of JSON per request.
    self._timeline_all = out
    self._timeline_groups = groups
    self._timeline_favs = bool(only_favorites)
    # Which profile this list was built for, so a month request for a different
    # scope cannot be answered from the wrong cache.
    self._timeline_profile = want
    return {"months": groups, "total": len(out), "profile": want or ""}

def timeline_month(self, month: str, offset: int = 0,
                   limit: int = 200, only_favorites: bool = False,
                   profile: str | None = None) -> dict:
    """One month's posts from the timeline, and how many there are in all.

    The UI drives a slider over the months and asks for just the one it is
    showing. Returning all 30,000 posts to draw one month was 7.6MB and 1.5s
    per request; a month is a few hundred posts.

    `only_favorites` and `profile` must be passed through: the built list is
    cached, and each combination is a DIFFERENT list, so reusing the cached
    list here would quietly serve every post while the UI said something else.
    """
    want = (profile or "").strip().lower() or None
    allp = getattr(self, "_timeline_all", None)
    stale = (getattr(self, "_timeline_favs", None) != bool(only_favorites)
             or getattr(self, "_timeline_profile", None) != want)
    if allp is None or not getattr(self, "_timeline_groups", None) or stale:
        self.timeline(only_favorites=only_favorites, profile=profile)
        allp = getattr(self, "_timeline_all", []) or []
    if month == "all":
        chosen = allp
    else:
        chosen = []
        for p in allp:
            key = ("0000-00" if not p["ts"] else
                   datetime.datetime.fromtimestamp(
                       p["ts"], datetime.timezone.utc).strftime("%Y-%m"))
            if key == month:
                chosen.append(p)
    total = len(chosen)
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 200), 600))
    page = chosen[offset:offset + limit]
    return {"month": month, "posts": page, "total": total,
            "offset": offset, "limit": limit,
            "has_more": offset + len(page) < total}



# Subfolders inside a profile's own directory. Their location IS the state:
# no sidecar file to fall out of sync with what is actually on disk.
FAVORITES_DIR = "favorites"
TRASH_DIR = "trash"
SUBDIRS = (FAVORITES_DIR, TRASH_DIR)

# A dotfile inside each profile folder listing the media that was there last
# time we looked. It lets us notice files you deleted from the file manager and
# leave a placeholder for them, so they are not silently re-downloaded. A
# dotfile is invisible to instaloader and to _gallery_media (extension filter).
INDEX_FILE = ".insta-index.json"


def _read_index(root: Path) -> dict:
    try:
        raw = (root / INDEX_FILE).read_bytes()
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_index(root: Path, index: dict) -> None:
    try:
        tmp = root / (INDEX_FILE + ".tmp")
        tmp.write_text(json.dumps(index, separators=(",", ":")))
        tmp.replace(root / INDEX_FILE)
    except OSError:
        pass


def _to_desktop_trash(path: Path) -> None:
    """Move `path` to the desktop trash bin, so a mistake is recoverable.

    Linux: `gio trash` (GNOME) then `trash-put` (trash-cli).
    macOS: Finder's Trash via osascript.
    Windows: the Recycle Bin via PowerShell.
    If none is available, falls back to a plain unlink so the delete the user
    asked for still happens.
    """
    cmds: list[list[str]] = []
    if os.name == "nt":
        ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
              "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
              f"'{path}', 'OnlyErrorDialogs', 'SendToRecycleBin')")
        cmds = [["powershell", "-NoProfile", "-Command", ps]]
    elif sys.platform == "darwin":
        cmds = [["osascript", "-e",
                 f'tell application "Finder" to delete POSIX file "{path}"']]
    else:
        cmds = [["gio", "trash"], ["trash-put"]]

    for cmd in cmds:
        try:
            r = subprocess.run(cmd if cmd[0] in ("osascript", "powershell") else cmd + [str(path)],
                               timeout=30,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0 and not path.exists():
                return
        except (OSError, subprocess.SubprocessError):
            continue
    try:
        path.unlink()
    except OSError:
        pass


def reconcile_deletions(self, username: str) -> list[str]:
    """Leave a placeholder for media deleted outside the app.

    Compares the profile's main folder against the remembered index. Any file
    that was there and is now gone (deleted from the file manager, or by any
    means other than our own trash flow) gets a 0-byte placeholder of the same
    name and a tombstone entry, so instaloader's fast-update skips it. Then the
    index is rewritten to the current listing.

    Returns the list of names newly tombstoned. Cheap: one small JSON read and
    one directory listing per call.
    """
    root = self.root_for(username)
    if root is None:
        return []
    index = _read_index(root)
    current = set(_gallery_media(root))
    previously = set(index.get("media") or [])

    # A move is not a deletion. Favouriting and trashing relocate a file, and
    # anything now sitting in favorites/ or trash/ must not be tombstoned:
    # otherwise un-favouriting has to fight an empty placeholder that shadows
    # the real file, and a lost race here is what left those placeholders in the
    # first place.
    moved: set[str] = set()
    for sub in (FAVORITES_DIR, TRASH_DIR):
        try:
            moved.update(p.name for p in (root / sub).iterdir() if p.is_file())
        except OSError:
            pass
    vanished = sorted(n for n in (previously - current) if n not in moved)
    added: list[str] = []
    if vanished:
        with self.lock:
            known = {(t.get("username"), t.get("name")) for t in self.tombstones}
            for name in vanished:
                placeholder = root / name
                try:
                    if not placeholder.exists():
                        placeholder.write_bytes(b"")
                    if (username, name) not in known:
                        self.tombstones.append({
                            "username": username, "name": name, "at": time.time(),
                            # Which platform this belongs to. A handle can exist on
                            # both, and matching is by (username, name), so without
                            # this a TikTok delete could match an Instagram entry.
                            "platform": _platform_of_folder(root),
                        })
                        added.append(name)
                except OSError:
                    continue
            if len(self.tombstones) > 5000:
                del self.tombstones[:-5000]
        if added:
            _media_cache_forget(root)
            self.save_state()

    # Remember what is there now (only real media, never placeholders).
    #
    # Written ONLY when it differs. Rewriting it on every call changed the
    # profile folder's mtime every time, which invalidated the listing cache
    # (keyed on that mtime) on every single request -- the gallery went from
    # 3ms to 1s, and the timeline from 0.7s to 5.8s, purely from this rewrite.
    now_media = sorted(current)
    if now_media != sorted(previously):
        _write_index(root, {"media": now_media})
    return added


def gallery_sections(self, username: str, platform: str = "instagram") -> dict:
    """Per-section gallery posts for one profile: main, favorites, trash.

    Media is grouped into posts, so a carousel's parts stay together and can
    be flipped through in the viewer. Nothing is sampled away.

    Each post carries a `section` so the UI can merge favorites into the main
    grid (a favourited photo stays visible in All, marked as favourited)
    without losing track of where the file physically lives.
    """
    out = {}
    # A merged account's media belongs here too. Every post keeps the profile it
    # actually came from, because moving or favouriting it has to act on that
    # folder -- the file is not in the primary's directory.
    family = self.profile_family(username, platform)
    for key in ("main", "favorites", "trash"):
        posts = []
        for who, who_plat in family:
            for p in self.gallery_posts(who, key, who_plat):
                p["section"] = key
                p["profile"] = who
                # The post keeps the platform of the account it actually came
                # from: a merged card shows two sites at once, and moving or
                # trashing a file has to act on the folder it is really in.
                p["platform"] = who_plat
                posts.append(p)
        posts.sort(key=lambda p: p.get("ts") or 0)
        out[key] = posts
    return out


# A profile is listed in the gallery once it has files on disk, even while it
# is still downloading. Previously only status=="done" was listed, which was
# fine when a queued profile had nothing to show -- but downloads now land
# immediately, so a profile mid-run had hundreds of files and was invisible
# until it finished. Its folder is scanned live, so the tiles fill in as it
# goes; the badge tells the UI which ones are still moving.
GALLERY_LIVE_STATUSES = {"running", "retrying", "queued", "paused"}


def _real_profile_name(self, username: str, platform: str = "instagram") -> str | None:
    """The folder's own spelling of a handle, or None if that account is absent.

    A match is case-insensitive: a handle is not case-sensitive on either site and
    the folder may not be spelled the way it was typed.
    """
    want = (username or "").strip().lower()
    if not want:
        return None
    for root in self.roots:
        dirs = tiktok_roots(root) if platform == "tiktok" else [root]
        for c in dirs:
            try:
                with os.scandir(c) as it:
                    for entry in it:
                        try:
                            if entry.is_dir() and entry.name.lower() == want:
                                return entry.name
                        except OSError:
                            continue
            except OSError:
                continue
    return None


def profile_family(self, username: str, platform: str = "instagram"
                   ) -> list[tuple[str, str]]:
    """[(handle, platform)] one gallery entry stands for, the card's own first.

    Links live in profiles.db, which is also what the gallery reads, so a merge
    survives a restart without anything else having to remember it.
    """
    import profile_db as _pdb
    db = _pdb._shared_db()
    plat = (platform or "instagram").lower()
    family = list(db.linked_family(username, plat))
    if not family:
        family = [(username, plat)]
    # The same-name rule: an identical handle on the other site is the same
    # person. Skipped for anything split by hand -- that split is an instruction,
    # and putting it back would undo a decision every time the page loaded.
    base, base_plat = family[0]
    if base.lower() not in db.profile_splits():
        for other in ("instagram", "tiktok"):
            if other == base_plat:
                continue
            real = self._real_profile_name(base, other)
            if real and not any(h.lower() == real.lower() and p == other
                                for h, p in family):
                family.append((real, other))
    return family


def gallery_profiles(self) -> list[dict]:
    """Profiles with files on disk, including ones still downloading."""
    out = []
    with self.lock:
        jobs = list(self.jobs.values())
    # One entry per person: a profile has up to three jobs (main, reels,
    # tagged) and the main one decides the folder and the status, so the
    # others never produce a second entry.
    # Keyed by (username, platform) so an Instagram profile and a TikTok
    # account sharing a handle do not overwrite each other's entry.
    mains = {}
    for job in jobs:
        if job.kind != "main":
            continue
        mains[(job.username, getattr(job, "platform", "instagram"))] = job
    # A profile can also exist on disk with no job at all (an archive copied
    # in by hand), which the roots still know about.
    on_disk = []
    tt_on_disk = []
    for root in self.roots:
        try:
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        if not entry.is_dir():
                            continue
                        # `tiktok` is not a profile itself. Its children are, and
                        # they are listed below from wherever TikTok actually
                        # lives for this root.
                        if entry.name == "tiktok":
                            continue
                        on_disk.append(entry.name)
                    except OSError:
                        continue
        except OSError:
            continue
        # TikTok archives sit BESIDE this root. The first candidate that exists
        # wins, so an archive left in the old nested shape is still listed and
        # nothing is counted twice.
        for tt_dir in tiktok_roots(root):
            if not tt_dir.is_dir():
                continue
            try:
                with os.scandir(tt_dir) as sub:
                    for s in sub:
                        try:
                            if s.is_dir():
                                tt_on_disk.append(s.name)
                        except OSError:
                            continue
            except OSError:
                pass
            break
    # Each profile is looked up under its own platform, so an Instagram profile
    # and a TikTok account sharing a handle stay separate entries on disk.
    wanted = [(u, "instagram") for u in on_disk] + [(u, "tiktok") for u in tt_on_disk]
    seen = set()
    for username, platform in wanted:
        key = (username, platform)
        if key in seen:
            continue
        seen.add(key)
        job = mains.get(key)
        # A root that is not mounted resolves to None here, so profiles on an
        # unplugged drive are simply absent from the list -- they reappear on
        # the next request once the drive is back.
        folder = self.root_for(username, platform)
        if folder is None:
            continue
        media = _gallery_media(folder)
        if not media:
            continue
        status = job.status if job else "done"
        out.append({
            "username": username,
            "platform": platform,
            "id": job.id if job else username,
            "count": len(media),
            "posts": job.posts if job else len(media),
            "root": str(folder.parent),
            "status": status,
            "downloading": status in GALLERY_LIVE_STATUSES,
            # Progress within the current run, for the tile's progress hint.
            "done_count": job.done_count if job else 0,
            "total_count": job.total_count if job else 0,
        })
    # Fold merged accounts into one entry.
    #
    # Gallery view only: the folders, the jobs and the downloads are untouched.
    # This is the one place that decides a card stands for more than one handle.
    #
    # Two rules, in this order:
    #   1. what was asked for -- the merge table, whether that is a burner on one
    #      site or the same person on both;
    #   2. the same-name rule -- an identical handle on both sites is the same
    #      person, unless it was split by hand. A split is an instruction, and a
    #      rule that silently undoes one is worse than having no rule.
    import profile_db as _pdb
    db = _pdb._shared_db()
    splits = db.profile_splits()
    by_key = {(p["username"].lower(), p["platform"]): p for p in out}

    pairs = []
    for (plat, alias), (parent, parent_plat, _auto) in sorted(db.profile_links().items()):
        pairs.append(((alias, plat), (parent.lower(), parent_plat)))
    for handle, plat in list(by_key):
        # Only from the Instagram side, so each pair is made once: walking both
        # would fold each card into the other and count everything twice.
        if plat != "instagram" or handle in splits:
            continue
        if (handle, "tiktok") not in by_key:
            continue
        mine, theirs = by_key[(handle, "instagram")], by_key[(handle, "tiktok")]
        # The better-populated archive keeps the card, so opening it lands on the
        # fuller view. A tie goes to Instagram, which is where stories and tagged
        # posts live.
        if (theirs.get("count", 0), True) > (mine.get("count", 0), False):
            pairs.append(((handle, "instagram"), (handle, "tiktok")))
        else:
            pairs.append(((handle, "tiktok"), (handle, "instagram")))

    folded = set()
    for child_key, parent_key in pairs:
        if child_key in folded or child_key == parent_key:
            continue
        child, prim = by_key.get(child_key), by_key.get(parent_key)
        if child is None or child is prim:
            continue
        if prim is None:
            # Nothing on disk for the parent, so this card stands for both but
            # there are no numbers to add to it. Register it under the parent's
            # key so a later pair folds into the same card instead of making a
            # second one, and record the other name so it is not silently lost.
            by_key[parent_key] = child
            child.setdefault("merged", []).append(parent_key[0])
            continue
        folded.add(child_key)
        prim["count"] = prim.get("count", 0) + child.get("count", 0)
        prim["posts"] = prim.get("posts", 0) + child.get("posts", 0)
        prim.setdefault("merged", []).append(child["username"])
        if child["platform"] != prim["platform"]:
            prim.setdefault("merged_platforms", []).append(child["platform"])
    if folded:
        out = [p for p in out
               if (p["username"].lower(), p["platform"]) not in folded]

    out.sort(key=lambda p: p["username"].lower())
    return out


def post_files(self, username: str, platform: str, section: str,
               first_file: str) -> list[str]:
    """Every file of the post whose first item is `first_file`.

    A tile stands for a whole post, so acting on one file of a carousel would
    leave the rest behind. Resolved on the server because the grouping lives here,
    and because the page only knows the tile's first file.
    """
    if not first_file:
        return []
    try:
        for p in self.gallery_posts(username, section, platform):
            items = p.get("items") or []
            if items and items[0].get("file") == first_file:
                return [i.get("file") for i in items if i.get("file")]
    except Exception:
        pass
    return [first_file]


def starred_posts(self) -> list[dict]:
    """Every starred clip, shaped like a gallery post so it can be shown as tiles.

    Starring happens in the viewer, so these are clips rather than profile posts:
    one item each, carrying the profile and platform it came from because those
    are what the media and thumbnail URLs are built from. The path is not used for
    that -- the two platforms store it differently (TikTok's carries a leading
    `tiktok/`, Instagram's does not) -- so only the file name is taken from it.
    """
    import profile_db as _pdb
    video_exts = {".mp4", ".mov", ".webm", ".m4v", ".mkv"}
    out = []
    for row in _pdb._shared_db().starred_clips():
        rel = str(row.get("path") or "")
        name = rel.rsplit("/", 1)[-1]
        who = str(row.get("profile") or "")
        if not name or not who:
            continue
        ext = os.path.splitext(name)[1].lower()
        out.append({
            "post": os.path.splitext(name)[0],
            "username": who,
            "platform": str(row.get("platform") or "instagram"),
            # Starred order, falling back to when the clip was seen: anything
            # starred before the column existed has no moment of its own.
            "ts": float(row.get("starred_at") or row.get("first_seen") or 0),
            # Where the FILE lives. A star does not move anything, so this is
            # always the profile's main folder.
            "section": "main",
            "items": [{"file": name, "part": 1,
                       "kind": "video" if ext in video_exts else "image"}],
        })
    return out


def gallery_all(self, section: str) -> list[dict]:
    """Every profile's posts for one section, across the whole archive.

    Backs the overarching Favourites and Trash views. Each post gains a
    `username` so the UI can label and link it back to its profile.
    """
    if section not in SUBDIRS:
        return []
    with self.lock:
        done = {j.username for j in self.jobs.values() if j.status == "done"}
    out = []
    for username in sorted(done):
        for p in self.gallery_posts(username, section):
            p["section"] = section
            p["username"] = username
            out.append(p)
    out.sort(key=lambda p: p["ts"])
    return out


def clear_trash(self) -> tuple[int, list[str]]:
    """Delete every trashed file across all profiles. Returns (n, errors).

    Only files inside a profile's own trash/ subfolder are touched, symlinks
    are skipped rather than followed, and each removed media file takes its
    sidecar siblings (json/txt) with it.

    Each deleted media file leaves a 0-byte placeholder of the same name in
    the profile's main folder. instaloader's fast-update decides a post is
    already downloaded purely by `os.path.isfile` on the expected filename, so
    the placeholder stops a later run from re-downloading what you deleted.
    The placeholder is recorded in self.tombstones and hidden from the gallery
    (which already skips 0-byte files).
    """
    removed = 0
    errors: list[str] = []
    with self.lock:
        # Platform per job: the same handle can be archived on both, in different
        # folders, so resolving without it looked only under Instagram and a
        # TikTok profile's trash was never cleared at all.
        targets = [(j.username, getattr(j, "platform", "instagram") or "instagram")
                   for j in self.jobs.values()]
    for username, platform in targets:
        root = self.root_for(username, platform)
        if root is None:
            continue
        trash = root / TRASH_DIR
        if not trash.is_dir():
            continue
        for name in _gallery_media(trash):
            target = trash / name
            if target.is_symlink():
                errors.append(f"{username}/{name}: symlink skipped")
                continue
            stem = name.rsplit(".", 1)[0]
            try:
                # Leave the tombstone FIRST: if anything below fails, the
                # placeholder already exists so the post is not re-fetched.
                placeholder = root / name
                placeholder.parent.mkdir(parents=True, exist_ok=True)
                if not placeholder.exists():
                    placeholder.write_bytes(b"")
                # Emptied contents do not change the directory's metadata, so
                # the cached listing would keep showing the trashed item. Drop
                # it now, at the one moment we know it changed.
                _media_cache_forget(root)
                with self.lock:
                    self.tombstones.append({
                        "username": username,
                        "name": name,
                        "at": time.time(),
                        "platform": _platform_of_folder(root),
                    })
                    if len(self.tombstones) > 5000:
                        del self.tombstones[:-5000]
                # Send to the desktop trash bin rather than unlinking outright,
                # so there is one more chance to recover a mistake. Falls back
                # to a hard delete if no trash tool is available.
                _to_desktop_trash(target)
                for sib in trash.glob(f"{stem}.*"):
                    if sib.is_file() and not sib.is_symlink():
                        _to_desktop_trash(sib)
                removed += 1
            except OSError as e:
                errors.append(f"{username}/{name}: {e}")
        # Drop the folder when it is empty.
        try:
            if not any(trash.iterdir()):
                trash.rmdir()
        except OSError:
            pass
    if removed:
        self.save_state()
    return removed, errors


def list_tombstones(self) -> list[dict]:
    """Every permanently-deleted post, newest first.

    Each entry is {username, name, at}. `name` is the placeholder filename in
    the profile's main folder; a 0-byte file of that name is what stops
    instaloader re-downloading the post.
    """
    with self.lock:
        return list(reversed(self.tombstones))[-200:]


def remove_tombstone(self, username: str, name: str,
                     platform: str = "instagram") -> tuple[bool, str]:
    """Undo a permanent delete: drop the placeholder so the post is fetched again.

    Removes the 0-byte placeholder from the profile's main folder and forgets
    the manifest entry. The media itself is already gone; the next run of that
    profile will re-download it.

    `platform` decides which folder the placeholder is looked for in. Without it
    a TikTok tombstone resolved to the Instagram folder, found no placeholder,
    and still reported success -- so the post was never actually re-downloaded.
    """
    username = clean_profile_name(username) or ""
    if not username:
        return False, "bad profile"
    if name != Path(name).name or Path(name).suffix.lower() not in GALLERY_EXTS:
        return False, "bad filename"
    with self.lock:
        before = len(self.tombstones)
        # Entries recorded before the platform was stored have none, so a missing
        # one matches any platform; a recorded one has to agree.
        self.tombstones = [
            t for t in self.tombstones
            if not (t.get("username") == username
                    and t.get("name") == name
                    and t.get("platform", platform) == platform)
        ]
        found = len(self.tombstones) != before
    if not found:
        return False, "no such tombstone"
    root = self.root_for(username, platform)
    try:
        root_real = root.resolve(strict=True) if root is not None else None
    except OSError:
        root_real = None
    if root_real is None:
        self.save_state()
        return True, "tombstone cleared (profile folder missing)"
    placeholder = root_real / name
    # Only ever remove a 0-byte file inside the profile folder itself.
    try:
        if placeholder.is_file() and placeholder.stat().st_size == 0:
            placeholder.unlink()
    except OSError as e:
        return False, f"could not remove placeholder: {e}"
    # Drop the name from the remembered index too.
    #
    # Without this the clear undoes itself: reconcile_deletions compares that
    # index against the folder, sees a file it remembers and cannot find, and
    # concludes someone deleted it outside the app -- so it writes the placeholder
    # straight back and re-records the tombstone. The very next gallery load did
    # it, which made "post will re-download" untrue in exactly the case the
    # feature exists for. Removing the entry first is what makes the clear hold.
    try:
        index = _read_index(root_real)
        media = [m for m in (index.get("media") or []) if m != name]
        if media != (index.get("media") or []):
            _write_index(root_real, {"media": media})
    except OSError:
        pass
    self.save_state()
    return True, "tombstone cleared; post will re-download"


def roots_status(self) -> list[dict]:
    """Every configured archive folder, and whether it is there right now.

    Drives get unplugged. Reporting `present` is all the UI needs: an absent
    root contributes no profiles and is not an error.
    """
    out = []
    for i, p in enumerate(self.roots):
        try:
            present = p.is_dir()
        except OSError:
            present = False
        out.append({
            "path": str(p),
            "present": present,
            "receives": _same(p, self.output_root),
            "index": i,
            # Space is reported per root, because each root can be a different
            # drive: one filling up must not stop downloads to another.
            "disk": disk_usage(p) if present else None,
        })
    return out


def disk_worst(self) -> dict | None:
    """The fullest reachable root, so one summary can drive the warning."""
    order = {"ok": 0, "warn": 1, "stop": 2}
    disks = [d for d in (r["disk"] for r in self.roots_status()) if d]
    if not disks:
        return None
    return max(disks, key=lambda d: (order[d["level"]], d["used_pct"]))


def disk_blocks(self, root=None) -> str:
    """Why a download must not start for this folder, or '' when it may.

    Called before a job starts, which is the only moment the app can decline to
    make a drive fuller. `root` is the job's own drive when it is pinned to one;
    otherwise the downloads folder.
    """
    path = root or self.output_root
    u = disk_usage(path)
    if u is None:
        # Not reachable. That is a mounting problem with its own handling, and
        # it must not be reported as a full disk.
        return ""
    if u["level"] != "stop":
        return ""
    return (f"{u['free_gb']} GB free on {u['path']} ({u['used_pct']}% used) — "
            f"at the {DISK_STOP_PCT:.0f}% limit of {u['total_gb']} GB")


def add_root(self, path: str) -> tuple[bool, str]:
    """Add an archive folder to read from (does not change new downloads).

    The folder must already exist: adding a path that is not there would be a
    typo trap, and an unplugged drive is something you re-add when it is back,
    not something to guess at. Nothing is ever moved or copied -- a profile is
    found in whichever root actually holds it.
    """
    p = (path or "").strip()
    if not p:
        return False, "no folder given"
    target = Path(p).expanduser()
    if not target.is_absolute():
        return False, "please give a full path (starting with /)"
    try:
        real = target.resolve(strict=True)
    except OSError:
        return False, f"{target} is not there right now (is the drive mounted?)"
    if not real.is_dir():
        return False, "that is a file, not a folder"
    if any(_same(r, real) for r in self.roots):
        return False, "that folder is already in the list"
    with self.lock:
        self.roots.append(real)
    self.save_state()
    return True, f"added {real}"


def remove_root(self, path: str) -> tuple[bool, str]:
    """Stop reading from an archive folder. Files are never touched."""
    p = (path or "").strip()
    if not p:
        return False, "no folder given"
    target = Path(p).expanduser()
    if _same(target, self.output_root):
        return False, ("that is where new downloads go -- change the downloads "
                       "folder first, then remove this one")
    before = len(self.roots)
    with self.lock:
        self.roots = [r for r in self.roots if not _same(r, target)]
    if len(self.roots) == before:
        return False, "that folder is not in the list"
    self.save_state()
    return True, f"stopped reading {target}"


def set_output_root(self, path: str, migrate: bool = False) -> tuple[bool, str]:
    """Change where downloads are stored. Persists across restarts.

    With migrate=False, future downloads land in the new folder; existing
    archives stay put (point back and they are seen again).

    With migrate=True, every existing profile folder is moved across first, so
    the archive stays in one place. Moving is safe to skip: instaloader's
    --latest-stamps lives outside the media folder, so it still knows what was
    already downloaded even if you move or delete the whole tree.

    Refuses while a download is running (a move mid-write is a bad idea).
    """
    p = (path or "").strip()
    if not p:
        return False, "no folder given"
    target = Path(p).expanduser()
    if not target.is_absolute():
        return False, "please give a full path (starting with /)"
    with self.lock:
        old = self.output_root
        busy = any(j.status in ("running", "retrying") for j in self.jobs.values())
    if busy:
        return False, "a download is running -- stop it first, then change the folder"
    if target == old:
        return False, "that is already the downloads folder"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"could not use that folder: {e}"
    if not os.access(target, os.W_OK):
        return False, "that folder is not writable"

    moved = 0
    if migrate:
        try:
            old_real = old.resolve(strict=True)
            target_real = target.resolve(strict=True)
        except OSError:
            old_real = target_real = None
        if old_real and target_real and old_real != target_real:
            for entry in list(old.iterdir()):
                if not entry.is_dir():
                    continue
                dest = target / entry.name
                if dest.exists():
                    # Never clobber; note it and move on.
                    return False, f"{entry.name} already exists in the new folder"
                try:
                    shutil.move(str(entry), str(dest))
                    moved += 1
                except OSError as e:
                    with self.lock:
                        self.output_root = target
                    self.save_state()
                    return False, (f"moved {moved}, then failed on {entry.name}: {e}. "
                                   "Downloads now point at the new folder.")

    with self.lock:
        self.output_root = target
        # A downloads folder is always a root, and always first.
        self.roots = [r for r in self.roots if not _same(r, target)]
        # Store the resolved path when we can, so state.json holds one stable
        # spelling of each root instead of a symlinked alias.
        self.roots.insert(0, _resolve(target) or target)
    self.save_state()
    if migrate and moved:
        return True, f"moved {moved} profile folder(s); downloads now go to {target}"
    return True, f"downloads now go to {target}"


THUMB_MAX = 320          # px on the long edge; the grid shows them ~190px
THUMB_VIDEO_AT = 1.0     # seconds into a clip for its poster frame
THUMB_CACHE = Path(os.environ.get(
    "INSTA_THUMBS", cache_dir() / "thumbs"))

# Profile pictures fetched from the site, which the archive does not carry.
# Kept in the cache rather than in the profile folder: everything in a profile
# folder is scanned as media, so a picture put there appears as an extra post.
AVATAR_CACHE = Path(os.environ.get("INSTA_AVATARS", cache_dir() / "avatars"))

# Hover-preview clips: a few silent seconds of the video, small enough to fetch
# on hover. A 4 MB source becomes ~180 KB, so sweeping the cursor over a row of
# video tiles does not pull tens of megabytes.
PREVIEW_MAX = 360        # px on the long edge
PREVIEW_SECONDS = 2
PREVIEW_CRF = 32


def _cache_key(src: Path) -> str:
    """Name + size + mtime, so a re-download invalidates the cached artefact."""
    st = src.stat()
    return f"{src.name}.{st.st_size}.{int(st.st_mtime)}"


def _thumb_path(kind: str, name: str) -> Path:
    return THUMB_CACHE / kind / (name + ".jpg")


def _preview_path(name: str) -> Path:
    return THUMB_CACHE / "prev" / (name + ".mp4")


_heif_ready = False


def ensure_pillow_formats() -> None:
    """Teach PIL the formats a phone writes before anything tries to open one.

    HEIC is not built into Pillow; it comes from pillow-heif. That package is
    optional, so its absence is not fatal -- JPEG and PNG keep working and only
    HEIC files fail to thumbnail, which is better than refusing to start.
    """
    global _heif_ready
    if _heif_ready:
        return
    _heif_ready = True
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass


def make_thumb(src: Path, ext: str) -> Path | None:
    """Create (or reuse) a small JPEG for `src`. Returns None on failure.

    Images are shrunk with PIL; videos get a single frame via ffmpeg. This is
    what makes a 2958-item grid usable -- originals average ~1.9 MB, so a
    screenful would otherwise pull tens of megabytes per scroll.
    """
    dest = _thumb_path("v" if ext in GALLERY_VIDEO_EXTS else "i",
                       _cache_key(src))
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part.jpg")
    try:
        if ext in GALLERY_VIDEO_EXTS:
            import subprocess
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-ss", str(THUMB_VIDEO_AT),
                 "-i", str(src), "-frames:v", "1", "-vf",
                 f"scale='min({THUMB_MAX},iw)':-2", str(tmp)],
                check=True, timeout=45,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            ensure_pillow_formats()
            from PIL import Image
            with Image.open(src) as im:
                im = im.convert("RGB")
                im.thumbnail((THUMB_MAX, THUMB_MAX))
                im.save(tmp, "JPEG", quality=82, optimize=True)
        if tmp.is_file() and tmp.stat().st_size > 0:
            tmp.replace(dest)
            return dest
    except Exception as e:
        print(f"thumb failed for {src.name}: {e}")
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return None


def make_preview(src: Path) -> Path | None:
    """Short silent clip for hover playback. Returns None on failure.

    Only for videos. The real file is far too heavy to start playing on a
    cursor pass; a truncated, downscaled copy starts instantly.
    """
    dest = _preview_path(_cache_key(src))
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part.mp4")
    try:
        import subprocess
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y",
             "-ss", str(THUMB_VIDEO_AT), "-t", str(PREVIEW_SECONDS),
             "-i", str(src),
             "-vf", f"scale='min({PREVIEW_MAX},iw)':-2",
             "-an", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", str(PREVIEW_CRF), "-movflags", "+faststart",
             "-pix_fmt", "yuv420p", str(tmp)],
            check=True, timeout=60,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if tmp.is_file() and tmp.stat().st_size > 0:
            tmp.replace(dest)
            return dest
    except Exception as e:
        print(f"preview failed for {src.name}: {e}")
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return None


def resolve_media(self, username: str, rel: str, section: str = "",
                  platform: str = "instagram") -> Path | None:
    """Map a requested gallery filename to a real file, or None.

    The file may live in the profile's own folder or in favorites/ or trash/,
    and must stay inside that profile.

    This used to require a job in `done` status before serving anything. That
    meant a profile still queued (or re-queued, or paused) had its files on disk
    but every tile 404'd, so whole galleries came up grey -- 32 profiles were in
    that state, with thousands of posts already on disk. The
    gallery now lists any profile with files on disk, so the gate has to follow
    suit: what is actually servable is decided by the file existing, which the
    path checks below already enforce.
    """
    folder = self.root_for(username, platform)
    if folder is None:
        return None
    # `rel` is a bare filename, not a path.
    if rel != Path(rel).name or Path(rel).suffix.lower() not in GALLERY_EXTS:
        return None
    # Only these three locations are servable.
    if section in SUBDIRS:
        target = folder / section / rel
    else:
        target = folder / rel
    try:
        folder_real = folder.resolve(strict=True)
        target_real = target.resolve(strict=True)
    except OSError:
        return None
    if target_real.parent not in (folder_real,
                                  folder_real / FAVORITES_DIR,
                                  folder_real / TRASH_DIR):
        return None
    return target_real


def move_media(self, username: str, name: str, section: str,
               platform: str = "instagram") -> tuple[bool, str]:
    """Move one file between a profile's main folder, favorites/ and trash/.

    `section` is "main" (un-favourite / restore), "favorites", or "trash".
    Never deletes: trashing is a move into trash/, and restoring moves back.
    Returns (ok, message).

    `platform` matters: the same handle can be archived on both Instagram and
    TikTok, and the two live in different folders. Resolving without it sent
    every TikTok favourite/trash to the Instagram folder, so the button failed
    on every TikTok post.
    """
    if section not in ("main",) + SUBDIRS:
        return False, f"unknown section {section!r}"
    if not clean_profile_name(username):
        return False, "bad profile"
    if name != Path(name).name or Path(name).suffix.lower() not in GALLERY_EXTS:
        return False, "bad filename"

    root = self.root_for(username, platform)
    if root is None:
        return False, "profile folder missing"
    try:
        # Resolving proves the folder is really there rather than a dangling
        # mount point; the result is only used as that check.
        root.resolve(strict=True)
    except OSError:
        return False, "profile folder missing"

    # Where the file is now (main, favorites/ or trash/) -> where it should go.
    dest_dir = root if section == "main" else root / section

    def real(p: Path) -> bool:
        """A file with contents, not a 0-byte deletion placeholder."""
        try:
            return p.is_file() and p.stat().st_size > 0
        except OSError:
            return False

    # Every copy of this file that has real bytes in it, whichever folder it is
    # in. A favourite leaves an empty placeholder in main/ -- that is what stops
    # instaloader re-downloading what you deleted -- and treating that placeholder
    # as the file made un-favouriting answer "already there" while the real copy
    # sat in favorites/. The heart flipped, nothing moved, and a refresh undid it.
    candidates = [root / name, root / FAVORITES_DIR / name, root / TRASH_DIR / name]
    copies = [p for p in candidates if real(p)]
    if not copies:
        if any(p.is_file() for p in candidates):
            return False, "only a deleted placeholder is here"
        return False, "file not found"

    in_dest = next((p for p in copies if p.parent == dest_dir), None)
    src = next((p for p in copies if p.parent != dest_dir), None)
    if src is None:
        return True, "already there"

    if in_dest is not None:
        # A real copy on both sides. The destination keeps its file and the other
        # is retired to the desktop trash -- but only when they are the same
        # bytes, because two different files sharing a name is a question for a
        # human, not something to resolve by guessing.
        try:
            same = filecmp.cmp(src, in_dest, shallow=False)
        except OSError:
            same = False
        if not same:
            return False, f"{dest_dir.name}/{name} already exists as a different file"
        try:
            _to_desktop_trash(src)
        except Exception as e:
            return False, f"could not retire the duplicate: {e}"
    else:
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return False, f"could not create {dest_dir.name}/: {e}"

        target = dest_dir / name
        if target.exists():
            # A placeholder is not media and must not block the real file coming
            # back to main/. Clearing it is part of the move; its tombstone entry
            # is dropped below so the post counts as wanted again.
            try:
                target.unlink()
            except OSError as e:
                return False, f"could not clear the placeholder in {dest_dir.name}/: {e}"
        try:
            src.rename(target)
        except OSError as e:
            return False, f"move failed: {e}"
    if dest_dir == root:
        # Back in main/, so it is no longer a deleted post.
        try:
            self.remove_tombstone(username, name, _platform_of_folder(root))
        except Exception:
            pass

    # Tell the deletion detector this file was MOVED, not deleted.
    #
    # reconcile_deletions compares main/ against .insta-index.json and writes a
    # tombstone plus a 0-byte placeholder for anything it thinks vanished. A
    # favourite is a rename OUT of main/, so without this it looked exactly like
    # a delete: every favourited post left a stub behind in main/ and got
    # recorded as a tombstone. Keeping the index in step is what tells the two
    # apart, so it is updated here, at the moment of the move.
    try:
        idx = _read_index(root)
        media = set(idx.get("media") or [])
        if dest_dir == root:
            media.add(name)          # back in main/
        else:
            media.discard(name)      # moved out of main/
        _write_index(root, {**idx, "media": sorted(media)})
    except Exception:
        pass
    return True, f"moved to {dest_dir.name}/"



# ---------- settings ----------

@dataclass
class Settings:
    """Global defaults, editable in the UI. Mirrors instaloader's real flags."""

    # what to download
    posts: bool = True
    profile_pic: bool = True
    stories: bool = True
    highlights: bool = True
    tagged: bool = False
    reels: bool = False
    igtv: bool = False
    videos: bool = True
    video_thumbnails: bool = False
    pictures: bool = True

    # metadata
    captions: bool = True
    metadata_json: bool = True
    compress_json: bool = False
    comments: bool = False
    geotags: bool = False

    # which posts
    fast_update: bool = True       # only new posts (default per user)
    latest_stamps: bool = True     # survive moving the media folder elsewhere
    count: int = 0                 # 0 = unlimited
    post_filter: str = ""
    storyitem_filter: str = ""

    # how
    sanitize_paths: bool = True
    resume: bool = True
    max_connection_attempts: int = 3
    request_timeout: int = 300
    iphone: bool = True            # --no-iphone flips this
    slide: str = ""

    # pacing (our scheduler, not instaloader)
    between_profiles: int = BETWEEN_PROFILES_SECONDS
    # TikTok gets its own gap. Sharing one timer would let a slow or failing
    # TikTok profile stall the Instagram queue (and the reverse), so the two
    # tools pace themselves independently -- but the same default, because the
    # reason for the gap (not looking automated) applies to both.
    # TikTok's gap is its own and much shorter than Instagram's. yt-dlp only
    # reads a public page per profile: there is no session, no request budget
    # shared with anything, and no 429s of its own, so the long Instagram pause
    # buys nothing here and turned 300 profiles into a day of waiting.
    between_tiktok: int = 15

    # yt-dlp options. Stored as a flat dict so the settings panel can treat them
    # like any other row, and validated on load by TikTokSettings itself.
    tiktok: dict = field(default_factory=dict)

    def __post_init__(self):
        # Fill the yt-dlp block with its defaults so the settings panel shows
        # real values on a fresh install instead of blank boxes. Existing
        # values always win, so this never overwrites a saved choice.
        try:
            import tiktok_engine as _tt
            base = _tt.TikTokSettings().to_dict()
            base.update(self.tiktok or {})
            self.tiktok = base
        except Exception:
            pass

    # Auto-check: when the queue empties, re-queue every profile already
    # archived so new posts are picked up without pressing anything. Off by
    # default -- it is a background fetch, so it should be an explicit choice,
    # not something that starts talking to Instagram the moment the queue runs
    # dry. hours=0 turns it off.
    autocheck: bool = False
    autocheck_hours: float = 6.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        """Apply a patch dict onto defaults. Unknown keys and bad types are ignored."""
        s = cls()
        for k, v in (d or {}).items():
            if not hasattr(s, k):
                continue
            cur = getattr(s, k)
            if isinstance(cur, bool):
                # Accept real bools and the strings "true"/"false".
                if isinstance(v, bool):
                    setattr(s, k, v)
                elif isinstance(v, str) and v.lower() in ("true", "false"):
                    setattr(s, k, v.lower() == "true")
            elif isinstance(cur, int) and not isinstance(cur, bool):
                if isinstance(v, bool):
                    continue
                try:
                    setattr(s, k, int(v))
                except (TypeError, ValueError):
                    pass
            elif isinstance(cur, str):
                if isinstance(v, str):
                    setattr(s, k, v)
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    setattr(s, k, str(v))
            elif isinstance(cur, dict):
                # Nested blocks (the yt-dlp options) merge rather than replace,
                # so saving one field cannot wipe the rest of the block.
                if isinstance(v, dict):
                    merged = dict(cur)
                    merged.update(v)
                    setattr(s, k, merged)
                elif hasattr(v, "items"):
                    merged = dict(cur)
                    merged.update(dict(v))
                    setattr(s, k, merged)
        return s


@dataclass
class Job:
    username: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "queued"  # queued running retrying done failed skipped cancelled
    kind: str = "main"  # "main" | "reels" | "tagged" — extra passes run as follow-up jobs
    platform: str = "instagram"  # "instagram" (instaloader) | "tiktok" (yt-dlp)
    tiktok_count: int = 0  # 0 = all; otherwise newest N videos
    private: bool = False  # TikTok profile yt-dlp reported as not visible
    tiktok_total: int = 0  # how many videos the profile has; 0 = not known yet
    parent_id: str | None = None  # for a reels job: the main job it follows
    root: str = ""  # archive root this job downloads into; "" = the current
                    # downloads folder. Pinned when the profile already exists
                    # on some root, so a re-run lands on its own drive instead
                    # of splitting one profile across two.
    attempts: int = 0
    max_attempts: int = 6
    added_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    message: str = ""
    posts: int = 0
    error: str = ""
    tail: list[str] = field(default_factory=list)
    total_files: int = 0
    total_bytes: int = 0
    # per-job overrides (None = use global settings)
    settings: dict | None = None
    last_run_seconds: float = 0.0
    retry_after: int = 0  # seconds Instagram asked us to wait
    done_count: int = 0   # posts processed so far, from "[ n/m ]"
    total_count: int = 0  # total posts, from "[ n/m ]" (0 = unknown)
    last_line: str = ""   # most recent output line, survives restarts
    heal_attempts: int = 0  # how many times a partial run was auto-refetched
    # Automatic retry bookkeeping (survives a restart, so a sweep scheduled
    # before a reboot still happens after it).
    auto_retries: int = 0   # how many sweeps have re-queued this job
    next_retry_at: float = 0.0  # when the sweep may pick it up again
    fatal: bool = False     # decided unsalvageable (missing/private); never swept
    _counted_at: float = 0.0  # throttle for the on-disk rescan

    def effective(self, base: Settings) -> Settings:
        if not self.settings:
            return base
        return Settings.from_dict({**base.to_dict(), **self.settings})

    def to_dict(self) -> dict:
        return asdict(self)


def has_profile_pic(folder: Path) -> bool:
    """Whether this profile's picture is already saved in its folder.

    instaloader asks for the picture on every run -- --fast-update does not
    cover it -- and the high-quality copy costs a separate API round trip that
    Instagram throttles like any other. The call is worth making once: the
    picture is what the gallery draws. After that it buys nothing, and a failed
    one ends the run with "Errors or warnings occurred" over a picture that is
    already on disk.
    """
    try:
        return any(folder.glob("*profile_pic.*"))
    except OSError:
        return False


class Engine:
    """Single background worker with a mutable, reorderable queue."""

    def __init__(self, output_root: Path = OUTPUT_ROOT):
        self.output_root = output_root
        # Every folder an archive may live in, in priority order. output_root is
        # always in here and is where NEW downloads go; the rest are read-only
        # extra places (another disk, an external drive). Roots that are not
        # mounted right now simply contribute nothing until they come back.
        self.roots: list[Path] = [output_root]
        self.q: "queue.Queue[Job]" = queue.Queue()
        # Ordered list of job ids waiting to run (source of truth for order/priority).
        self.pending: list[str] = []
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.worker: threading.Thread | None = None
        # One worker per platform. Instagram goes through instaloader and TikTok
        # through yt-dlp: different programs talking to different servers with
        # separate rate limits, so neither should ever wait behind the other's
        # queue. Everything that used to be a single slot is keyed by platform
        # now -- the running profile, the cancel flag, the child process and the
        # pacing deadline.
        self.worker_tt: threading.Thread | None = None
        self.current: dict[str, str | None] = {"instagram": None, "tiktok": None}
        self.cancel_current: dict[str, bool] = {"instagram": False, "tiktok": False}
        # A pause that has to stop a profile mid-download. `cancel_current` makes
        # the worker abandon the run; this flag is what tells it to put the job
        # back in the queue instead of marking it cancelled. Transient: a pause
        # is not persisted (see `pause`).
        self.pause_current: dict[str, bool] = {"instagram": False, "tiktok": False}
        self.paused = False          # Instagram: user pause, or a 429
        self.tt_paused = False       # TikTok: user pause only
        # Set while a platform is refusing to start work because a drive is
        # nearly full. Per platform, because the two queues can live on
        # different drives and only one of them may be out of room.
        self.disk_blocked: dict[str, str] = {"instagram": "", "tiktok": ""}
        self.stop_requested = False
        self.settings = Settings()
        self.active_session: str | None = None  # chosen instaloader session
        # Sessions the user has locked out (a flagged account cooling off).
        # Persisted, so a restart cannot quietly put that account back to work;
        # only an explicit unlock does.
        self.locked_sessions: list[str] = []
        self._proc: dict[str, subprocess.Popen | None] = {"instagram": None,
                                                          "tiktok": None}
        # Wall-clock time before which NO request may be made. Persisted, so
        # restarting the app cannot bypass a rate-limit cooldown.
        self.cooldown_until: float = 0.0
        self.consecutive_limits: int = 0
        # True when the current pause was caused by a rate limit (auto-clears
        # when the cooldown expires) rather than by the user (stays paused).
        # Jobs the user removed, kept so they can be restored.
        self.removed: list[dict] = []
        # Posts permanently deleted from trash, kept as a list of
        # {username, name} so we can report them and un-tombstone on demand.
        # A 0-byte placeholder with the original name is left in the profile's
        # main folder so instaloader's fast-update (which only checks whether a
        # file exists) skips the post instead of re-downloading it.
        self.tombstones: list[dict] = []
        # Timestamp of the last automatic check for new posts (0 = never).
        self._last_autocheck: float = 0.0
        self._autocheck_note: str = ""
        self._limit_paused: bool = False
        # While a worker sleeps out its gap, these hold that platform's wait
        # start and deadline (epoch seconds, 0 when not waiting). Keyed by
        # platform so an Instagram gap and a TikTok gap can run at once; the UI
        # counts down from wait_until so the number it shows is the real one.
        self.wait_started: dict[str, float] = {"instagram": 0.0, "tiktok": 0.0}
        self._skip_gap: dict[str, bool] = {"instagram": False, "tiktok": False}
        self.wait_until: dict[str, float] = {"instagram": 0.0, "tiktok": 0.0}
        # The length chosen for the gap in progress. Jitter is picked once, when
        # the gap starts: recomputing it per poll would make the countdown's total
        # jump around while the number counted down.
        self.wait_total: dict[str, float] = {"instagram": 0.0, "tiktok": 0.0}
        self._load_state()

    # ---------- gallery ----------

    gallery_profiles = gallery_profiles
    gallery_sections = gallery_sections
    starred_posts = starred_posts
    post_files = post_files
    tiktok_avatar = tiktok_avatar
    gallery_posts = gallery_posts
    # These two are what make a merged card show more than one account, and they
    # have to be bound here to be reachable as methods at all.
    profile_family = profile_family
    _real_profile_name = _real_profile_name
    timeline = timeline
    timeline_month = timeline_month
    gallery_all = gallery_all
    clear_trash = clear_trash
    list_tombstones = list_tombstones
    remove_tombstone = remove_tombstone
    reconcile_deletions = reconcile_deletions
    resolve_media = resolve_media
    move_media = move_media
    set_output_root = set_output_root
    root_for = root_for
    _existing_root = _existing_root
    roots_status = roots_status
    disk_worst = disk_worst
    disk_blocks = disk_blocks
    add_root = add_root
    remove_root = remove_root

    def job_log(self, job_id: str) -> dict:
        """One job's full log tail, for the UI to fetch when a log is opened.

        The poll no longer carries logs for finished jobs, so this is how a
        card's "log (N lines)" panel fills itself in.
        """
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                archived = next((r for r in self.removed if r.get("id") == job_id), None)
                if archived is None:
                    return {"ok": False, "error": "no such job"}
                tail = list(archived.get("tail") or [])
                return {"ok": True, "id": job_id, "tail": tail, "tail_len": len(tail)}
            tail = list(job.tail)
            return {"ok": True, "id": job_id, "tail": tail, "tail_len": len(tail)}

    # ---------- persistence ----------

    def _load_state(self) -> None:
        # Try the main state file, then fall back to backups. A corrupt file
        # (trailing garbage, partial write) must never wipe the queue silently:
        # raw_decode recovers the first complete object, and backups are a
        # last resort that at least keeps the archive reachable.
        candidates = [STATE_FILE,
                      STATE_FILE.with_suffix(".bak"),
                      STATE_FILE.with_name(STATE_FILE.name + ".bak2"),
                      STATE_FILE.with_name(STATE_FILE.name + ".before-gap-120")]
        d = None
        for cand in candidates:
            if not cand.is_file():
                continue
            try:
                text = cand.read_text()
                try:
                    d = json.loads(text)
                except json.JSONDecodeError:
                    try:
                        d, _end = json.JSONDecoder().raw_decode(text)
                    except Exception:
                        d = None
                if d is not None:
                    break
            except Exception:
                d = None
                continue
        if d is None:
            return
        try:
            self.settings = Settings.from_dict(d.get("settings", {}))
            self.active_session = d.get("active_session") or None
            self.locked_sessions = [str(u) for u in (d.get("locked_sessions") or []) if u]
            self.cooldown_until = float(d.get("cooldown_until") or 0.0)
            self.consecutive_limits = int(d.get("consecutive_limits") or 0)
            if d.get("limit_paused"):
                self._limit_paused = True
                self.paused = True
            # A pause the user pressed survives a restart. Read after the
            # rate-limit flag above, so a deliberate pause is never mistaken for
            # one that clears itself when the cooldown expires.
            up = d.get("user_paused")
            if isinstance(up, dict):
                if up.get("instagram"):
                    self.paused = True
                if up.get("tiktok"):
                    self.tt_paused = True
            self.removed = list(d.get("removed") or [])
            self.tombstones = list(d.get("tombstones") or [])
            try:
                self._last_autocheck = float(d.get("last_autocheck") or 0.0)
            except (TypeError, ValueError):
                self._last_autocheck = 0.0
            saved_roots = [Path(p) for p in (d.get("roots") or []) if p]
            if saved_roots:
                self.roots = saved_roots
            saved_root = d.get("output_root")
            if saved_root:
                self.output_root = Path(saved_root)
            self.roots = [r for r in self.roots if not _same(r, self.output_root)]
            self.roots.insert(0, self.output_root)
            # Restore queued/running jobs.
            for jd in d.get("jobs", []):
                if not jd.get("username"):
                    continue
                j = Job(**{k: v for k, v in jd.items() if k in Job.__dataclass_fields__})
                if j.status in ("running", "retrying"):
                    j.status = "queued"
                    j.message = "resumed after restart"
                    j.attempts = 0
                self.jobs[j.id] = j
            # Restore run order.
            for jid in (d.get("order") or []):
                if jid in self.jobs and jid not in self.pending:
                    self.pending.append(jid)
            for jid, j in self.jobs.items():
                if j.status == "queued" and jid not in self.pending:
                    self.pending.append(jid)
        except Exception:
            pass


    def save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with self.lock:
                jobs = [j.to_dict() for j in self.jobs.values()]
                data = {
                    "settings": self.settings.to_dict(),
                    "active_session": self.active_session,
                    "locked_sessions": list(self.locked_sessions),
                    "cooldown_until": self.cooldown_until,
                    "consecutive_limits": self.consecutive_limits,
                    "limit_paused": self._limit_paused,
                    # A pause the user pressed is remembered, so a restart (or a
                    # reboot, or a deploy) comes back PAUSED instead of quietly
                    # putting downloads back to work. Derived rather than stored
                    # separately: "paused and not a rate-limit pause" IS a user
                    # pause, which is the same distinction `wait_reason` reports.
                    "user_paused": {
                        "instagram": bool(self.paused and not self._limit_paused),
                        "tiktok": bool(self.tt_paused),
                    },
                    # When the automatic check last ran. Persisted so a restart
                    # does not immediately look like "never checked" and fire
                    # another sweep against Instagram.
                    "last_autocheck": getattr(self, "_last_autocheck", 0.0),
                    # Persist the run order explicitly: rebuilding it from a dict
                    # on restart would scramble the queue.
                    "order": list(self.pending),
                    "removed": self.removed,
                    "tombstones": self.tombstones,
                    "output_root": str(self.output_root),
                    "roots": [str(p) for p in self.roots],
                    "jobs": jobs,
                }
            # Write to a uniquely named temp file so two concurrent saves can
            # never interleave on the same path, fsync it, validate that it
            # parses, then atomically replace the real file. This is what keeps
            # a partial or interleaved write from ever corrupting the queue.
            import tempfile, os as _os
            fd, tmp_name = tempfile.mkstemp(prefix="state-", suffix=".tmp",
                                            dir=str(STATE_FILE.parent))
            try:
                with _os.fdopen(fd, "w") as fh:
                    json.dump(data, fh, indent=1)
                    fh.flush()
                    _os.fsync(fh.fileno())
                # Validate: a file that does not parse back is never installed.
                with open(tmp_name) as fh:
                    json.loads(fh.read())
                # Rotate: the previous good state becomes the backup, so a bad
                # write can always be recovered from by hand if needed.
                if STATE_FILE.is_file():
                    backup = STATE_FILE.with_suffix(".bak")
                    try:
                        # Keep one rolling backup. Overwrite is fine: the new
                        # backup is the state we are about to replace.
                        if backup.is_file():
                            backup.unlink()
                        STATE_FILE.rename(backup)
                    except OSError:
                        pass
                _os.replace(tmp_name, STATE_FILE)
            except Exception:
                try:
                    _os.unlink(tmp_name)
                except OSError:
                    pass
        except Exception:
            pass

    # ---------- public API ----------

    def start(self) -> None:
        """Make sure both platform workers are running.

        Two threads, one per platform. Instagram and TikTok downloads do not
        wait on each other, so a long Instagram queue no longer delays a single
        TikTok video.
        """
        with self.lock:
            self.stop_requested = False
            if not (self.worker and self.worker.is_alive()):
                self.worker = threading.Thread(target=self._run, args=("instagram",),
                                               daemon=True, name="worker-instagram")
                self.worker.start()
            if not (self.worker_tt and self.worker_tt.is_alive()):
                self.worker_tt = threading.Thread(target=self._run, args=("tiktok",),
                                                  daemon=True, name="worker-tiktok")
                self.worker_tt.start()

    def cleanup_partials_at_startup(self) -> None:
        """Remove interrupted-download leftovers once per start.

        Runs in a background thread: on a large archive the directory walk takes
        a moment, and the UI should not wait on housekeeping to appear. Any
        failure here is logged and ignored -- leaving stale .temp files is
        harmless, so cleanup must never be able to break startup.
        """
        def _work():
            try:
                res = self.sweep_partials(dry_run=False)
                if res["count"]:
                    print(f"cleaned {res['count']} interrupted download(s), "
                          f"{res['bytes'] // 1024} KB freed", flush=True)
                    self.save_state()
            except Exception as e:
                print(f"partial cleanup skipped: {e}", flush=True)

        threading.Thread(target=_work, daemon=True,
                         name="cleanup-partials").start()

    def kick_if_work(self) -> None:
        """Start the worker if there is queued work pending.

        Called once at boot: without it a restarted service sits idle with a
        full queue and never drains a cooldown, because start() is otherwise
        only reached from add()/requeue().
        """
        with self.lock:
            has_work = bool(self.pending)
        if has_work or self.cooldown_remaining() > 0:
            self.start()

    def watchdog(self) -> None:
        """Restart a worker that died while work remains.

        Each worker thread exits when it believes its platform is idle. If jobs
        for that platform are still queued at that moment, something went wrong
        -- revive it rather than leaving that queue stalled until the user
        notices. Checked per platform, because one worker going quiet says
        nothing about the other.
        """
        with self.lock:
            for platform, alive in (
                ("instagram", bool(self.worker and self.worker.is_alive())),
                ("tiktok", bool(self.worker_tt and self.worker_tt.is_alive())),
            ):
                if alive:
                    continue
                waiting = any(j.status == "queued"
                              and getattr(j, "platform", "instagram") == platform
                              for j in self.jobs.values())
                blocked = self.paused if platform == "instagram" else self.tt_paused
                if waiting and not blocked:
                    self.start()
                    return

    def update_settings(self, patch: dict) -> Settings:
        with self.lock:
            merged = {**self.settings.to_dict(), **(patch or {})}
            # The yt-dlp options arrive one field at a time, so a shallow merge
            # would replace the whole block and silently reset every other
            # TikTok setting. Merge that block key by key instead.
            if isinstance((patch or {}).get("tiktok"), dict):
                block = dict(self.settings.to_dict().get("tiktok") or {})
                block.update(patch["tiktok"])
                merged["tiktok"] = block
            self.settings = Settings.from_dict(merged)
            cur = self.settings
        self.save_state()
        return cur

    def add(self, raw_names: list[str], overrides: dict | None = None) -> tuple[list[Job], list[str]]:
        """Queue profiles. Refuses a second job of the same kind for one person.

        The old check only looked at queued/running/retrying jobs, so once a
        profile reached done (or failed, or was cancelled) it could be added
        again and silently got a second job for the same person -- shown as two
        duplicate rows. The archive folder is the same either way, so it was not
        data loss, but it was confusing and wasted requests re-walking that
        profile.

        Duplicates are judged per KIND: a main + tagged + reels set for one
        profile is correct and still allowed (they fetch different content),
        while a second main job is refused. Extra passes are queued through
        add_extra_pass(), which is untouched.
        """
        added, rejected = [], []
        # A profile the user removed or deleted stays out until they restore it,
        # even if they paste it again. Checked before the duplicate logic so the
        # message says "excluded" rather than "already in the list".
        blocked = self._excluded_set("instagram")
        with self.lock:
            active = {
                j.username for j in self.jobs.values()
                if j.status in ("queued", "running", "retrying")
            }
            # Every kind already present for a profile, in ANY status, so a
            # finished job cannot be duplicated. Cancelled jobs are excluded:
            # removing someone and adding them back is a deliberate re-add.
            #
            # Keyed by LOWERCASE name: Instagram usernames are case-insensitive,
            # so "SomeUser" and "someuser" are the same person and used to
            # become two separate jobs for one archive folder. The stored name
            # keeps its original spelling -- only the comparison folds case.
            seen_kinds: dict[str, set[str]] = {}
            spelling: dict[str, str] = {}
            for j in self.jobs.values():
                if j.status == "cancelled":
                    continue
                key = j.username.lower()
                seen_kinds.setdefault(key, set()).add(j.kind or "main")
                spelling.setdefault(key, j.username)
            for raw in raw_names:
                name = clean_profile_name(raw)
                if not name:
                    if raw.strip():
                        rejected.append(raw.strip())
                    continue
                lname = name.lower()
                if lname in blocked:
                    rejected.append(f"{name} (excluded — restore it in the Excluded list)")
                    continue
                if lname in spelling:
                    # Report the name as it is already listed, so the message
                    # matches the row you can see.
                    name = spelling[lname]
                    lname = name.lower()
                if name in active:
                    rejected.append(f"{name} (already queued)")
                    continue
                if "main" in seen_kinds.get(lname, set()):
                    rejected.append(f"{name} (already in the list)")
                    continue

                # A reels job is queued as its own separate pass, never mixed
                # into the main download for the same profile.
                effective = Job(username=name, settings=overrides or None).effective(self.settings)

                job = Job(username=name, kind="main", settings=overrides or None)
                # Pin to the drive the profile already lives on, so a re-run
                # (or "check for new") updates its existing archive instead of
                # starting a second copy on the downloads folder.
                root = self._existing_root(name)
                if root is not None:
                    job.root = str(root)
                self.jobs[job.id] = job
                self.pending.append(job.id)
                added.append(job)

                if effective.reels:
                    reels_job = Job(username=name, kind="reels", parent_id=job.id,
                                    settings={**(overrides or {}), "reels": True},
                                    root=job.root)
                    self.jobs[reels_job.id] = reels_job
                    self.pending.append(reels_job.id)
                    added.append(reels_job)

                active.add(name)
                seen_kinds.setdefault(lname, set()).add("main")
                spelling.setdefault(lname, name)
        self._kick()
        self.save_state()
        return added, rejected

    def add_tiktok(self, handles: list[str], count: int = 5,
                   start: bool = False,
                   excluded: set[str] | None = None) -> tuple[list[Job], list[str]]:
        """Queue TikTok profiles for yt-dlp.

        Deliberately does not reuse add(): that one applies Instagram settings
        (reels, tagged passes, instaloader flags) which mean nothing here.

        A profile already in the list is skipped rather than duplicated -- the
        way to fetch more of someone is to raise the count on the existing job,
        because yt-dlp's archive file means re-running only pulls new videos.
        `start=False` leaves them queued without kicking the worker, so the
        user can delete the ones they did not want.
        """
        added: list[Job] = []
        skipped: list[str] = []
        raised: list[str] = []
        count = max(0, int(count))
        blocked = {h.lower() for h in (excluded or ())}
        with self.lock:
            existing = {}
            for j in self.jobs.values():
                if j.platform == "tiktok" and j.status != "cancelled":
                    existing[j.username.lower()] = j
            for raw in handles:
                h = (raw or "").strip().lstrip('@')
                if not h:
                    continue
                # A profile the user removed or deleted stays out until they
                # restore it, even if it is still in their following list.
                # Checked before `existing` so it is reported as excluded
                # rather than silently vanishing.
                if h.lower() in blocked:
                    skipped.append(h)
                    continue
                if h.lower() in existing:
                    # Already queued or downloaded: raising the number is the
                    # way to fetch more of them. yt-dlp's archive file means a
                    # re-run only pulls what is not already on disk, so going
                    # 5 -> 10 fetches five more rather than ten again.
                    job = existing[h.lower()]
                    if count > job.tiktok_count:
                        job.tiktok_count = count
                        if job.status == "done":
                            # A finished job has to run again to fetch the rest.
                            job.status = "queued"
                            job.finished_at = None
                            job.error = ""
                            job.message = "queued for more videos"
                            if job.id not in self.pending:
                                self.pending.append(job.id)
                        raised.append(f"{job.username} ({count})")
                    else:
                        skipped.append(h)
                    continue
                job = Job(username=h, kind="main", platform="tiktok",
                          tiktok_count=count)
                root = self._existing_root(h, "tiktok")
                if root is not None:
                    job.root = str(root)
                self.jobs[job.id] = job
                self.pending.append(job.id)
                added.append(job)
                existing[h.lower()] = job
        if start:
            self._kick()
        self.save_state()
        self.last_raised = raised
        return added, skipped

    def remove(self, job_id: str) -> bool:
        """Remove a job. Running jobs are cancelled first.

        Removing a TikTok job also remembers the profile as excluded, so pasting
        the following list again does not put it straight back. That is the
        whole point: a deliberate removal should stick. It is only a memory --
        the Excluded list can restore it, and nothing is deleted from disk.
        """
        exclude_handle = ""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False
            exclude_platform = getattr(job, "platform", "instagram")
            exclude_handle = job.username
            if job.status == "running":
                # Cancel only this job's own worker process. Both platforms can
                # be downloading at once, and the other one is not involved.
                self.cancel_current[exclude_platform] = True
                self._terminate_proc(exclude_platform)
                job.status = "cancelled"
                job.finished_at = time.time()
                job.message = "cancelled"
                self.current[exclude_platform] = None
            elif job.status in ("queued", "retrying"):
                job.status = "cancelled"
                job.finished_at = time.time()
                job.message = "removed"
            if job_id in self.pending:
                self.pending.remove(job_id)
        self.save_state()
        if exclude_handle:
            self.exclude_profile(exclude_platform, exclude_handle, "removed_from_queue")
        return True

    def exclude_profile(self, platform: str, handle: str, reason: str) -> bool:
        """Remember a profile as excluded. Called on remove and delete.

        Lives on the engine rather than in the HTTP layer so every path that
        removes a profile records it -- including any future caller.

        Keyed by platform: the same handle on Instagram and TikTok is usually a
        different person, so excluding one must never block the other.
        """
        try:
            return _shared_db().exclude(platform, handle, reason)
        except Exception:
            return False

    def _excluded_set(self, platform: str) -> set[str]:
        """Handles excluded for one platform, as lowercase names."""
        try:
            return _shared_db().excluded(platform)
        except Exception:
            return set()

    def forget(self, job_id: str) -> bool:
        """Drop a finished job from the list, archiving it so it can come back.

        This is the button shown on finished jobs ("Remove from list"), so it is
        just as much a deliberate removal as remove() is -- it excludes the
        TikTok profile too, otherwise re-pasting would silently re-add someone
        the user had cleared out.
        """
        exclude_handle = ""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status in ("running", "queued", "retrying"):
                return False
            exclude_platform = getattr(job, "platform", "instagram")
            exclude_handle = job.username
            entry = job.to_dict()
            entry["removed_at"] = time.time()
            self.removed.append(entry)
            if len(self.removed) > 500:      # keep a bounded history
                del self.removed[:-500]
            self.jobs.pop(job_id, None)
            if job_id in self.pending:
                self.pending.remove(job_id)
        self.save_state()
        if exclude_handle:
            self.exclude_profile(exclude_platform, exclude_handle, "deleted_from_gallery")
        return True

    def restore(self, job_id: str) -> bool:
        """Bring back a removed job, at the end of the queue."""
        with self.lock:
            entry = next((r for r in self.removed if r.get("id") == job_id), None)
            if not entry:
                return False
            self.removed = [r for r in self.removed if r.get("id") != job_id]
            if job_id in self.jobs:
                return False  # already present
            entry = {k: v for k, v in entry.items()
                     if k in Job.__dataclass_fields__}
            job = Job(**entry)
            job.status = "queued"
            job.finished_at = None
            job.started_at = None
            job.attempts = 0
            job.message = "restored"
            self.jobs[job.id] = job
            if job.id not in self.pending:
                self.pending.append(job.id)
        self._kick()
        self.save_state()
        return True

    def restore_all(self) -> int:
        """Bring back every removed job, in the order they were removed."""
        n = 0
        with self.lock:
            ids = [r.get("id") for r in self.removed if r.get("id")]
        for jid in ids:
            if self.restore(jid):
                n += 1
        return n

    def sweep_partials(self, dry_run: bool = False) -> dict:
        """Delete leftovers from interrupted downloads.

        instaloader writes `<name>.temp` while fetching and renames it only on
        success, so a killed download leaves a `.temp` behind. Those are pure
        junk: the real filename does not exist, so fast-update fetches the post
        again on the next run -- meaning the leftover is not only wasted space,
        it is not even a safety net.

        Deliberately only `.temp` files. It does NOT touch 0-byte media files:
        those are the placeholders the trash flow leaves, and deleting them
        would make instaloader re-download everything you deleted on purpose.
        """
        roots = [self.output_root] + [r for r in self.roots
                                      if not _same(r, self.output_root)]
        found = []
        freed = 0
        errors = []
        for root in roots:
            try:
                if not root.is_dir():
                    continue
                profiles = [p for p in root.iterdir() if p.is_dir()]
            except OSError:
                continue
            for prof in profiles:
                try:
                    entries = list(prof.iterdir())
                except OSError:
                    continue
                for f in entries:
                    if not f.is_file():
                        continue
                    # .temp only. Never a tombstone placeholder.
                    if not f.name.lower().endswith(".temp"):
                        continue
                    try:
                        size = f.stat().st_size
                    except OSError:
                        continue
                    found.append({"profile": prof.name, "name": f.name,
                                  "bytes": size})
                    freed += size
                    if not dry_run:
                        try:
                            f.unlink()
                        except OSError as e:
                            errors.append(f"{prof.name}/{f.name}: {e}")
        if found and not dry_run:
            # The listing cache may still mention them; cheap to drop.
            for root in roots:
                _media_cache_forget(root)
        return {"count": len(found), "bytes": freed,
                "files": sorted(found, key=lambda r: -r["bytes"])[:50],
                "errors": errors, "dry_run": dry_run}

    def purge_removed(self) -> int:
        """Empty the removed-jobs archive."""
        with self.lock:
            n = len(self.removed)
            self.removed = []
        self.save_state()
        return n

    def requeue(self, job_id: str) -> bool:
        """Re-run a finished/failed/cancelled job, keeping --fast-update.

        There is deliberately no way to force a full re-fetch. Disabling
        fast-update made instaloader walk every post and fetch its metadata
        again, fetching nothing new (download_post never overwrites) while
        burning the request budget that gets the session throttled. Anything
        already on disk is skipped, so a re-run is always cheap.
        """
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status in ("running", "queued", "retrying"):
                return False
            job.status = "queued"
            job.error = ""
            job.message = "re-queued"
            job.attempts = 0
            job.finished_at = None
            job.started_at = None
            # A retry is you asking for this one next, so it goes to the FRONT
            # of the queue. It used to append to the tail, which put a failed
            # profile behind however many hundred jobs were already waiting --
            # you press retry and nothing visibly happens.
            self.pending[:] = [x for x in self.pending if x != job_id]
            self.pending.insert(0, job_id)
        self._kick()
        self.save_state()
        return True

    def move(self, job_id: str, delta: int) -> bool:
        """Move a queued job up (-1) or down (+1) among the runnable jobs.

        Reorders only positions that matter (queued jobs), so the numbers the
        UI shows always match the order the worker will actually run.
        """
        with self.lock:
            main = [jid for jid in self.pending
                    if self.jobs.get(jid) and self.jobs[jid].status == "queued"]
            if job_id not in main:
                return False
            i = main.index(job_id)
            j = i + delta
            if j < 0 or j >= len(main):
                return False
            main[i], main[j] = main[j], main[i]
            self._rewrite_pending(main)
        self.save_state()
        return True

    def move_to(self, job_id: str, index: int) -> bool:
        with self.lock:
            main = [jid for jid in self.pending
                    if self.jobs.get(jid) and self.jobs[jid].status == "queued"]
            if job_id not in main:
                return False
            main.remove(job_id)
            index = max(0, min(index, len(main)))
            main.insert(index, job_id)
            self._rewrite_pending(main)
        self.save_state()
        return True

    def _rewrite_pending(self, runnable: list[str]) -> None:
        """Place `runnable` in the given order, preserving everything else.

        Only the listed ids are repositioned; all other entries keep their
        relative order. Rebuilding from a set (as an earlier version did)
        scrambled the queue whenever a job was re-queued.

        Must be called with self.lock held.
        """
        target = list(runnable)
        rest = [jid for jid in self.pending if jid not in set(target)]
        self.pending[:] = target + rest

    def move_top(self, job_id: str) -> bool:
        return self.move_to(job_id, 0)

    def clear_finished(self) -> int:
        with self.lock:
            gone = [jid for jid, j in self.jobs.items()
                    if j.status in ("done", "failed", "cancelled", "skipped")]
            for jid in gone:
                self.jobs.pop(jid, None)
        self.save_state()
        return len(gone)

    def retry_all_failed(self) -> int:
        n = 0
        with self.lock:
            for jid, j in list(self.jobs.items()):
                if j.status == "failed":
                    j.status = "queued"
                    j.attempts = 0
                    j.error = ""
                    j.message = "re-queued"
                    j.finished_at = None
                    self.pending[:] = [x for x in self.pending if x != jid]
                    n += 1
            # Retried jobs go to the front, keeping the order they were
            # retried in so they run in the order they are listed.
            if n:
                retried = [jid for jid, j in self.jobs.items()
                           if j.status == "queued" and jid not in self.pending]
                self.pending[:] = retried + self.pending
        if n:
            self._kick()
        self.save_state()
        return n

    # ---------- automatic check for new posts ----------
    #
    # Runs inside the worker when the queue empties, rather than from a cron or
    # a systemd timer: one process already holds the queue, the session and the
    # rate-limit state, and a second process poking the same state file would
    # fight it. Re-queueing goes through add(), so an auto-checked profile
    # behaves exactly like one you typed in.

    def autocheck_due(self, now: float | None = None) -> bool:
        """True when an automatic check may run now."""
        now = time.time() if now is None else now
        if not getattr(self.settings, "autocheck", False):
            return False
        hours = float(getattr(self.settings, "autocheck_hours", 0) or 0)
        if hours <= 0:
            return False
        if self.paused or self._limit_paused:
            return False
        # Never pile on: only when there is genuinely nothing left to do.
        # Autocheck tops up Instagram profiles, so only Instagram's work counts
        # here -- a busy TikTok queue is no reason to skip it.
        if self._has_pending("instagram") or self.current["instagram"]:
            return False
        last = float(getattr(self, "_last_autocheck", 0) or 0)
        return now - last >= hours * 3600

    def autocheck(self, now: float | None = None) -> dict:
        """Queue a re-check of every archived profile. Returns what was added.

        Each profile is added with fast_update on, so only new posts are
        fetched and nothing already on disk is re-downloaded or overwritten.
        Profiles already waiting in the queue are skipped by add() itself.
        """
        now = time.time() if now is None else now
        names = []
        for prof in self.gallery_profiles():
            u = prof.get("username")
            if u:
                names.append(u)
        added, rejected = self.add(names)
        self._last_autocheck = now
        self._autocheck_note = (f"auto-check queued {len(added)} of {len(names)} "
                                f"profile{'s' if len(names) != 1 else ''}")
        if added:
            self.save_state()
        return {"queued": len(added), "considered": len(names),
                "rejected": len(rejected)}

    # ---------- automatic retry sweep ----------
    #
    # Instagram throttles in waves: a batch of profiles gets refused, then the
    # same profiles work fine an hour later. Without this, every refusal leaves
    # a red card that only a human can clear, so the queue slowly fills with
    # profiles that are perfectly fine. The sweep re-queues them on a widening
    # delay instead, and gives up after a few tries so a truly broken profile
    # does not retry forever.

    def sweep_retries(self, now: float | None = None) -> int:
        """Re-queue failed jobs whose backoff has elapsed. Returns how many."""
        now = time.time() if now is None else now
        n = 0
        with self.lock:
            for jid, j in self.jobs.items():
                if j.status != "failed":
                    continue
                # A profile that really is gone must not be swept back in: the
                # classification already decided that, and retrying it forever
                # would just churn.
                if j.fatal:
                    continue
                if j.auto_retries >= AUTO_RETRY_MAX:
                    continue
                if now < j.next_retry_at:
                    continue
                j.auto_retries += 1
                j.attempts = 0
                j.error = ""
                j.message = (f"auto-retry {j.auto_retries}/{AUTO_RETRY_MAX} "
                             f"after throttling")
                j.finished_at = None
                # Widening delay: 15min, 1h, 3h, 8h. Long enough for Instagram
                # to forget, short enough that a real block clears in a day.
                base_step = AUTO_RETRY_DELAYS[min(j.auto_retries, len(AUTO_RETRY_DELAYS)) - 1]
                # Spread both ways: this is scheduling, not a safety margin, and
                # jobs that failed together should not all come back together.
                step = jitter(base_step, RETRY_JITTER)
                j.next_retry_at = now + step
                j.status = "queued"
                self.pending[:] = [x for x in self.pending if x != jid]
                self.pending.append(jid)   # behind anything already waiting
                n += 1
        if n:
            self._kick()
            self.save_state()
        return n

    def retry_status(self) -> dict:
        """What the sweep is waiting on, for the UI to show honestly."""
        now = time.time()
        waiting = []
        for j in self.jobs.values():
            if j.status == "failed" and not j.fatal and j.auto_retries < AUTO_RETRY_MAX:
                waiting.append({
                    "username": j.username,
                    "tries": j.auto_retries,
                    "in_seconds": max(0, int(j.next_retry_at - now)),
                })
        waiting.sort(key=lambda r: r["in_seconds"])
        return {"enabled": AUTO_RETRY_MAX > 0, "max": AUTO_RETRY_MAX,
                "waiting": waiting}

    def pause(self, platform: str = "instagram") -> None:
        """Pause one platform's worker, stopping its profile mid-download.

        Instagram and TikTok pause separately. Stopping Instagram to sit out a
        block has no bearing on yt-dlp, so it must not stop it too. "all" pauses
        both, for callers that want the whole app quiet.

        The profile downloading right now is stopped rather than waited out:
        sitting out a block for another twenty minutes while a run finishes is
        not what "pause" means. The interrupted profile goes back to the front
        of its platform's queue and restarts on resume. Nothing is lost --
        instaloader writes media to `<name>.temp` and only renames it into place
        when the file is complete, and yt-dlp keeps `.part` files, so a killed
        run leaves no half-file for the next run to mistake for a finished one.

        Not persisted: a service restart resumes downloading (see `_load_state`).
        """
        platform = (platform or "instagram").strip().lower()
        if platform in ("instagram", "all"):
            self.paused = True
            self._limit_paused = False  # a user pause must never auto-clear
            self._interrupt_running("instagram")
        if platform in ("tiktok", "all"):
            self.tt_paused = True
            self._interrupt_running("tiktok")
        self.save_state()

    def resume(self, platform: str = "instagram") -> None:
        """Resume one platform. Instagram's resume also clears any rate-limit
        cooldown the user is explicitly choosing to override."""
        platform = (platform or "instagram").strip().lower()
        if platform in ("instagram", "all"):
            self.paused = False
            self._limit_paused = False
        if platform in ("tiktok", "all"):
            self.tt_paused = False
        # Save BEFORE kicking off: the paused state is persisted, so a resume
        # that never reached disk would come back paused after a restart.
        self.save_state()
        self.kick_if_work()

    def start_platform(self, platform: str) -> dict:
        """Start one platform's worker, unpausing just that platform.

        There is a worker per platform now, so nothing needs reordering: the
        TikTok worker picks up TikTok jobs wherever they sit in the shared
        order. Moving them to the front would rewrite the user's queue order for
        no benefit, so it no longer happens.
        """
        platform = (platform or "").strip().lower()
        if platform not in ("instagram", "tiktok"):
            return {"ok": False, "error": "unknown platform"}
        if platform == "tiktok":
            self.tt_paused = False
        else:
            self.paused = False
            self._limit_paused = False
        # Pressing Start means "go now", so cut short any routine gap this
        # platform is sitting out. Without this the button did nothing at all
        # for up to four minutes, which reads as the app being stuck.
        self.skip_pacing(platform)
        self.save_state()
        self.start()
        return {"ok": True, "platform": platform}

    def clear_cooldown(self) -> None:
        """Drop the rate-limit cooldown (user override)."""
        with self.lock:
            self.cooldown_until = 0.0
            self.consecutive_limits = 0
            self._limit_paused = False
        self.save_state()

    def cooldown_remaining(self) -> int:
        return max(0, int(self.cooldown_until - time.time()))

    def wait_reason(self, platform: str = "instagram") -> dict:
        """What (if anything) is holding one platform's worker up.

        Three distinct causes, deliberately kept apart so a countdown can say
        which one it is:
          paused       -- user action; no deadline, waits for a human
          rate_limit   -- Instagram told us to back off; deadline is real
          pacing       -- the normal between-profiles gap; deadline is real
        Returns {} when that platform's worker is free to run.

        Instagram's pause and cooldown are not reported for TikTok. The TikTok
        worker ignores both, so repeating them there would say a download is
        stalled when it is running.
        """
        now = time.time()
        ig = platform == "instagram"
        if ig and self.paused:
            # A 429 sets paused *and* cooldown_until. If this pause came from a
            # rate limit, report that with its real deadline -- otherwise this
            # branch shadows the cooldown check below and the UI shows a bare
            # "paused", which is indistinguishable from the user pausing it and
            # gives no countdown.
            if self._limit_paused:
                cd = max(0, int(self.cooldown_until - now))
                if cd > 0:
                    return {"kind": "rate_limit", "remaining": cd,
                            "total": cd, "until": self.cooldown_until,
                            "label": "rate limited"}
            return {"kind": "paused", "remaining": 0, "total": 0,
                    "until": 0, "label": "paused"}
        if ig:
            cd = int(self.cooldown_until - now)
            if cd > 0:
                return {"kind": "rate_limit", "remaining": cd,
                        "total": cd, "until": self.cooldown_until,
                        "label": "rate limited"}
        elif self.tt_paused:
            return {"kind": "paused", "remaining": 0, "total": 0,
                    "until": 0, "label": "paused"}
        wu = self.wait_until.get(platform, 0.0)
        if wu > now:
            if ig:
                # Named for what it actually is: the voluntary gap between
                # profiles that keeps Instagram from rate-limiting us.
                # Instaloader has no inter-profile delay of its own, so this
                # gap is ours.
                label = "pacing between profiles"
            else:
                label = "pacing between TikTok profiles"
            return {"kind": "pacing", "remaining": int(wu - now),
                    # The length chosen for this gap, not the raw setting, so the
                    # bar matches the countdown it is drawn against.
                    "total": int(self.wait_total.get(platform)
                                 or self._pacing_delay(platform)),
                    "until": wu, "label": label}
        return {}

    def check_ip_block(self) -> bool:
        """One-shot check whether Instagram is refusing this IP (429)."""
        blocked = probe_ip_block()
        if blocked:
            with self.lock:
                # Same rule as the rate-limit cooldown: only ever longer.
                self.cooldown_until = max(
                    self.cooldown_until,
                    time.time() + jitter(IP_BLOCK_COOLDOWN, COOLDOWN_JITTER,
                                         up_only=True))
                self.paused = True
            self.save_state()
        return blocked

    def set_priority_high(self, job_id: str) -> bool:
        return self.move_top(job_id)

    # ---------- snapshot ----------

    def session_username(self) -> str | None:
        d = session_diagnosis(self.active_session, self.locked_sessions)
        return d["username"] if d["valid"] else None

    def session_info(self) -> dict:
        return session_diagnosis(self.active_session, self.locked_sessions)

    def list_sessions(self) -> list[dict]:
        locked = {u.lower() for u in self.locked_sessions}
        return [{**s, "locked": s["username"].lower() in locked}
                for s in list_sessions()]

    def lock_session(self, username: str) -> bool:
        """Lock a session out of downloads until it is explicitly unlocked.

        Persisted, so restarting the app does not put a flagged account back to
        work. Locking the session in use drops it as the active one, so the
        engine falls back to another usable session rather than stalling.
        """
        safe = clean_profile_name(username)
        if not safe:
            return False
        with self.lock:
            if safe.lower() not in {u.lower() for u in self.locked_sessions}:
                self.locked_sessions.append(safe)
            if (self.active_session or "").lower() == safe.lower():
                self.active_session = None
        self.save_state()
        return True

    def unlock_session(self, username: str) -> bool:
        """Undo `lock_session`. The account is usable again from the next job."""
        safe = clean_profile_name(username)
        if not safe:
            return False
        with self.lock:
            self.locked_sessions = [u for u in self.locked_sessions
                                    if u.lower() != safe.lower()]
        self.save_state()
        return True

    def browser_sessions(self) -> list[dict]:
        """Instagram logins found in installed browsers (for the Grab panel)."""
        return find_browser_sessions()

    def tiktok_browsers(self) -> list[str]:
        return find_tiktok_browsers()

    def tiktok_cookie_file(self) -> Path:
        """Where the app keeps its own TikTok session."""
        return STATE_FILE.parent / "tiktok-cookies.txt"

    def tiktok_session_files(self) -> list[Path]:
        """Every TikTok session the app holds.

        The first one keeps the plain `tiktok-cookies.txt` name, so an install
        that already had one carries on working, and later ones are named
        `tiktok-cookies-<slug>.txt`. Instagram has always kept several sessions
        side by side; TikTok had a single slot, so signing in as anyone else
        silently destroyed the login you had.
        """
        try:
            files = [p for p in STATE_FILE.parent.glob("tiktok-cookies*.txt")
                     if p.is_file()]
        except OSError:
            return []
        # The plain name first, then name order, so the list does not reshuffle
        # itself as sessions come and go.
        files.sort(key=lambda p: (p.name != "tiktok-cookies.txt", p.name))
        return files

    def _tiktok_session_map(self, key: str) -> dict:
        """A {file name: something} map out of settings, or an empty one."""
        value = self.settings.tiktok.get(key)
        return dict(value) if isinstance(value, dict) else {}

    def _save_tiktok_session_map(self, key: str, value: dict) -> None:
        with self.lock:
            self.settings.tiktok[key] = value

    def tiktok_sessions(self) -> list[dict]:
        """The held TikTok sessions, one row each, with the one in use marked."""
        import tiktok_engine as _tt
        active = Path(str(self.settings.tiktok.get("cookies_file") or "")).name
        labels = self._tiktok_session_map("session_labels")
        sources = self._tiktok_session_map("session_sources")
        rows = []
        for f in self.tiktok_session_files():
            try:
                st = _tt.cookie_jar_state(str(f))
            except Exception as e:
                st = {"error": f"{type(e).__name__}: {e}"}
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            source = str(sources.get(f.name) or "")
            rows.append({
                "source_label": tiktok_source_label(source),
                # Ref is the file name: it is what the API takes back, and it
                # resolves only against files we already know about.
                "ref": f.name,
                "label": str(labels.get(f.name)
                             or tiktok_session_label(source) or f.name),
                "source": source,
                "active": f.name == active,
                "cookies": st.get("cookies", 0),
                "login": st.get("login", 0),
                "saved": st.get("saved", 0),
                "bytes": st.get("bytes", size),
                "error": st.get("error", ""),
            })
        return rows

    def _new_tiktok_session_path(self, spec: str) -> Path:
        """A file name for a new session, named after where its login came from."""
        slug = re.sub(r"[^a-z0-9]+", "-", browser_name_for_spec(spec).lower()).strip("-")
        if slug in ("", "browser"):
            slug = "session"
        out = STATE_FILE.parent / f"tiktok-cookies-{slug}.txt"
        n = 2
        while out.exists():
            out = STATE_FILE.parent / f"tiktok-cookies-{slug}-{n}.txt"
            n += 1
        return out

    def _tiktok_session_path(self, ref: str) -> Path | None:
        """Resolve a session ref to one of the files we actually hold.

        Matched against the known files rather than joined onto a directory, so
        a ref that arrives from the browser can never name something outside
        the session folder.
        """
        name = Path(str(ref or "")).name
        if not name:
            return None
        for f in self.tiktok_session_files():
            if f.name == name:
                return f
        return None

    def set_tiktok_session(self, ref: str) -> dict:
        """Switch which saved TikTok session downloads use.

        Takes effect from the next TikTok job: the path is read when the
        download command is built, which is how the Instagram switch works too.
        """
        path = self._tiktok_session_path(ref)
        if path is None:
            return {"ok": False, "error": "No such saved TikTok session."}
        with self.lock:
            self.settings.tiktok["cookies_file"] = str(path)
        self.save_state()
        return {"ok": True, "ref": path.name, **self.tiktok_session_state()}

    def rename_tiktok_session(self, ref: str, label: str) -> dict:
        """Give a saved session a name. Empty puts the default back."""
        path = self._tiktok_session_path(ref)
        if path is None:
            return {"ok": False, "error": "No such saved TikTok session."}
        label = " ".join(str(label or "").split())[:40]
        labels = self._tiktok_session_map("session_labels")
        if label:
            labels[path.name] = label
        else:
            labels.pop(path.name, None)
        self._save_tiktok_session_map("session_labels", labels)
        self.save_state()
        return {"ok": True, "sessions": self.tiktok_sessions()}

    def tiktok_session_state(self) -> dict:
        """Whether TikTok has a session of its own, and what is in it."""
        import tiktok_engine as _tt
        path = str(self.settings.tiktok.get("cookies_file") or "")
        st = _tt.cookie_jar_state(path)
        st["browser"] = str(self.settings.tiktok.get("cookies_from_browser") or "")
        # Say plainly which of the two the next download will use, because that
        # is the thing that was invisible before.
        st["using"] = "app" if st.get("held") else ("browser" if st["browser"] else "none")
        # Which of the saved sessions it is. The path alone is hidden inside the
        # config folder, so the panel names the file it is using.
        st["ref"] = Path(str(self.settings.tiktok.get("cookies_file") or "")).name
        labels = self._tiktok_session_map("session_labels")
        sources = self._tiktok_session_map("session_sources")
        st["label"] = str(labels.get(st["ref"])
                          or tiktok_session_label(str(sources.get(st["ref"]) or ""))
                          or st["ref"])
        # Where that login came from, in words, so the panel does not have to
        # show a profile directory path as if it were a name.
        st["browser_label"] = tiktok_source_label(str(st.get("browser") or ""))
        return st

    def tiktok_browser_sources(self) -> list[dict]:
        """Browsers with a TikTok login, each with a name to show for it."""
        return [{"spec": spec, "label": tiktok_source_label(spec)}
                for spec in self.tiktok_browsers()]

    def capture_tiktok_session(self, browser: str = "") -> dict:
        """Copy a browser's TikTok login into a session the app keeps itself.

        Instagram has had its own session files all along; TikTok only ever had a
        pointer at a browser profile, re-read on every run. That is not a session
        the app holds: it lasts as long as the browser stays signed in and its
        profile directory stays where it was, and nothing here could say whether
        it was there at all.
        """
        import tiktok_engine as _tt
        spec = (browser or "").strip()
        if not spec:
            spec = str(self.settings.tiktok.get("cookies_from_browser") or "").strip()
        if not spec or not _tt.cookies_db_for_spec(spec):
            # Pick a browser that actually has a login to copy, rather than the
            # configured one, which may be a browser we cannot read.
            found = ""
            for cand in self.tiktok_browsers():
                if _tt.cookies_db_for_spec(cand):
                    found = cand
                    break
            spec = found or spec
        if not spec:
            return {"ok": False,
                    "error": "No browser with a TikTok login was found. Sign in to "
                             "TikTok in Firefox or Zen, then try again."}
        # A second capture gets a file of its own rather than replacing the
        # first one: keeping a login per account and swapping between them is
        # the point. The first keeps the plain name, so an install that already
        # had a session is unchanged.
        out = self.tiktok_cookie_file()
        if out.is_file():
            out = self._new_tiktok_session_path(spec)
        result = _tt.capture_cookies(spec, out)
        if not result.get("ok"):
            return result
        labels = self._tiktok_session_map("session_labels")
        sources = self._tiktok_session_map("session_sources")
        labels.setdefault(out.name, tiktok_session_label(spec, set(labels.values())))
        sources[out.name] = spec
        with self.lock:
            # Kept as the fallback for browsers whose cookies cannot be copied.
            self.settings.tiktok["cookies_from_browser"] = spec
            self.settings.tiktok["cookies_file"] = str(out)
            self.settings.tiktok["session_labels"] = labels
            self.settings.tiktok["session_sources"] = sources
        self.save_state()
        return {"ok": True, "captured": result.get("cookies"),
                "login": result.get("login"), "ref": out.name,
                **self.tiktok_session_state()}

    def forget_tiktok_session(self, ref: str = "") -> dict:
        """Delete a saved session, going back to reading the browser.

        With no ref: the one downloads are using. Deleting a session that is not
        in use deliberately leaves the active one alone -- throwing away a login
        you are not using should not stop the queue.
        """
        targets: list[Path] = []
        if ref:
            found = self._tiktok_session_path(ref)
            if found is None:
                return {"ok": False, "error": "No such saved TikTok session.",
                        **self.tiktok_session_state()}
            targets = [found]
        else:
            active = str(self.settings.tiktok.get("cookies_file") or "")
            if active:
                targets = [p for p in [Path(active)] if p.is_file()]
            else:
                targets = self.tiktok_session_files()[:1]
        if not targets:
            return {"ok": True, **self.tiktok_session_state()}

        names = {p.name for p in targets}
        for p in targets:
            try:
                p.unlink()
            except OSError:
                pass
        labels = self._tiktok_session_map("session_labels")
        sources = self._tiktok_session_map("session_sources")
        for n in names:
            labels.pop(n, None)
            sources.pop(n, None)
        with self.lock:
            if Path(str(self.settings.tiktok.get("cookies_file") or "")).name in names:
                self.settings.tiktok["cookies_file"] = ""
            self.settings.tiktok["session_labels"] = labels
            self.settings.tiktok["session_sources"] = sources
        self.save_state()
        return {"ok": True, "forgotten": sorted(names),
                "sessions": self.tiktok_sessions(), **self.tiktok_session_state()}

    def set_tiktok_cookies_browser(self, browser: str) -> dict:
        """Set tiktok.cookies_from_browser. Empty string clears it.

        Validated before it is stored: yt-dlp exits 2 on a browser name it does
        not know, so an unchecked value here does not just fail to help -- it
        fails every TikTok download with an opaque "yt-dlp exited 2". Saving
        "zen" once was enough to stall the whole TikTok queue.
        """
        # Keep the case of a profile PATH (firefox:/home/... has a real path in
        # it); only the browser part is case-insensitive.
        browser = (browser or "").strip()
        if ":" in browser:
            head, tail = browser.split(":", 1)
            browser = f"{head.strip().lower()}:{tail}"
        else:
            browser = browser.lower()
        import tiktok_engine as _tt
        problem = _tt.cookie_spec_problem(browser)
        if problem:
            return {"ok": False, "error": problem, "browser": ""}
        with self.lock:
            self.settings.tiktok["cookies_from_browser"] = browser
        self.save_state()
        return {"ok": True, "browser": browser}

    def import_from_browser(self, cookiefile: str | None = None) -> dict:
        """Pull a session out of a browser and install it for instaloader.

        With no path given, pick the best candidate automatically.
        """
        if cookiefile:
            target = Path(cookiefile)
            res = import_browser_cookie(target)
        else:
            cands = [c for c in find_browser_sessions() if c["has_sessionid"]]
            if not cands:
                return {"ok": False,
                        "error": "No browser has an Instagram login. Sign in to "
                                 "Instagram in Firefox or Zen, then try again."}
            target = Path(cands[0]["path"])
            res = import_browser_cookie(target)

        if res.get("ok"):
            with self.lock:
                # A locked account is not auto-selected just because it was
                # freshly grabbed: the lock is the point.
                if res["username"].lower() not in {u.lower() for u in self.locked_sessions}:
                    self.active_session = res["username"]
                # a fresh session clears any stale auth cooldown
                self.consecutive_limits = 0
            self.save_state()
            res["browser"] = str(target)
        return res

    def set_session(self, username: str) -> bool:
        """Switch the instaloader session used for all downloads."""
        if username.lower() in {u.lower() for u in self.locked_sessions}:
            return False  # locked out; unlock it in Sessions first
        names = {s["username"] for s in list_sessions()}
        if username not in names:
            return False
        with self.lock:
            self.active_session = username
        self.save_state()
        return True

    def add_session(self, username: str) -> dict:
        """Install a session for `username` by importing it from a browser.

        Finds the browser store logged into that account and writes an
        instaloader session file for it, so several accounts can be kept
        side by side and switched between.
        """
        name = clean_profile_name(username)
        if not name:
            return {"ok": False, "error": "Enter an Instagram username."}

        # Prefer a store whose cookie user id we can match, else try each store
        # that has a sessionid until one resolves to this username.
        candidates = [b for b in find_browser_sessions() if b["has_sessionid"]]
        if not candidates:
            return {"ok": False,
                    "error": "No browser has an Instagram login. Log into that "
                             "account in Firefox or Zen first, then try again."}

        tried = []
        for b in candidates:
            res = import_browser_cookie(Path(b["path"]), name)
            if res.get("ok"):
                got = res["username"]
                tried.append(f"{b['browser']} → {got}")
                if got.lower() == name.lower():
                    with self.lock:
                        self.active_session = got
                    self.save_state()
                    return {"ok": True, "username": got, "browser": b["browser"]}
            else:
                tried.append(f"{b['browser']} → {res.get('error','failed')}")

        return {"ok": False,
                "error": f"None of the browser sessions belong to {name}. "
                         f"Tried: {'; '.join(tried)}"}

    def add_extra_pass(self, job_id: str, kind: str) -> dict:
        """Queue an extra pass (reels/tagged) for a profile already known.

        The new job is inserted directly after the source job so the extra
        content lands with its profile rather than at the end of the queue.
        """
        if kind not in ("reels", "tagged"):
            return {"ok": False, "error": "unknown pass"}

        with self.lock:
            src = self.jobs.get(job_id)
            if src is None:
                # allow calling by username too
                src = next((j for j in self.jobs.values()
                            if j.username == job_id or j.id == job_id), None)
            if src is None:
                return {"ok": False, "error": "profile not in the list"}

            # Don't stack duplicates of the same pass for one profile.
            existing = [j for j in self.jobs.values()
                        if j.username == src.username and j.kind == kind
                        and j.status in ("queued", "running", "retrying")]
            if existing:
                return {"ok": False, "error": f"{kind} pass already queued"}

            job = Job(username=src.username, kind=kind, parent_id=src.id,
                      settings={**(src.settings or {}), kind: True},
                      root=getattr(src, "root", ""))
            self.jobs[job.id] = job

            # Insert right after the source job when it is still waiting;
            # otherwise append (the source has already run).
            if src.id in self.pending:
                idx = self.pending.index(src.id) + 1
                self.pending.insert(idx, job.id)
            else:
                self.pending.append(job.id)
        self._kick()
        self.save_state()
        return {"ok": True, "username": src.username, "kind": kind}

    def forget_session(self, username: str) -> bool:
        """Delete a saved session file (used to clear a broken login)."""
        safe = clean_profile_name(username)
        if not safe:
            return False
        f = instaloader_config_dir() / f"session-{safe}"
        if not f.is_file():
            return False
        try:
            f.unlink()
        except OSError:
            return False
        with self.lock:
            if self.active_session == safe:
                self.active_session = None
        self.save_state()
        return True

    def _order(self) -> list[Job]:
        """Queued jobs in queue order, then everything else (newest first).

        Built so that a job which is queued but temporarily absent from the
        pending id list still shows up -- never silently hide a live job.
        """
        with self.lock:
            seen: set[str] = set()
            out: list[Job] = []
            # 1. jobs listed in the run order, in that order
            for jid in self.pending:
                j = self.jobs.get(jid)
                if j is not None and j.status == "queued":
                    out.append(j)
                    seen.add(jid)
            # 2. any remaining queued/retrying job not in pending (safety net)
            rest_q = [j for jid, j in self.jobs.items()
                      if jid not in seen and j.status in ("queued", "retrying")]
            rest_q.sort(key=lambda x: x.added_at)
            out.extend(rest_q)
            seen.update(j.id for j in rest_q)
            # 3. finished / cancelled / failed, newest first
            done = [j for jid, j in self.jobs.items()
                    if jid not in seen and j.status not in ("queued", "retrying")]
            done.sort(key=lambda x: x.added_at, reverse=True)
            out.extend(done)
        return out

    def _job_summary(self, job: Job) -> dict:
        """One job as the UI's poll sees it, without its log unless it matters.

        Log tails were 95% of a 1.6 MB snapshot (7.6k lines over 148 jobs, ~22 KB
        each even on finished jobs), and the UI polls every 1.2 s -- so the
        browser re-downloaded and re-parsed the entire log history continuously,
        which is what made every click feel dead. Only a running or failed job's
        log is worth shipping on every poll; everything else keeps a line count
        and fetches its log on demand via /api/job-log.
        """
        d = job.to_dict()
        tail = d.pop("tail", None) or []
        d["tail_len"] = len(tail)
        d["tail"] = tail if job.status in ("running", "retrying", "failed") else []
        return d

    def snapshot(self) -> dict:
        jobs = [self._job_summary(j) for j in self._order()]
        with self.lock:
            pending_count = len(self.pending)
            # The removed-jobs archive is capped at 500 entries and each carries
            # its own tail, so it is the same trap in slower motion. The UI only
            # needs the metadata there.
            removed = [dict(r, tail=[]) for r in list(reversed(self.removed))[-100:]]
        sess = self.session_info()
        return {
            # Per platform now, because both can be downloading at once. The
            # flat keys that follow are kept for older callers.
            "current": self.current,
            "current_instagram": self.current["instagram"],
            "current_tiktok": self.current["tiktok"],
            "paused": self.paused,
            "tt_paused": self.tt_paused,
            # Disk space, so the header can show it and a nearly-full drive can
            # explain why a queue is not moving.
            "disks": [r["disk"] for r in self.roots_status() if r["disk"]],
            "roots": self.roots_status(),
            # Where TikTok files actually go. Sent from here rather than worked
            # out again in the browser: a second copy of the location rule is
            # what made TikTok profiles vanish from the Gallery after the folder
            # moved, while everything else carried on working.
            "tiktok_path": str(tiktok_roots(self.output_root)[0]),
            "disk_worst": self.disk_worst(),
            "disk_blocked": dict(self.disk_blocked),
            "limit_paused": self._limit_paused,
            # `busy` must mean "a profile is being downloaded right now". It used
            # to be true whenever the worker thread was alive with anything
            # pending, so the pacing gap, a pause and a rate-limit wait all
            # reported busy=True with current=None -- which looks like a wedged
            # worker. The wait block below says which of those it is.
            "busy": any(self.current.values()),
            "queued": pending_count,
            "output_root": str(self.output_root),
            "instaloader": INSTALOADER,
            "session_present": sess["present"] and sess["valid"],
            "session_username": sess["username"] if sess["valid"] else None,
            "session_ok": sess["valid"],
            "session_problem": sess["problem"],
            "sessions": sess["sessions"],
            "active_session": self.active_session,
            "locked_sessions": list(self.locked_sessions),
            "settings": self.settings.to_dict(),
            "cooldown_remaining": self.cooldown_remaining(),
            # Auto-check state, so the UI can show "last checked" and "next in"
            # rather than leaving a toggle with invisible behaviour.
            "autocheck": {
                "on": bool(getattr(self.settings, "autocheck", False))
                      and float(getattr(self.settings, "autocheck_hours", 0) or 0) > 0,
                "hours": float(getattr(self.settings, "autocheck_hours", 0) or 0),
                "last": float(getattr(self, "_last_autocheck", 0) or 0),
                "note": getattr(self, "_autocheck_note", ""),
            },
            # Two unrelated waits can make a queue look idle. Report both,
            # labelled, so a countdown never implies the wrong one:
            #   "pacing"   -- the between-profiles gap (short, routine)
            #   "rate_limit" -- Instagram asked us to back off (long, an issue)
            # Per platform: Instagram being rate limited says nothing about
            # whether the TikTok worker is running.
            "wait": self.wait_reason("instagram"),
            "waits": {"instagram": self.wait_reason("instagram"),
                      "tiktok": self.wait_reason("tiktok")},
            # Which platform the queue will run next. The UI shows one platform
            # at a time, so a countdown belongs on that tab and not the other.
            "next_platform": self._next_platform(),
            "consecutive_limits": self.consecutive_limits,
            "removed": removed,
            "jobs": jobs,
            "counts": self._counts(),
        }

    def _counts(self) -> dict:
        with self.lock:
            keys = ("queued", "running", "done", "failed", "cancelled")
            c: dict = {k: 0 for k in keys}
            # Per platform as well, because the Download tab shows one platform
            # at a time: a global total would count profiles the user cannot
            # see on the tab they are looking at. Private is tracked apart from
            # the statuses because a private profile is still queued -- it just
            # will not download.
            by: dict[str, dict[str, int]] = {}
            for j in self.jobs.values():
                key = j.status if j.status in keys else ("running" if j.status == "retrying" else None)
                plat = getattr(j, "platform", "instagram")
                d = by.setdefault(plat, {k: 0 for k in keys + ("private",)})
                if key:
                    c[key] += 1
                    d[key] += 1
                if getattr(j, "private", False):
                    d["private"] += 1
            c["by_platform"] = by
            return c

    def _next_platform(self) -> str:
        """Platform of the job that will run next, for the per-tab countdown.

        Mirrors what the scheduler is about to do: the running job if there is
        one, otherwise the first queued job in order.
        """
        with self.lock:
            for j in self.jobs.values():
                if j.status in ("running", "retrying"):
                    return getattr(j, "platform", "instagram")
            for jid in self.pending:
                j = self.jobs.get(jid)
                if j is not None and j.status == "queued":
                    return getattr(j, "platform", "instagram")
        return "instagram"
    # ---------- worker ----------

    def _kick(self) -> None:
        self.start()

    def _terminate_proc(self, platform: str = "instagram") -> None:
        """Kill the child process belonging to one platform's worker.

        Each worker owns its own yt-dlp/instaloader process, so cancelling a
        TikTok profile must not kill the Instagram download running alongside it.
        """
        p = self._proc.get(platform)
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def _interrupt_running(self, platform: str) -> None:
        """Stop one platform's in-flight download, keeping the job in the queue.

        Sets the cancel flag so the worker abandons this run, plus `pause_current`
        so the worker's cleanup puts the job back in the queue instead of marking
        it cancelled. The actual re-queue happens in `_run`'s `finally`, which is
        the one place every exit path passes through -- including an attempt that
        is sitting in a retry backoff with no child process to kill.
        """
        if not self.current.get(platform):
            return  # nothing running: the pause just holds the next job back
        with self.lock:
            self.pause_current[platform] = True
            self.cancel_current[platform] = True
        self._terminate_proc(platform)

    def _requeue_paused(self, platform: str, job: Job) -> bool:
        """Put a profile that was paused mid-download back at the front of its queue.

        Called from the worker's cleanup, which is the one path every exit
        passes through. A pause is not a failed attempt, so `attempts` resets --
        otherwise a few pauses would exhaust `max_attempts` and the job would be
        written off as a throttle. Returns True when it re-queued the job.
        """
        with self.lock:
            paused_mid = self.pause_current[platform]
            self.pause_current[platform] = False
            if not paused_mid or job.status not in ("running", "retrying"):
                return False
            if job.id in self.pending:
                self.pending.remove(job.id)
            self.pending.insert(0, job.id)
            job.status = "queued"
            job.finished_at = None
            job.attempts = 0
            job.error = ""
            job.message = "paused mid-profile — restarts on resume"
            return True

    def _set(self, job: Job, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(job, k, v)

    def _log(self, job: Job, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        with self.lock:
            job.tail.append(line)
            if len(job.tail) > 120:
                del job.tail[:-120]
            # Remember the latest line so a resumed job still shows context.
            job.last_line = line.strip()[:200]

    def _retry_due_in(self, platform: str = "instagram") -> float | None:
        """Seconds until this platform's next automatic retry is due.

        None means nothing is waiting on the sweep: no failed job that is still
        eligible, or every eligible one has used its retries. Zero means a retry
        is due right now (a job that failed with no backoff set yet).
        """
        now = time.time()
        soonest: float | None = None
        with self.lock:
            for j in self.jobs.values():
                if getattr(j, "platform", "instagram") != platform:
                    continue
                if j.status != "failed" or j.fatal:
                    continue
                if j.auto_retries >= AUTO_RETRY_MAX:
                    continue
                if not j.next_retry_at or j.next_retry_at <= now:
                    return 0.0
                soonest = j.next_retry_at if soonest is None else min(soonest, j.next_retry_at)
        return None if soonest is None else max(0.0, soonest - now)

    def retry_now(self, platform: str = "instagram") -> int:
        """Put this platform's failed profiles back in the queue immediately.

        The sweep's delay exists so Instagram forgets a throttle, not to make a
        person wait after they have asked for a profile again. This does not
        spend an automatic retry, so pressing Start can never write a job off.
        """
        n = 0
        with self.lock:
            for jid, j in self.jobs.items():
                if getattr(j, "platform", "instagram") != platform:
                    continue
                if j.status != "failed" or j.fatal:
                    continue
                j.status = "queued"
                j.attempts = 0
                j.error = ""
                j.message = "retrying now"
                j.next_retry_at = 0.0
                j.finished_at = None
                self.pending[:] = [x for x in self.pending if x != jid]
                self.pending.append(jid)
                n += 1
        if n:
            self._kick()
            self.save_state()
        return n

    def _has_pending(self, platform: str) -> bool:
        """Is there a queued job for this platform waiting in the order?"""
        with self.lock:
            for jid in self.pending:
                j = self.jobs.get(jid)
                if j is not None and getattr(j, "platform", "instagram") == platform:
                    return True
            return False

    def skip_pacing(self, platform: str = "instagram") -> None:
        """End this platform's between-profiles gap immediately.

        Called when the user presses Start on that tab: waiting out a routine
        gap while the button plainly does nothing is indistinguishable from the
        app being stuck, which is exactly how it read.
        """
        with self.lock:
            self._skip_gap[platform] = True

    def _pacing_delay(self, platform: str) -> int:
        """The voluntary gap this platform takes before its next profile."""
        if platform == "tiktok":
            return max(0, int(self.settings.between_tiktok))
        return max(0, int(self.settings.between_profiles))

    def _next_job(self, platform: str = "instagram") -> Job | None:
        """Return the next runnable job for `platform`, leaving the order intact.

        Only this platform's jobs are considered. Both workers take from the one
        shared order, but each skips past the other's jobs, so a long Instagram
        queue can never hold up a TikTok download or the other way round.

        Only removes the id from self.pending once the job is actually handed to
        the worker, so a job that is temporarily not runnable is never lost.
        """
        with self.lock:
            for jid in list(self.pending):
                job = self.jobs.get(jid)
                if job is None:
                    self.pending.remove(jid)  # forgotten job: drop the stale id
                    continue
                if getattr(job, "platform", "instagram") != platform:
                    continue
                if job.status == "queued":
                    self.pending.remove(jid)
                    return job
            return None

    def _run(self, platform: str = "instagram") -> None:
        """One platform's worker loop.

        Instagram waits out pauses and rate-limit cooldowns, and stops when its
        session is broken. TikTok does none of that: yt-dlp has no session, does
        not share Instagram's request budget and has no 429s of its own, so
        gating it on Instagram's state would only make it wait for no reason.
        """
        ig = platform == "instagram"
        while not self.stop_requested:
            # Clear out anything whose backoff has elapsed before choosing the
            # next job, so a swept job joins the queue the moment it is eligible
            # rather than waiting for a separate timer.
            try:
                self.sweep_retries()
            except Exception:
                pass

            if ig:
                # Wait out both user-pause and any rate-limit cooldown BEFORE
                # taking a job. A 429 sets paused=True *and* cooldown_until, so
                # the two must be handled together or the worker sleeps forever
                # in the pause loop and never reaches the cooldown countdown.
                while not self.stop_requested:
                    # A 429 pause clears itself when its cooldown elapses; a
                    # pause the user asked for never does.
                    if self.paused and self._limit_paused and self.cooldown_remaining() <= 0:
                        with self.lock:
                            self.paused = False
                            self._limit_paused = False
                        self.save_state()
                    if self.paused:
                        self.save_state()  # keep the UI countdown fresh
                        time.sleep(1.0)
                        continue
                    with self.lock:
                        cooling = self.cooldown_remaining() > 0 and bool(self.pending)
                    if cooling:
                        self.save_state()
                        time.sleep(2.0)
                        continue
                    break
                if self.stop_requested:
                    break

                # A broken session fails every request with 401 and just burns
                # attempts. Stop and let the UI explain what to fix. Only
                # Instagram needs a session, so only this worker cares.
                if not self.session_info()["valid"] and self._has_pending("instagram"):
                    self.save_state()
                    return  # idle; the UI shows session_problem
            else:
                while not self.stop_requested and self.tt_paused:
                    time.sleep(1.0)
                if self.stop_requested:
                    break

            job = self._next_job(platform)
            if job is None:
                # Nothing runnable for this platform. Before giving up, re-sync
                # the order from jobs{}: a job can be "queued" yet missing from
                # pending (e.g. after a crash path or an interrupted edit).
                # Other-platform jobs are skipped here on purpose -- the other
                # worker owns them and this one must not adopt them.
                with self.lock:
                    stragglers = [jid for jid, j in self.jobs.items()
                                  if j.status == "queued" and jid not in self.pending]
                    for jid in stragglers:
                        self.pending.append(jid)
                if stragglers:
                    continue  # try again now that the order is repaired

                # Nothing to do. Before going idle, see whether an automatic
                # check for new posts is due -- it tops up Instagram profiles,
                # so the Instagram worker owns it.
                if ig and self.autocheck_due():
                    with self.lock:
                        self.current["instagram"] = "auto-check"
                    try:
                        self.autocheck()
                    except Exception:
                        pass
                    finally:
                        with self.lock:
                            self.current["instagram"] = None
                    if self._has_pending("instagram"):
                        continue

                # Nothing queued -- but a failed profile may be waiting out its
                # retry backoff. That retry is only ever fired by the sweep at
                # the top of this loop, so returning here would kill the one
                # thread that could pick it up: the job stayed red for good and
                # the platform looked stopped until someone pressed Start.
                # Stay alive until the earliest retry is due instead.
                wait = self._retry_due_in(platform)
                if wait is not None:
                    self.save_state()
                    end = time.time() + wait
                    while not self.stop_requested and time.time() < end:
                        # Start on this tab means "try it again now", not "wait
                        # out the rest of the backoff".
                        if self._skip_gap.get(platform):
                            with self.lock:
                                self._skip_gap[platform] = False
                            self.retry_now(platform)
                            break
                        time.sleep(min(2.0, max(0.1, end - time.time())))
                    if not self.stop_requested:
                        continue

                self.save_state()
                return  # genuinely idle; thread exits, this platform goes free

            # Space check, before a single byte is written. This is the moment
            # the app can decline to fill a drive further. The job has already
            # been taken off the queue, so put it back at the FRONT: it keeps
            # its place for the moment space is freed, instead of being consumed
            # and marked failed for a problem it did not cause.
            blocked = self.disk_blocks(job.root)
            if blocked:
                with self.lock:
                    if job.id in self.pending:
                        self.pending.remove(job.id)
                    self.pending.insert(0, job.id)
                    self.disk_blocked[platform] = blocked
                self.save_state()
                time.sleep(5.0)   # re-check periodically; space may be freed
                continue
            with self.lock:
                self.disk_blocked[platform] = ""

            with self.lock:
                self.current[platform] = job.username
                self.cancel_current[platform] = False
            self._set(job, status="running", started_at=time.time(),
                      message="starting", error="")
            t0 = time.time()
            try:
                self._process(job)
            except Exception as e:
                self._set(job, status="failed", error=f"engine error: {e}",
                          finished_at=time.time())
            finally:
                with self.lock:
                    self.current[platform] = None
                    self.cancel_current[platform] = False
                # Paused mid-profile? Then the job goes back in the queue rather
                # than being written off as cancelled.
                self._requeue_paused(platform, job)
                self._set(job, last_run_seconds=round(time.time() - t0, 1))
                self.save_state()

            if self.stop_requested:
                break

            # The gap this platform waits before its next profile. It depends on
            # whether that platform has more to do, not on the shared order.
            delay = self._pacing_delay(platform) if self._has_pending(platform) else 0
            if delay:
                # Vary it, once, here. The setting stays what it says; only the gap
                # actually waited is spread around it.
                delay = jitter(delay)
                # Publish the deadline so the UI can count down honestly, rather
                # than guessing from the last job's finish time.
                with self.lock:
                    self.wait_started[platform] = time.time()
                    self.wait_total[platform] = delay
                    self.wait_until[platform] = self.wait_started[platform] + delay
                waited = 0
                while waited < delay:
                    # Start pressed on this tab: drop the rest of the gap. The
                    # wait is voluntary pacing, not a limit we must respect, so
                    # the user can always cut it short.
                    if self._skip_gap.get(platform):
                        with self.lock:
                            self._skip_gap[platform] = False
                        break
                    if self.stop_requested or self.cancel_current[platform]:
                        break
                    if not ig and self.tt_paused:
                        time.sleep(0.5)   # paused: hold this platform's clock
                        continue
                    if ig and self.paused:
                        break             # the pause loop above takes over
                    waited += 1
                    time.sleep(1.0)
                with self.lock:
                    self.wait_started[platform] = 0.0
                    self.wait_total[platform] = 0.0
                    self.wait_until[platform] = 0.0

    def _process_tiktok(self, job: Job) -> None:
        """Download a TikTok profile with yt-dlp.

        Mirrors the Instagram path's shape: same target folder layout, same
        one-at-a-time worker, same "done but empty is a failure" rule. The
        difference is that yt-dlp keeps its own --download-archive, so a re-run
        quietly skips videos that are already on disk.
        """
        import tiktok_engine as tt

        if not tt.available():
            self._set(job, status="failed", finished_at=time.time(),
                      error="yt-dlp is not installed",
                      message="yt-dlp not found on PATH")
            return

        # TikTok gets its own folder next to the Instagram profiles rather than
        # mixed in with them. One shared folder made the gallery ambiguous and
        # meant a TikTok handle could collide with an Instagram profile of the
        # same name. Each platform stays independently deletable. A job pinned
        # to a drive keeps downloading there even if the downloads folder moves.
        # TikTok sits BESIDE the Instagram folder rather than inside it, so it
        # reads as a peer of Instagram instead of a sub-folder of it. A profile
        # already in the old nested location keeps downloading there, so a
        # layout change cannot silently start a second copy of an archive.
        base_root = Path(job.root) if job.root else self.output_root
        existing = next((c / job.username for c in tiktok_roots(base_root)
                         if (c / job.username).is_dir()), None)
        tt_root = existing.parent if existing else tiktok_roots(base_root)[0]

        # The profile picture, once, from the account's own page. yt-dlp does not
        # fetch one and a TikTok folder otherwise holds nothing but videos, so
        # without this the tile has no face. Already cached means already done, so
        # a retry costs a stat rather than a request.
        try:
            self.tiktok_avatar(job.username, fetch=True)
        except Exception:
            pass    # a missing picture must never fail a download
        target_dir = tt_root / job.username
        target_dir.mkdir(parents=True, exist_ok=True)
        # Say so, once. `tiktok_roots` only adopts a sibling folder it can
        # recognise as an archive; dropping this marker is what makes that
        # recognition certain for a folder it just created, rather than relying
        # on the downloads inside it to be spotted later.
        marker = tt_root / TIKTOK_MARKER
        if not marker.exists():
            try:
                marker.write_text(
                    "This folder holds this app's TikTok archives "
                    "(yt-dlp downloads).\n", encoding="utf-8")
            except OSError:
                pass
        archive = tt.archive_path(tt_root, job.username)

        count = int(getattr(job, "tiktok_count", 0) or 0)
        if job.attempts >= job.max_attempts:
            # Handing a job over that has nothing left to spend must not leave it
            # looking like it is still running: the worker never returns a
            # "running" job to the queue, so the row would sit there forever.
            self._set(job, status="failed", finished_at=time.time(),
                      message="gave up after max attempts",
                      error=job.error or f"stopped after {job.attempts} attempts")
            return
        job.attempts += 1

        # Per-job settings win so a queued job keeps the options it was added
        # with, exactly like the Instagram side.
        saved = getattr(job, "settings", None) or {}
        tt_opts = saved.get("tiktok") if isinstance(saved, dict) else None
        ttset = tt.TikTokSettings.from_dict(tt_opts or self.settings.tiktok)
        # The held session is a credential, not a per-job preference, so it
        # applies to every job. A job queued before the session existed carries
        # its own tiktok settings without this key, and from_dict copies only the
        # keys that are there -- without this line those jobs would keep reading
        # the browser and quietly ignore the session the app holds.
        if not ttset.cookies_file:
            ttset.cookies_file = str(self.settings.tiktok.get("cookies_file") or "")

        # "All" can be made to mean "a slice per turn". Only when the profile is
        # actually set to all: a profile given an explicit number already fetches
        # that many and then finishes normally. `chunk` is this run's allowance --
        # zero means the run is a whole profile in one go, as it always was.
        chunk = 0
        if count <= 0 and getattr(ttset, "chunk_all", False):
            chunk = max(1, int(getattr(ttset, "chunk_size", 25) or 25))

        cmd = tt.build_download_cmd(job.username, target_dir, archive, count,
                                    settings=ttset)
        self._log(job, f"$ {' '.join(cmd)}")
        self._set(job, message=f"attempt {job.attempts}/{job.max_attempts}")

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, start_new_session=True,
            )
        except Exception as e:
            self._set(job, status="failed", finished_at=time.time(),
                      error=f"{type(e).__name__}: {e}")
            return

        with self.lock:
            self._proc["tiktok"] = proc

        downloaded = 0
        warned = False
        disk_stopped = ""
        seen = 0
        for line in (proc.stdout or []):
            if self.cancel_current["tiktok"] or self.stop_requested:
                break
            seen += 1
            # The check before the job starts keeps a run from being started on
            # a nearly-full drive, but one profile can be bigger than the whole
            # reserve. If the drive fills while this is running, stop here rather
            # than write until the volume itself errors. Every 20 lines is a
            # couple of seconds of output: statvfs on every line is pointless.
            if seen % 20 == 0:
                disk_stopped = self.disk_blocks(job.root)
                if disk_stopped:
                    self._log(job, f"stopping: {disk_stopped}")
                    break
            line = line.rstrip()
            if not line:
                continue
            kind = tt.classify(line)
            if kind == "started-file":
                downloaded += 1
                self._set(job, posts=downloaded, last_line=line[:200])
            elif kind == "progress":
                self._set(job, last_line=line[:200])
            elif kind == "warn":
                warned = True
                # yt-dlp says this for a profile it cannot see. Record it on the
                # job so the queue can show it in red: it is not a temporary
                # failure, and retrying it will never succeed.
                if "private or has embedding disabled" in line.lower():
                    self._set(job, private=True)
                self._log(job, line)
            elif kind == "already":
                self._log(job, line)
            else:
                self._log(job, line)

        rc = proc.wait()
        with self.lock:
            self._proc["tiktok"] = None

        if disk_stopped:
            # Out of space, not a failure: the files already written stay, and
            # the job goes back in the queue so it resumes once space is freed.
            # yt-dlp's archive means the finished videos are not fetched twice.
            self._terminate_proc("tiktok")
            with self.lock:
                self.disk_blocked["tiktok"] = disk_stopped
            self._set(job, status="queued", finished_at=None, fatal=False,
                      error="", message="stopped: drive nearly full")
            self.save_state()
            return

        if self.cancel_current["tiktok"]:
            if self.pause_current["tiktok"]:
                # Paused, not cancelled: _run's finally re-queues this job.
                return
            self._set(job, status="cancelled", finished_at=time.time(), message="cancelled")
            return
        if self.stop_requested:
            self._set(job, status="queued", message="stopped")
            return

        files = len(list(target_dir.glob("*.mp4")))
        total_bytes = sum(p.stat().st_size for p in target_dir.glob("*.mp4"))

        if rc != 0 and files == 0:
            # A private profile is fatal, not a failure worth retrying, and the
            # queue keeps showing it in red so it is not mistaken for a profile
            # that will work next time.
            if getattr(job, "private", False):
                self._set(job, status="failed", finished_at=time.time(), fatal=True,
                          error="private profile - yt-dlp cannot download it",
                          message="private, cannot download")
                return
            self._set(job, status="failed", finished_at=time.time(),
                      error=f"yt-dlp exited {rc}", message="download failed")
            return

        if files == 0:
            # Matches the Instagram rule: a clean run that produced nothing is
            # not a success. Usually means the profile is private or empty.
            self._set(job, status="failed", finished_at=time.time(),
                      message="no videos downloaded")
            return

        if chunk and downloaded >= chunk:
            # This turn used its whole allowance, so there is probably more to
            # come. Rather than sitting here until the profile is finished, it
            # goes to the BACK of the queue: the next profile gets a turn, and
            # this one comes round again. The next turn picks up where this one
            # stopped because yt-dlp's archive is what --max-downloads reads --
            # nothing already fetched is fetched twice.
            with self.lock:
                if job.id in self.pending:
                    self.pending.remove(job.id)
                self.pending.append(job.id)
            self._set(job, status="queued", finished_at=None, attempts=0,
                      posts=files, total_files=files, total_bytes=total_bytes,
                      error="", message=f"{downloaded} this turn — back of the queue")
            self.save_state()
            return

        self._set(job, status="done", finished_at=time.time(),
                  posts=files, total_files=files, total_bytes=total_bytes,
                  message=("done, some videos skipped" if warned else "done"))
        self.save_state()

    def _process(self, job: Job) -> None:
        # TikTok jobs are a different tool with different output; keep the two
        # paths apart so instaloader's flags and retry rules never leak into
        # yt-dlp runs (or the reverse).
        if getattr(job, "platform", "instagram") == "tiktok":
            return self._process_tiktok(job)

        # A job pinned to a drive keeps downloading there even after the
        # downloads folder moves, so one profile never splits across roots.
        target_dir = (Path(job.root) if job.root else self.output_root) / job.username
        target_dir.mkdir(parents=True, exist_ok=True)
        session_user = self.session_username()
        cfg = job.effective(self.settings)

        while job.attempts < job.max_attempts and not self.stop_requested and not self.cancel_current["instagram"]:
            job.attempts += 1
            cmd = self._build_cmd(job, target_dir, session_user, cfg)
            self._log(job, f"$ {' '.join(cmd)}")
            self._set(job, message=f"attempt {job.attempts}/{job.max_attempts}")

            outcome = self._exec(job, cmd, target_dir)

            if self.cancel_current["instagram"]:
                if self.pause_current["instagram"]:
                    # A pause, not a cancel: leave the status alone so _run's
                    # finally puts this job back in the queue.
                    return
                self._set(job, status="cancelled", finished_at=time.time(), message="cancelled")
                return
            if outcome in ("ok", "ok_warned"):
                with self.lock:
                    self.consecutive_limits = 0
                n_media = self._count_media(target_dir)
                if n_media == 0 and cfg.posts:
                    # Ran clean but grabbed no posts at all: not a success. Most
                    # often Instagram served a degraded response. Retry rather
                    # than record an empty archive as done.
                    #
                    # Counted across every archive root, not just the one this
                    # run wrote to: a profile archived on another drive used to
                    # read as zero here and be marked failed while complete.
                    self._set(job, status="failed", finished_at=time.time(),
                              message="no posts downloaded",
                              error="instaloader reported success but no posts were saved")
                    return
                msg = "complete" if outcome == "ok" else "complete (with warnings)"

                # Self-heal a fragment: if instaloader said there were more posts
                # than it fetched, re-queue once so the gap actually gets filled
                # instead of being frozen forever.
                #
                # This used to switch fast_update OFF for the heal, which was
                # both destructive and permanent: --fast-update stops at the
                # first file already on disk, so the heal walked and rewrote the
                # entire profile -- thousands of files that were already there --
                # and the job kept that setting forever, so every later run did
                # the same. The gap is usually near the top of the profile, where
                # fast-update still reaches it, and files already present are
                # skipped either way, so healing with fast-update ON fills the
                # hole without touching what is already archived.
                partial, why = self._is_partial(job, target_dir)
                if partial and job.heal_attempts < 2:
                    with self.lock:
                        self.consecutive_limits = 0
                    job.heal_attempts += 1
                    self._set(job,
                              status="queued",
                              started_at=None,
                              finished_at=None,
                              done_count=0,
                              total_count=0,
                              message=f"partial ({why}) — checking for the missing posts")
                    self._log(job, f"! only {job.done_count}/{job.total_count} posts; "
                                   f"re-queueing a check for the gap (keeping "
                                   f"--fast-update so nothing is re-downloaded)")
                    with self.lock:
                        self.pending[:] = [x for x in self.pending if x != job.id]
                        self.pending.append(job.id)
                    self.save_state()
                    self._kick()
                    return

                if partial:
                    # Gave up healing; record it honestly rather than as success.
                    self._set(job, status="done", finished_at=time.time(),
                              message=f"PARTIAL — {why}, gave up after {job.heal_attempts} retries",
                              error="")
                    return

                self._set(job, status="done", finished_at=time.time(),
                          message=msg, error="")
                return
            if outcome == "fatal":
                # A "fatal" marker can still be a false positive (e.g. an HTTP
                # error mentioned in a warning line). If media actually landed,
                # the archive is fine -- record it as done with warnings.
                if self._count_media(target_dir) > 0:
                    with self.lock:
                        self.consecutive_limits = 0
                    self._set(job, status="done", finished_at=time.time(),
                              message="complete (warnings ignored)",
                              error="")
                    return
                self._set(job, status="failed", finished_at=time.time(),
                          message="unrecoverable", error=job.error or "unrecoverable error")
                return

            # instaloader crashed on stories/highlights (an API-shape bug:
            # Instagram returns 200 with a "fail" payload, so installoader's
            # KeyError fires). Those extras run BEFORE posts, so the archive is
            # usually still empty -- retry once with the extras disabled rather
            # than reporting a success that never downloaded anything.
            if outcome == "crash_optional":
                if cfg.stories or cfg.highlights:
                    # IMPORTANT: also disable fast_update for the retry. The
                    # crashed run may already have written the newest posts, and
                    # with --fast-update instaloader stops at the first file it
                    # finds -- so it would stop immediately and leave the profile
                    # partial (e.g. 6 posts out of 361).
                    extra = {**(job.settings or {}),
                             "stories": False, "highlights": False,
                             "fast_update": False}
                    self._set(job, settings=extra, status="running", error="",
                              message="stories/highlights failed — retrying without them")
                    self._log(job, "! stories/highlights crashed instaloader; "
                                   "retrying the full profile without them")
                    cfg = job.effective(self.settings)
                    continue
                # Extras already off and it still crashed: nothing more to try.
                self._set(job, status="failed", finished_at=time.time(),
                          message="instaloader crashed",
                          error=job.error or "crash with stories/highlights disabled")
                return

            # A plain crash: do not burn a rate-limit cooldown on it.
            if outcome == "crash":
                self._set(job, status="failed", finished_at=time.time(),
                          message="instaloader crashed",
                          error=job.error or "instaloader crashed")
                return

            # Instagram pushed back. Set a GLOBAL cooldown (persisted, so a
            # restart can't bypass it) rather than just waiting inside this job.
            backoff = DEFAULT_RATE_LIMIT_COOLDOWN
            if job.retry_after:
                backoff = max(backoff, min(job.retry_after, 2 * 3600))
            # Longer at random, never shorter: this exists to let a block clear,
            # and coming back early is what makes the block longer.
            backoff = jitter(backoff, COOLDOWN_JITTER, up_only=True)
            note = getattr(self, "_retry_note", "")
            self._retry_note = ""
            with self.lock:
                self.cooldown_until = max(self.cooldown_until, time.time() + backoff)
                self.consecutive_limits += 1
                tripped = self.consecutive_limits >= RATE_LIMIT_TRIP
                if tripped:
                    self.paused = True
                    self._limit_paused = True  # auto-clears when cooldown ends
            self._set(job, status="retrying",
                      message=note or
                      f"rate limited — cooling down {max(1, round(backoff / 60))} min "
                      f"(attempt {job.attempts})")
            self.save_state()
            if tripped:
                # Stop hammering entirely; the user resumes when it's safe.
                self._set(job, status="queued", message="paused: rate limit")
                return
            waited = 0
            while waited < backoff and not self.stop_requested and not self.cancel_current["instagram"]:
                if not self.paused:
                    waited += 1
                time.sleep(1.0)
            self._set(job, status="running", retry_after=0)

        # Attempts exhausted. Without this terminal status the job is left
        # "queued" and silently restarts forever.
        if not self.cancel_current["instagram"] and job.status not in ("done", "failed", "cancelled"):
            # Schedule an automatic retry rather than leaving a dead card the
            # user has to clear by hand. The first delay is set here so the
            # sweep has a deadline to work from.
            if job.fatal:
                self._set(job, status="failed", finished_at=time.time(),
                          message="gave up", error=job.error)
            else:
                self._set(job, status="failed", finished_at=time.time(),
                          next_retry_at=time.time() + AUTO_RETRY_DELAYS[0],
                          message=("will retry automatically in "
                                   f"{AUTO_RETRY_DELAYS[0] // 60} min "
                                   f"(Instagram throttled it)"),
                          error=job.error or "still rate limited after max attempts")

    def _build_cmd(self, job: Job, target_dir: Path, session_user: str | None,
                   s: Settings) -> list[str]:
        cmd = [INSTALOADER, f"--dirname-pattern={target_dir}"]

        # The picture is fetched on every run whatever --fast-update says, and
        # the high-quality copy is an extra API call. Once the picture is on
        # disk there is nothing left to collect, so stop asking: a "fail"
        # payload on that call is what turns a finished profile into a run that
        # exits non-zero, retries, and fails again over a picture it already has.
        want_pic = s.profile_pic and not has_profile_pic(target_dir)

        # metadata / json
        if s.compress_json:
            pass  # default compresses
        else:
            cmd.append("--no-compress-json")
        if not s.metadata_json:
            cmd.append("--no-metadata-json")
        if not s.captions:
            cmd.append("--no-captions")
        if s.comments:
            cmd.append("--comments")
        if s.geotags:
            cmd.append("--geotags")

        # what of each post
        if not s.pictures:
            cmd.append("--no-pictures")
        if not s.videos:
            cmd.append("--no-videos")
        if not s.video_thumbnails:
            cmd.append("--no-video-thumbnails")
        if s.slide:
            cmd += ["--slide", str(s.slide)]

        # which of each profile
        if job.kind == "reels":
            # A reels job is its own pass: reels only, nothing else. instaloader
            # treats download_posts/tagged/reels/igtv as one "profile content"
            # group, so --reels with everything else off does just reels.
            cmd.append("--reels")
            cmd.append("--no-posts")
            if not want_pic:
                cmd.append("--no-profile-pic")
        elif job.kind == "tagged":
            # Tagged posts as their own pass, same reasoning as reels.
            cmd.append("--tagged")
            cmd.append("--no-posts")
            if not want_pic:
                cmd.append("--no-profile-pic")
        else:
            if not s.posts:
                cmd.append("--no-posts")
            if not want_pic:
                cmd.append("--no-profile-pic")
            if s.stories:
                cmd.append("--stories")
            if s.highlights:
                cmd.append("--highlights")
            if s.tagged:
                cmd.append("--tagged")
            # Reels are deliberately NOT added here: they run as a separate
            # follow-up job so one pass never does two things at once.
            if s.igtv:
                cmd.append("--igtv")

        # which posts
        if s.fast_update:
            cmd.append("--fast-update")
        # Records the newest post scraped per profile, independent of the media
        # folder — so moving/deleting the output dir does NOT cause re-downloads.
        if s.latest_stamps:
            cmd.append("--latest-stamps")
        if s.post_filter:
            cmd += ["--post-filter", s.post_filter]
        if s.storyitem_filter:
            cmd += ["--storyitem-filter", s.storyitem_filter]

        # how
        if s.sanitize_paths:
            cmd.append("--sanitize-paths")
        if not s.resume:
            cmd.append("--no-resume")
        if s.max_connection_attempts:
            cmd += ["--max-connection-attempts", str(s.max_connection_attempts)]
        if s.request_timeout:
            cmd += ["--request-timeout", str(s.request_timeout)]
        if not s.iphone:
            cmd.append("--no-iphone")

        if session_user:
            cmd.append(f"--login={session_user}")

        # last positional arg is the target
        cmd.append(job.username)
        return cmd

    def _exec(self, job: Job, cmd: list[str], target_dir: Path) -> str:
        """Run instaloader, stream output. Returns 'ok' | 'retryable' | 'fatal'."""
        fatal_hit = False
        blocked_hint = False
        retry_hit = False
        crash_hit = False
        crash_optional = False
        crash_line = ""
        pic_warn = False
        session_hit = False
        last_err = ""
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            self._set(job, error=f"instaloader not found at {INSTALOADER}")
            return "fatal"

        self._proc["instagram"] = proc
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                self._log(job, line)
                # instaloader prints "[ n/m ]" per post — the only honest
                # progress figure it offers. Capture it for the UI.
                pm = PROGRESS_RE.search(line)
                if pm:
                    try:
                        job.done_count = int(pm.group(1))
                        job.total_count = int(pm.group(2))
                        # Persist occasionally so a restart or crash mid-download
                        # still shows how far this job got.
                        if job.done_count % 25 == 0:
                            self.save_state()
                    except ValueError:
                        pass
                m = WAIT_HINT.search(line)
                if m:
                    try:
                        job.retry_after = max(job.retry_after, int(m.group(1)))
                    except ValueError:
                        pass
                if CRASH.search(line) and not session_hit:
                    crash_hit = True
                    crash_line = line.strip()
                if CRASH_IN_OPTIONAL.search(line) and not session_hit:
                    crash_optional = True
                if BLOCKED_HINT.search(line):
                    # instaloader's own note that it is retrying anonymously
                    # because this may be a block rather than a deletion.
                    blocked_hint = True
                if SESSION_PROMPT.search(line):
                    # It is asking for a password because the session check
                    # failed. Everything after this -- the traceback, the
                    # EOFError -- is fallout, not the diagnosis.
                    session_hit = True
                elif PROFILE_PIC_WARN.search(line):
                    # Nothing about the picture may fail a run. instaloader
                    # files it under "Errors or warnings occurred" and exits
                    # non-zero, often over a profile that is already fully
                    # archived -- so it must not set the error, a retry, or a
                    # fatal verdict.
                    pic_warn = True
                elif FATAL.search(line):
                    fatal_hit = True
                    last_err = line.strip()
                elif RETRYABLE.search(line):
                    retry_hit = True
                    last_err = line.strip()
        finally:
            proc.wait()
            self._proc["instagram"] = None
        self._parse_counts(job, target_dir)

        if self.cancel_current["instagram"]:
            return "ok"

        # instaloader could not verify the saved session, so it asked for a
        # password and died on an empty stdin. Nothing is wrong with the profile
        # or the archive: Instagram answered the session check with 401 "please
        # wait a few minutes", which is a throttle. Back off like a rate limit
        # rather than failing the job, burning an attempt, or retrying at once.
        if session_hit:
            self._set(job, error="")
            self._retry_note = (
                "Instagram rejected the session check (401) — cooling down. "
                "If this keeps happening, switch to another saved session."
            )
            return "retryable"

        # A Python crash must never be mistaken for a rate limit: retrying the
        # same arguments would crash identically and falsely burn a cooldown.
        if crash_hit:
            self._set(job, error=crash_line or "instaloader crashed")
            return "crash_optional" if crash_optional else "crash"

        # "Profile X does not exist" is also how Instagram answers a throttled
        # client, and instaloader prints its own "helps in case you are just
        # blocked" hint when that is what it suspects. Treating that as fatal
        # marked real profiles as permanently gone -- the profile page returned
        # 200 and the account was followable the whole time. So when the hint is
        # present, this is a retryable block: back off, try again later, and
        # leave it queued rather than writing it off.
        if fatal_hit and blocked_hint:
            self._set(job, error=(last_err or "blocked by Instagram")
                      + "  (Instagram is throttling this profile; it will be "
                        "retried rather than marked missing)")
            return "retryable"

        if proc.returncode == 0:
            return "fatal" if (fatal_hit and not retry_hit) else "ok"
        if fatal_hit and not retry_hit:
            # A profile that genuinely does not exist, or is private, is not a
            # throttle: mark it fatal so the retry sweep never picks it up and
            # quietly burns hours retrying something that cannot succeed.
            self._set(job, error=last_err, fatal=True)
            return "fatal"
        if not retry_hit:
            # instaloader exits non-zero when it merely logged warnings (e.g.
            # "Unable to fetch high quality profile pic", or Instagram replying
            # 200 with a "fail" payload). If media actually landed, that is a
            # success with warnings -- not a failure worth retrying.
            if self._count_media(target_dir) > 0:
                self._set(job, error="")
                return "ok_warned"
            # No content at all and no recognised marker: treat as a crash, not
            # a rate limit, so we don't wait 15 minutes for nothing.
            note = " (the profile picture was the only thing reported)" if pic_warn else ""
            self._set(job, error=last_err or f"exit code {proc.returncode}{note}")
            return "crash"
        self._set(job, error=last_err or f"exit code {proc.returncode}")
        return "retryable"

    def _count_media(self, target_dir: Path) -> int:
        """Number of media files saved for a profile (excludes metadata/json).

        Counts the given folder first, then the same profile in EVERY other
        archive root. Without that, a profile whose files live on a different
        drive than the current downloads folder counted as zero -- and the
        caller treats zero as "nothing was downloaded", so a perfectly complete
        archive was marked failed instead of done.
        """
        exts = (".jpg", ".jpeg", ".png", ".mp4", ".webp", ".heic")
        seen: set[str] = set()
        n = 0

        def _tally(folder: Path) -> int:
            if not folder.is_dir():
                return 0
            # Two roots can be the same place spelled two ways, so count each
            # real directory once.
            real = _resolve(folder)
            key = str(real) if real is not None else str(folder)
            if key in seen:
                return 0
            seen.add(key)
            found = 0
            try:
                for p in folder.rglob("*"):
                    if p.is_file() and p.suffix.lower() in exts \
                            and "_profile_pic" not in p.name:
                        found += 1
            except OSError:
                pass
            return found

        n += _tally(target_dir)
        name = target_dir.name
        for root in self.roots:
            if _same(root, target_dir.parent):
                continue          # already counted above
            n += _tally(root / name)
        return n

    def _is_partial(self, job: Job, target_dir: Path) -> tuple[bool, str]:
        """Detect a run that clearly did not finish the profile.

        A finished job can still be a fragment: Instagram refuses pagination
        (`500`/"fail") or instaloader crashes on stories, leaving only the newest
        page. With --fast-update a later re-run would stop at the first file and
        never fill the gap, so these must be retried with fast_update OFF.

        Heuristic: instaloader reported a total ("[n/m]") that is far larger than
        the number of posts actually completed.
        """
        total = job.total_count or 0
        done = job.done_count or 0
        if total > 0 and done < total:
            # It stopped short of what instaloader itself said was there.
            missing = total - done
            if missing >= 5:
                return True, f"stopped at {done}/{total} posts"
        return False, ""

    def _parse_counts(self, job: Job, target_dir: Path) -> None:
        """Recount files/size on disk, throttled.

        A full rglob on a multi-GB archive is expensive, and it used to run on
        every log line. Only rescan every few seconds.
        """
        now = time.time()
        if now - job._counted_at < 5.0:
            return
        job._counted_at = now
        files = 0
        total = 0
        posts = 0
        try:
            for p in target_dir.rglob("*"):
                if p.is_file():
                    files += 1
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
                    if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".mp4", ".webp", ".heic"):
                        posts += 1
        except OSError:
            return
        with self.lock:
            job.total_files = files
            job.total_bytes = total
            job.posts = posts


if __name__ == "__main__":
    print(json.dumps({"instaloader": INSTALOADER, "output_root": str(OUTPUT_ROOT)}, indent=2))
