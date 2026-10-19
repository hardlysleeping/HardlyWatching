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

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Where downloads and app state live. Overridable so this works on any machine
# (and so no personal path is baked into the source).
OUTPUT_ROOT = Path(os.environ.get("INSTA_OUTPUT", Path.home() / "instagram"))
STATE_FILE = Path(os.environ.get(
    "INSTA_STATE", Path.home() / ".config" / "insta-desktop" / "state.json"))
INSTALOADER = shutil.which("instaloader") or str(Path.home() / ".local/bin/instaloader")

BETWEEN_PROFILES_SECONDS = 240  # 4 min. instaloader's own RateController already throttles
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
    r"\b404\b|does not exist|"
    r"is private|Private profile|"
    r"Wrong login|HTTPError 401)",
    re.IGNORECASE,
)

# Instagram telling us how long to wait: "retried in 664 seconds".
WAIT_HINT = re.compile(r"retried in (\d+) seconds", re.IGNORECASE)

# instaloader's per-post progress: "[ 404/764]". The only honest progress figure.
PROGRESS_RE = re.compile(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]")

PROFILE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def list_sessions() -> list[dict]:
    """Every saved instaloader session, newest first, each with a validity check."""
    cfg = Path.home() / ".config" / "instaloader"
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


def session_diagnosis(active: str | None = None) -> dict:
    """Inspect instaloader sessions without network access.

    `active` selects which session to treat as current (by username). When
    None, the chosen session from config, else the newest, is used.

    A session file can exist yet be useless: instaloader writes one after a
    login that Instagram accepted but never issued a sessionid for (an
    unfinished checkpoint), which then fails every request with 401.
    """
    sessions = list_sessions()
    if not sessions:
        return {"present": False, "username": None, "valid": False,
                "problem": "No instaloader session found. Log in with the button above.",
                "sessions": []}

    chosen = None
    if active:
        chosen = next((s for s in sessions if s["username"] == active), None)
    if chosen is None:
        chosen = next((s for s in sessions if s["valid"]), sessions[0])

    problem = chosen["problem"]
    if not chosen["valid"]:
        problem = (f"Login for {chosen['username']} is incomplete — Instagram issued no "
                   f"session token. Log in again with the button above.")

    return {
        "present": True,
        "username": chosen["username"],
        "valid": chosen["valid"],
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
    ("Firefox",        ".mozilla/firefox/*/cookies.sqlite"),
    ("Firefox",        ".config/mozilla/firefox/*/cookies.sqlite"),
    ("Firefox",        ".var/app/org.mozilla.firefox/.mozilla/firefox/*/cookies.sqlite"),
    ("Zen",            ".zen/*/cookies.sqlite"),
    ("Zen",            ".var/app/app.zen_browser.zen/.zen/*/cookies.sqlite"),
    ("LibreWolf",      ".librewolf/*/cookies.sqlite"),
    ("LibreWolf",      ".var/app/io.gitlab.librewolf-community/.librewolf/*/cookies.sqlite"),
]


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
    """Instagram logins available in installed browsers, best candidate first."""
    home = Path.home()
    out: list[dict] = []
    seen: set[str] = set()
    for label, pattern in BROWSER_GLOBS:
        for p in home.glob(pattern):
            rp = str(p.resolve())
            if rp in seen or not p.is_file():
                continue
            seen.add(rp)
            entry = {"browser": label, "path": str(p), "cookies": 0,
                     "has_sessionid": False, "mtime": 0.0}
            try:
                entry["mtime"] = p.stat().st_mtime
            except OSError:
                pass
            try:
                c = read_browser_cookies(p)
                entry["cookies"] = len(c)
                entry["has_sessionid"] = bool(c.get("sessionid"))
            except Exception:
                pass
            out.append(entry)
    out.sort(key=lambda e: (not e["has_sessionid"], -e["mtime"]))
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
        return s


@dataclass
class Job:
    username: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "queued"  # queued running retrying done failed skipped cancelled
    kind: str = "main"  # "main" | "reels" — reels runs as its own follow-up job
    parent_id: str | None = None  # for a reels job: the main job it follows
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
    _counted_at: float = 0.0  # throttle for the on-disk rescan

    def effective(self, base: Settings) -> Settings:
        if not self.settings:
            return base
        return Settings.from_dict({**base.to_dict(), **self.settings})

    def to_dict(self) -> dict:
        return asdict(self)


class Engine:
    """Single background worker with a mutable, reorderable queue."""

    def __init__(self, output_root: Path = OUTPUT_ROOT):
        self.output_root = output_root
        self.q: "queue.Queue[Job]" = queue.Queue()
        # Ordered list of job ids waiting to run (source of truth for order/priority).
        self.pending: list[str] = []
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.worker: threading.Thread | None = None
        self.current: str | None = None
        self.paused = False
        self.stop_requested = False
        self.cancel_current = False
        self.settings = Settings()
        self.active_session: str | None = None  # chosen instaloader session
        self._proc: subprocess.Popen | None = None
        # Wall-clock time before which NO request may be made. Persisted, so
        # restarting the app cannot bypass a rate-limit cooldown.
        self.cooldown_until: float = 0.0
        self.consecutive_limits: int = 0
        # True when the current pause was caused by a rate limit (auto-clears
        # when the cooldown expires) rather than by the user (stays paused).
        # Jobs the user removed, kept so they can be restored.
        self.removed: list[dict] = []
        self._limit_paused: bool = False
        self._load_state()

    # ---------- persistence ----------

    def _load_state(self) -> None:
        try:
            if STATE_FILE.is_file():
                d = json.loads(STATE_FILE.read_text())
                self.settings = Settings.from_dict(d.get("settings", {}))
                self.active_session = d.get("active_session") or None
                self.cooldown_until = float(d.get("cooldown_until") or 0.0)
                self.consecutive_limits = int(d.get("consecutive_limits") or 0)
                self.removed = list(d.get("removed") or [])
                # Restore a rate-limit pause so the worker waits it out after a
                # restart instead of hammering Instagram immediately.
                if d.get("limit_paused"):
                    self._limit_paused = True
                    self.paused = True
                for jd in d.get("jobs", []):
                    if not jd.get("username"):
                        continue
                    j = Job(**{k: v for k, v in jd.items() if k in Job.__dataclass_fields__})
                    # Jobs interrupted by a restart are not still running.
                    if j.status in ("running", "retrying"):
                        j.status = "queued"
                        j.message = "resumed after restart"
                        j.attempts = 0
                    self.jobs[j.id] = j
                # Restore the saved run order first, then append anything queued
                # that the order list did not mention.
                for jid in (d.get("order") or []):
                    job = self.jobs.get(jid)
                    if job is not None and job.status == "queued" and jid not in self.pending:
                        self.pending.append(jid)
                for jid, j in self.jobs.items():
                    if j.status == "queued" and jid not in self.pending:
                        self.pending.append(jid)
        except Exception:
            pass  # corrupt state is not fatal

    def save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with self.lock:
                jobs = [j.to_dict() for j in self.jobs.values()]
                data = {
                    "settings": self.settings.to_dict(),
                    "active_session": self.active_session,
                    "cooldown_until": self.cooldown_until,
                    "consecutive_limits": self.consecutive_limits,
                    "limit_paused": self._limit_paused,
                    # Persist the run order explicitly: rebuilding it from a dict
                    # on restart would scramble the queue.
                    "order": list(self.pending),
                    "removed": self.removed,
                    "jobs": jobs,
                }
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(STATE_FILE)
        except Exception:
            pass

    # ---------- public API ----------

    def start(self) -> None:
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
            self.stop_requested = False
            self.worker = threading.Thread(target=self._run, daemon=True)
            self.worker.start()

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
        """Restart the worker if it died while work remains.

        The worker thread exits when it believes it is idle. If jobs are still
        queued at that moment, something went wrong -- revive it rather than
        leaving the queue stalled until the user notices.
        """
        with self.lock:
            work = [jid for jid, j in self.jobs.items() if j.status == "queued"]
            alive = bool(self.worker and self.worker.is_alive())
        if work and not alive and not self.paused:
            self.start()

    def update_settings(self, patch: dict) -> Settings:
        with self.lock:
            self.settings = Settings.from_dict({**self.settings.to_dict(), **(patch or {})})
            cur = self.settings
        self.save_state()
        return cur

    def add(self, raw_names: list[str], overrides: dict | None = None) -> tuple[list[Job], list[str]]:
        added, rejected = [], []
        with self.lock:
            active = {
                j.username for j in self.jobs.values()
                if j.status in ("queued", "running", "retrying")
            }
            for raw in raw_names:
                name = clean_profile_name(raw)
                if not name:
                    if raw.strip():
                        rejected.append(raw.strip())
                    continue
                if name in active:
                    rejected.append(f"{name} (already queued)")
                    continue

                # A reels job is queued as its own separate pass, never mixed
                # into the main download for the same profile.
                effective = Job(username=name, settings=overrides or None).effective(self.settings)

                job = Job(username=name, kind="main", settings=overrides or None)
                self.jobs[job.id] = job
                self.pending.append(job.id)
                added.append(job)

                if effective.reels:
                    reels_job = Job(username=name, kind="reels", parent_id=job.id,
                                    settings={**(overrides or {}), "reels": True})
                    self.jobs[reels_job.id] = reels_job
                    self.pending.append(reels_job.id)
                    added.append(reels_job)

                active.add(name)
        self._kick()
        self.save_state()
        return added, rejected

    def remove(self, job_id: str) -> bool:
        """Remove a job. Running jobs are cancelled first."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False
            if job.status == "running":
                self.cancel_current = True
                self._terminate_proc()
                job.status = "cancelled"
                job.finished_at = time.time()
                job.message = "cancelled"
                self.current = None
            elif job.status in ("queued", "retrying"):
                job.status = "cancelled"
                job.finished_at = time.time()
                job.message = "removed"
            if job_id in self.pending:
                self.pending.remove(job_id)
        self.save_state()
        return True

    def forget(self, job_id: str) -> bool:
        """Drop a finished job from the list, archiving it so it can come back."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status in ("running", "queued", "retrying"):
                return False
            entry = job.to_dict()
            entry["removed_at"] = time.time()
            self.removed.append(entry)
            if len(self.removed) > 500:      # keep a bounded history
                del self.removed[:-500]
            self.jobs.pop(job_id, None)
            if job_id in self.pending:
                self.pending.remove(job_id)
        self.save_state()
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

    def purge_removed(self) -> int:
        """Empty the removed-jobs archive."""
        with self.lock:
            n = len(self.removed)
            self.removed = []
        self.save_state()
        return n

    def requeue(self, job_id: str, force_full: bool = False) -> bool:
        """Re-run a finished/failed/cancelled job. force_full disables fast-update."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status in ("running", "queued", "retrying"):
                return False
            job.status = "queued"
            job.error = ""
            job.message = "re-queued" + (" (full re-check)" if force_full else "")
            job.attempts = 0
            job.finished_at = None
            job.started_at = None
            if force_full:
                job.settings = {**(job.settings or {}), "fast_update": False}
            # Guard against a duplicate entry in the pending order.
            self.pending[:] = [x for x in self.pending if x != job_id]
            self.pending.append(job_id)
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
                    self.pending.append(jid)
                    n += 1
        if n:
            self._kick()
        self.save_state()
        return n

    def pause(self) -> None:
        self.paused = True
        self._limit_paused = False  # a user pause must never auto-clear

    def resume(self) -> None:
        """Resume the queue. Also clears any rate-limit cooldown the user
        is explicitly choosing to override."""
        self.paused = False
        self._limit_paused = False
        self.kick_if_work()

    def clear_cooldown(self) -> None:
        """Drop the rate-limit cooldown (user override)."""
        with self.lock:
            self.cooldown_until = 0.0
            self.consecutive_limits = 0
            self._limit_paused = False
        self.save_state()

    def cooldown_remaining(self) -> int:
        return max(0, int(self.cooldown_until - time.time()))

    def check_ip_block(self) -> bool:
        """One-shot check whether Instagram is refusing this IP (429)."""
        blocked = probe_ip_block()
        if blocked:
            with self.lock:
                self.cooldown_until = max(self.cooldown_until,
                                          time.time() + IP_BLOCK_COOLDOWN)
                self.paused = True
            self.save_state()
        return blocked

    def set_priority_high(self, job_id: str) -> bool:
        return self.move_top(job_id)

    # ---------- snapshot ----------

    def session_username(self) -> str | None:
        d = session_diagnosis(self.active_session)
        return d["username"] if d["valid"] else None

    def session_info(self) -> dict:
        return session_diagnosis(self.active_session)

    def list_sessions(self) -> list[dict]:
        return list_sessions()

    def browser_sessions(self) -> list[dict]:
        return find_browser_sessions()

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
                self.active_session = res["username"]
                # a fresh session clears any stale auth cooldown
                self.consecutive_limits = 0
            self.save_state()
            res["browser"] = str(target)
        return res

    def set_session(self, username: str) -> bool:
        """Switch the instaloader session used for all downloads."""
        names = {s["username"] for s in list_sessions()}
        if username not in names:
            return False
        with self.lock:
            self.active_session = username
        self.save_state()
        return True

    def forget_session(self, username: str) -> bool:
        """Delete a saved session file (used to clear a broken login)."""
        safe = clean_profile_name(username)
        if not safe:
            return False
        f = Path.home() / ".config" / "instaloader" / f"session-{safe}"
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

    def snapshot(self) -> dict:
        jobs = [j.to_dict() for j in self._order()]
        with self.lock:
            pending_count = len(self.pending)
        sess = self.session_info()
        return {
            "current": self.current,
            "paused": self.paused,
            "limit_paused": self._limit_paused,
            "busy": bool(self.worker and self.worker.is_alive() and (self.current or pending_count)),
            "queued": pending_count,
            "output_root": str(self.output_root),
            "instaloader": INSTALOADER,
            "session_present": sess["present"] and sess["valid"],
            "session_username": sess["username"] if sess["valid"] else None,
            "session_ok": sess["valid"],
            "session_problem": sess["problem"],
            "sessions": sess["sessions"],
            "active_session": self.active_session,
            "settings": self.settings.to_dict(),
            "cooldown_remaining": self.cooldown_remaining(),
            "consecutive_limits": self.consecutive_limits,
            "removed": list(reversed(self.removed))[-100:],
            "jobs": jobs,
            "counts": self._counts(),
        }

    def _counts(self) -> dict:
        with self.lock:
            c = {"queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
            for j in self.jobs.values():
                key = j.status if j.status in c else ("running" if j.status == "retrying" else None)
                if key:
                    c[key] += 1
            return c

    # ---------- worker ----------

    def _kick(self) -> None:
        self.start()

    def _terminate_proc(self) -> None:
        p = self._proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

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

    def _next_job(self) -> Job | None:
        """Return the next runnable job in queue order, leaving the order intact.

        Only removes the id from self.pending once the job is actually handed
        to the worker, so a job that is temporarily not runnable is never lost.
        """
        with self.lock:
            for jid in list(self.pending):
                job = self.jobs.get(jid)
                if job is None:
                    self.pending.remove(jid)  # forgotten job: drop the stale id
                    continue
                if job.status == "queued":
                    self.pending.remove(jid)
                    return job
            return None

    def _run(self) -> None:
        while not self.stop_requested:
            # Wait out both user-pause and any rate-limit cooldown BEFORE taking
            # a job. A 429 sets paused=True *and* cooldown_until, so the two
            # must be handled together or the worker sleeps forever in the
            # pause loop and never reaches the cooldown countdown.
            while not self.stop_requested:
                # A 429 pause clears itself when its cooldown elapses; a pause
                # the user asked for never does.
                if self.paused and self._limit_paused and self.cooldown_remaining() <= 0:
                    with self.lock:
                        self.paused = False
                        self._limit_paused = False
                    self.save_state()
                if self.paused:
                    self.save_state()  # keep the UI countdown fresh
                    time.sleep(1.0)
                    continue
                if self.cooldown_remaining() > 0 and self.pending:
                    self.save_state()
                    time.sleep(2.0)
                    continue
                break
            if self.stop_requested:
                break

            # A broken session fails every request with 401 and just burns
            # attempts. Stop and let the UI explain what to fix.
            sess = self.session_info()
            if not sess["valid"] and self.pending:
                self.save_state()
                return  # idle; the UI shows session_problem

            job = self._next_job()
            if job is None:
                # Nothing runnable in the order. Before giving up, re-sync the
                # order from jobs{}: a job can be "queued" yet missing from
                # pending (e.g. after a crash path or an interrupted edit).
                with self.lock:
                    stragglers = [jid for jid, j in self.jobs.items()
                                  if j.status == "queued" and jid not in self.pending]
                    for jid in stragglers:
                        self.pending.append(jid)
                if stragglers:
                    continue  # try again now that the order is repaired
                self.save_state()
                return  # genuinely idle; thread exits, busy=false

            with self.lock:
                self.current = job.username
                self.cancel_current = False
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
                    self.current = None
                    self.cancel_current = False
                self._set(job, last_run_seconds=round(time.time() - t0, 1))
                self.save_state()

            if self.stop_requested:
                break

            with self.lock:
                has_more = bool(self.pending)
            if has_more:
                delay = max(0, int(self.settings.between_profiles))
                for _ in range(delay):
                    if self.stop_requested or self.cancel_current:
                        break
                    time.sleep(1.0)

    def _process(self, job: Job) -> None:
        target_dir = self.output_root / job.username
        target_dir.mkdir(parents=True, exist_ok=True)
        session_user = self.session_username()
        cfg = job.effective(self.settings)

        while job.attempts < job.max_attempts and not self.stop_requested and not self.cancel_current:
            job.attempts += 1
            cmd = self._build_cmd(job, target_dir, session_user, cfg)
            self._log(job, f"$ {' '.join(cmd)}")
            self._set(job, message=f"attempt {job.attempts}/{job.max_attempts}")

            outcome = self._exec(job, cmd, target_dir)

            if self.cancel_current:
                self._set(job, status="cancelled", finished_at=time.time(), message="cancelled")
                return
            if outcome == "ok":
                with self.lock:
                    self.consecutive_limits = 0
                n_media = self._count_media(target_dir)
                if n_media == 0 and cfg.posts:
                    # Ran clean but grabbed no posts at all: not a success. Most
                    # often Instagram served a degraded response. Retry rather
                    # than record an empty archive as done.
                    self._set(job, status="failed", finished_at=time.time(),
                              message="no posts downloaded",
                              error="instaloader reported success but no posts were saved")
                    return
                self._set(job, status="done", finished_at=time.time(),
                          message="complete", error="")
                return
            if outcome == "fatal":
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
                    extra = {**(job.settings or {}), "stories": False, "highlights": False}
                    self._set(job, settings=extra, status="running", error="",
                              message="stories/highlights failed — retrying without them")
                    self._log(job, "! stories/highlights crashed instaloader; "
                                   "retrying this profile with them disabled")
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
            with self.lock:
                self.cooldown_until = max(self.cooldown_until, time.time() + backoff)
                self.consecutive_limits += 1
                tripped = self.consecutive_limits >= RATE_LIMIT_TRIP
                if tripped:
                    self.paused = True
                    self._limit_paused = True  # auto-clears when cooldown ends
            self._set(job, status="retrying",
                      message=f"rate limited — cooling down {max(1, round(backoff / 60))} min "
                              f"(attempt {job.attempts})")
            self.save_state()
            if tripped:
                # Stop hammering entirely; the user resumes when it's safe.
                self._set(job, status="queued", message="paused: rate limit")
                return
            waited = 0
            while waited < backoff and not self.stop_requested and not self.cancel_current:
                if not self.paused:
                    waited += 1
                time.sleep(1.0)
            self._set(job, status="running", retry_after=0)

        # Attempts exhausted. Without this terminal status the job is left
        # "queued" and silently restarts forever.
        if not self.cancel_current and job.status not in ("done", "failed", "cancelled"):
            self._set(job, status="failed", finished_at=time.time(),
                      message="gave up after retries",
                      error=job.error or "still rate limited after max attempts")

    def _build_cmd(self, job: Job, target_dir: Path, session_user: str | None,
                   s: Settings) -> list[str]:
        cmd = [INSTALOADER, f"--dirname-pattern={target_dir}"]

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
            if not s.profile_pic:
                cmd.append("--no-profile-pic")
        else:
            if not s.posts:
                cmd.append("--no-posts")
            if not s.profile_pic:
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
        retry_hit = False
        crash_hit = False
        crash_optional = False
        crash_line = ""
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

        self._proc = proc
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
                    except ValueError:
                        pass
                m = WAIT_HINT.search(line)
                if m:
                    try:
                        job.retry_after = max(job.retry_after, int(m.group(1)))
                    except ValueError:
                        pass
                if CRASH.search(line):
                    crash_hit = True
                    crash_line = line.strip()
                if CRASH_IN_OPTIONAL.search(line):
                    crash_optional = True
                if FATAL.search(line):
                    fatal_hit = True
                    last_err = line.strip()
                elif RETRYABLE.search(line):
                    retry_hit = True
                    last_err = line.strip()
        finally:
            proc.wait()
            self._proc = None
        self._parse_counts(job, target_dir)

        if self.cancel_current:
            return "ok"

        # A Python crash must never be mistaken for a rate limit: retrying the
        # same arguments would crash identically and falsely burn a cooldown.
        if crash_hit:
            self._set(job, error=crash_line or "instaloader crashed")
            return "crash_optional" if crash_optional else "crash"

        if proc.returncode == 0:
            return "fatal" if (fatal_hit and not retry_hit) else "ok"
        if fatal_hit and not retry_hit:
            self._set(job, error=last_err)
            return "fatal"
        if not retry_hit:
            # Non-zero exit with no recognised marker: treat as a crash, not a
            # rate limit, so we don't wait 15 minutes for nothing.
            self._set(job, error=last_err or f"exit code {proc.returncode}")
            return "crash"
        self._set(job, error=last_err or f"exit code {proc.returncode}")
        return "retryable"

    def _count_media(self, target_dir: Path) -> int:
        """Number of media files saved for a profile (excludes metadata/json)."""
        exts = (".jpg", ".jpeg", ".png", ".mp4", ".webp", ".heic")
        n = 0
        try:
            for p in target_dir.rglob("*"):
                if p.is_file() and p.suffix.lower() in exts and "_profile_pic" not in p.name:
                    n += 1
        except OSError:
            pass
        return n

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
