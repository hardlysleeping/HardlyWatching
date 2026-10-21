"""
Local SQLite record of every profile the app knows about, per platform.

Why this exists: re-pasting a following list should cost nothing. Store what was
imported, and a second import only reports what is new. It also survives cache
wipes, queue clears and restarts, so "who did I already grab" stays answerable.

Deliberately separate from gallery-cache.db: that caches filesystem scans and is
safe to delete at any time. This is a record of intent, so it lives in its own
file next to state.json.
"""

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    platform     TEXT NOT NULL,
    handle       TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    source       TEXT DEFAULT '',      -- following | manual | ig-paste
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    last_imported REAL DEFAULT 0,      -- last time it was pushed into the queue
    PRIMARY KEY (platform, handle)
);
CREATE INDEX IF NOT EXISTS idx_profiles_seen ON profiles(platform, last_seen DESC);

-- Per-clip state for the shorts feed. Kept separate from the files on disk so a
-- re-download of the same video inherits its weight instead of resetting it.
CREATE TABLE IF NOT EXISTS clips (
    path        TEXT PRIMARY KEY,   -- relative to the archive root
    profile     TEXT NOT NULL,
    platform    TEXT NOT NULL,
    clip_id     TEXT NOT NULL,      -- yt-dlp id / instagram shortcode
    weight      REAL NOT NULL DEFAULT 1.0,   -- how often it reappears
    shown       INTEGER NOT NULL DEFAULT 0,
    liked       INTEGER NOT NULL DEFAULT 0,
    trashed     INTEGER NOT NULL DEFAULT 0,
    last_shown  REAL DEFAULT 0,
    first_seen  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clips_weight ON clips(weight DESC, last_shown ASC);

-- How often a whole PROFILE reappears in the shorts feed. "Show more/less
-- often" is about the person, not the individual clip: one video lands well
-- and you want to see more from them, so the setting lives per profile and
-- multiplies each of their clips' weights. A separate table (rather than a
-- column on profiles) so it works for a profile that exists only on disk and
-- has no imported row.
CREATE TABLE IF NOT EXISTS profile_weights (
    platform TEXT NOT NULL,
    profile  TEXT NOT NULL,
    weight   REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (platform, profile)
);
"""


def _key(handle: str) -> str:
    """Handles are case-insensitive on both platforms."""
    return handle.strip().lower()


class ProfileDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The HTTP server is threaded and shares one ProfileDB, so the
        # connection must tolerate cross-thread use and every statement has to
        # go through a lock rather than relying on sqlite's own guarding.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            self._migrate()

    def close(self):
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass

    # Columns added after the first release. These cannot go in SCHEMA because
    # CREATE TABLE IF NOT EXISTS is a no-op on a database that already exists,
    # so an upgrade has to add them explicitly or the queries below break.
    _MIGRATIONS = [
        ("profiles", "state", "TEXT NOT NULL DEFAULT 'active'"),
        ("profiles", "reason", "TEXT DEFAULT ''"),
        ("profiles", "excluded_at", "REAL DEFAULT 0"),
        # 1 = a reel, 0 = an Instagram video that is not a reel, -1 = not known
        # (no sidecar). Stored rather than re-read on every feed request.
        ("clips", "is_reel", "INTEGER NOT NULL DEFAULT -1"),
        # A favourite, kept apart from `liked` on purpose. A like is a reaction --
        # it should stay a light, throwaway tap. A star is a bookmark: the thing
        # you actually want to find again -- so they are separate columns and one
        # never implies the other.
        ("clips", "starred", "INTEGER NOT NULL DEFAULT 0"),
    ]

    def _migrate(self):
        for table, column, decl in self._MIGRATIONS:
            cols = {r["name"] for r in
                    self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_profiles_state ON profiles(platform, state)")
        # Measured video dimensions, so a portrait upload can be recognised as a
        # reel. Probing with ffprobe costs ~140ms a file, which is far too much
        # to pay on every feed request, so the answer is stored against the
        # file's size and mtime and only re-measured when the file changes.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS clip_dims (
                   path   TEXT PRIMARY KEY,
                   size   INTEGER NOT NULL DEFAULT 0,
                   mtime  REAL    NOT NULL DEFAULT 0,
                   width  INTEGER NOT NULL DEFAULT 0,
                   height INTEGER NOT NULL DEFAULT 0
               )""")
        self._conn.commit()

    def record(self, platform: str, handle: str, display: str = '', source: str = '') -> bool:
        with self._lock:
            """Insert or touch a profile. Returns True if it was new.

            Uses the lowercased handle for matching so @Name and @name are one row,
            while keeping the spelling that was first seen for display.
            """
            h = handle.strip()
            if not h:
                return False
            now = time.time()
            cur = self._conn.execute(
                "SELECT handle FROM profiles WHERE platform=? AND lower(handle)=?",
                (platform, _key(h)),
            )
            row = cur.fetchone()
            if row:
                self._conn.execute(
                    "UPDATE profiles SET last_seen=? WHERE platform=? AND lower(handle)=?",
                    (now, platform, _key(h)),
                )
                self._conn.commit()
                return False
            self._conn.execute(
                "INSERT INTO profiles (platform, handle, display_name, source, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?)",
                (platform, h, display.strip(), source, now, now),
            )
            self._conn.commit()
            return True


    def mark_imported(self, platform: str, handles: list[str]):
        with self._lock:
            now = time.time()
            for h in handles:
                self._conn.execute(
                    "UPDATE profiles SET last_seen=?, last_imported=? WHERE platform=? AND lower(handle)=?",
                    (now, now, platform, _key(h)),
                )
            self._conn.commit()


    def known(self, platform: str) -> set[str]:
        with self._lock:
            cur = self._conn.execute("SELECT handle FROM profiles WHERE platform=?", (platform,))
            return {r['handle'].lower() for r in cur}

    # ---- exclusions ------------------------------------------------------
    # A profile the user removed or deleted is remembered as excluded so that
    # re-pasting a following list never quietly puts it back. Restoring is
    # always possible: this is a memory, not a blacklist.

    def exclude(self, platform: str, handle: str, reason: str) -> bool:
        """Mark a profile excluded. Returns True if anything changed."""
        h = handle.strip()
        if not h:
            return False
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "SELECT state FROM profiles WHERE platform=? AND lower(handle)=?",
                (platform, _key(h)))
            row = cur.fetchone()
            if row is None:
                # Never seen before: record it excluded so a future import
                # skips it rather than adding it.
                self._conn.execute(
                    "INSERT INTO profiles (platform, handle, source, first_seen, "
                    "last_seen, state, reason, excluded_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (platform, h, 'excluded', now, now, 'excluded', reason, now))
                self._conn.commit()
                return True
            if row['state'] == 'excluded':
                return False
            self._conn.execute(
                "UPDATE profiles SET state='excluded', reason=?, excluded_at=? "
                "WHERE platform=? AND lower(handle)=?",
                (reason, now, platform, _key(h)))
            self._conn.commit()
            return True

    def restore(self, platform: str, handle: str) -> bool:
        """Undo an exclusion. Does not queue anything by itself."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT state FROM profiles WHERE platform=? AND lower(handle)=?",
                (platform, _key(handle)))
            row = cur.fetchone()
            if row is None or row['state'] != 'excluded':
                return False
            self._conn.execute(
                "UPDATE profiles SET state='active', reason='', excluded_at=0 "
                "WHERE platform=? AND lower(handle)=?",
                (platform, _key(handle)))
            self._conn.commit()
            return True

    def excluded(self, platform: str) -> set[str]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT handle FROM profiles WHERE platform=? AND state='excluded'",
                (platform,))
            return {r['handle'].lower() for r in cur}

    def excluded_list(self, platform: str, limit: int = 500) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM profiles WHERE platform=? AND state='excluded' "
                "ORDER BY excluded_at DESC LIMIT ?", (platform, limit))
            return [dict(r) for r in cur]

    def excluded_count(self, platform: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) c FROM profiles WHERE platform=? AND state='excluded'",
                (platform,))
            return cur.fetchone()['c']


    def count(self, platform: str | None = None) -> int:
        with self._lock:
            if platform:
                cur = self._conn.execute("SELECT COUNT(*) c FROM profiles WHERE platform=?", (platform,))
            else:
                cur = self._conn.execute("SELECT COUNT(*) c FROM profiles")
            return cur.fetchone()['c']


    def recent(self, platform: str | None = None, limit: int = 50) -> list[dict]:
        with self._lock:
            if platform:
                cur = self._conn.execute(
                    "SELECT * FROM profiles WHERE platform=? ORDER BY last_seen DESC LIMIT ?",
                    (platform, limit),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM profiles ORDER BY last_seen DESC LIMIT ?", (limit,)
                )
            return [dict(r) for r in cur]

    # ---- shorts feed -----------------------------------------------------
    # Weights are the whole point: the feed is random with repetition, and
    # "show more" / "show less" nudges how often a clip comes back rather than
    # hiding it. Bounded so it can never become always or never by accident.
    WEIGHT_MIN = 0.1
    # How strongly the feed compensates for a profile having few clips.
    #
    # A profile's share of the feed is proportional to how many clips it has, so
    # the biggest archives dominated: of 484 profiles here, the top ten took
    # 48.7% of the feed while the 378 profiles with five clips or fewer took
    # 8.6% between them. A profile with 500 clips came up 500x as often as one
    # with a single clip, which is not a judgement about the person, just about
    # how many files they happen to have.
    #
    # Scaling each clip by 1 / count**this makes a profile's share proportional
    # to count**(1 - this):
    #   0.0 -> off; the biggest archive wins (what this did before)
    #   0.5 -> measured: the small profiles go from 8.6% of the feed to 42%, and
    #          the top ten from 48.7% to 15.8%; the 500-vs-1 gap becomes 22x
    #   0.75 -> 62.9% and 6.2%; 1.0 -> every profile gets an equal share
    # Deliberately not 1.0: a profile with one video should be seen, not given
    # the same airtime as one with two thousand.
    ALGO_CLIP_BALANCE = 0.5
    WEIGHT_MAX = 5.0
    WEIGHT_STEP = 1.6

    def sync_clips(self, entries: list[dict]) -> int:
        """Record clips found on disk. Returns how many were new."""
        now = time.time()
        new = 0
        with self._lock:
            for e in entries:
                path = e.get("path")
                if not path:
                    continue
                # None means "no sidecar to read", which is not the same as
                # "not a reel" -- store -1 so a later run with the file present
                # can fill it in rather than freezing a wrong answer.
                flag = e.get("is_reel")
                reel = -1 if flag is None else (1 if flag else 0)
                cur = self._conn.execute("SELECT path FROM clips WHERE path=?", (path,))
                if cur.fetchone():
                    self._conn.execute(
                        "UPDATE clips SET profile=?, clip_id=?, is_reel=? WHERE path=?",
                        (e.get("profile", ""), e.get("clip_id", ""), reel, path),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO clips (path, profile, platform, clip_id, is_reel, first_seen) "
                        "VALUES (?,?,?,?,?,?)",
                        (path, e.get("profile", ""), e.get("platform", "tiktok"),
                         e.get("clip_id", ""), reel, now),
                    )
                    new += 1
            self._conn.commit()
        return new

    def feed(self, limit: int = 20, favourites_only: bool = False,
             exclude: list[str] | None = None, known_paths: list[str] | None = None,
             profile: str | None = None, platform: str | None = None,
             starred_only: bool = False) -> list[dict]:
        """Pick the next clips, weighted, newest-ignored-first.

        Weighted choice is done here rather than in SQL: the set is small enough
        to fetch and sampling in Python keeps the weight logic readable and
        testable. Clips not seen for longer are nudged up so the feed rotates
        instead of looping the same few.
        """
        import random
        with self._lock:
            # The feed is for short-form video only: every TikTok clip, and
            # Instagram only where the sidecar marked the post as a reel.
            # Unknown (-1) is excluded rather than guessed at, because showing
            # an ordinary video in a reels feed is the mistake worth avoiding.
            # Parenthesised: without it AND binds tighter than OR, and the
            # favourites filter would apply to the Instagram branch only.
            sql = ("SELECT * FROM clips WHERE trashed=0 AND ("
                   "platform='tiktok' OR (platform='instagram' AND is_reel=1))")
            params: list = []
            # One profile only, when the feed is opened from someone's gallery.
            # Matched case-insensitively: a handle is not case-sensitive on either
            # site, and the folder name may not match the way it is typed.
            if profile:
                sql += " AND lower(profile)=?"
                params.append(profile.strip().lower())
            if platform:
                sql += " AND platform=?"
                params.append(platform.strip().lower())
            # `favourites_only` was the old name for this filter and meant likes.
            # Either one now means starred: the star is the bookmark, the heart is
            # only a reaction.
            sql += " AND starred=1" if (starred_only or favourites_only) else ""
            cur = self._conn.execute(sql, params)
            rows = [dict(r) for r in cur]
        if known_paths:
            alive = set(known_paths)
            rows = [r for r in rows if r["path"] in alive]
        if exclude:
            skip = set(exclude)
            rows = [r for r in rows if r["path"] not in skip]
        if not rows:
            return []

        # A profile-level "show more/less" multiplies every clip from that
        # profile, so the same setting applies whether you hit it on one of
        # their videos or another.
        pw = self._profile_weights()

        # How many clips each profile has, counted over everything the feed
        # could show rather than over what is left after `exclude`. Counting the
        # remainder would make a profile's weight shift as you swiped through it,
        # so its clips would get easier to hit exactly when they were running
        # out -- the opposite of what the balance is for.
        counts: dict[tuple, int] = {}
        for r in rows:
            key = (r["platform"], (r["profile"] or "").lower())
            counts[key] = counts.get(key, 0) + 1

        now = time.time()
        weights = []
        for r in rows:
            key = (r["platform"], (r["profile"] or "").lower())
            w = max(self.WEIGHT_MIN, float(r["weight"] or 1.0))
            w *= pw.get(key, 1.0)
            # Fewer clips, more of the feed. See ALGO_CLIP_BALANCE.
            w *= counts.get(key, 1) ** -self.ALGO_CLIP_BALANCE
            # Staleness nudge: half a weight per hour unseen, capped, so a
            # high-weight clip still reappears more often but does not dominate.
            unseen_h = min(24.0, max(0.0, (now - (r["last_shown"] or 0)) / 3600.0))
            weights.append(w * (1.0 + 0.5 * unseen_h))

        picked = []
        pool = list(zip(rows, weights))
        for _ in range(min(limit, len(pool))):
            total = sum(w for _, w in pool)
            if total <= 0:
                break
            x = random.uniform(0, total)
            acc = 0.0
            for i, (row, w) in enumerate(pool):
                acc += w
                if acc >= x:
                    picked.append(row)
                    pool.pop(i)
                    break
        return picked

    def mark_shown(self, paths: list[str]):
        now = time.time()
        with self._lock:
            for pth in paths:
                self._conn.execute(
                    "UPDATE clips SET shown=shown+1, last_shown=? WHERE path=?", (now, pth))
            self._conn.commit()


    def dims_get(self, rel: str) -> tuple[tuple[int, int], int, float] | None:
        """Cached (width, height, size, mtime) for one clip, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT width, height, size, mtime FROM clip_dims WHERE path=?",
                (rel,)).fetchone()
        if not row:
            return None
        return (row["width"], row["height"]), row["size"], row["mtime"]

    def dims_put(self, rel: str, size: int, mtime: float,
                 width: int, height: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO clip_dims (path, size, mtime, width, height)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                       size=excluded.size, mtime=excluded.mtime,
                       width=excluded.width, height=excluded.height""",
                (rel, int(size), float(mtime), int(width), int(height)))
            self._conn.commit()

    def known_reels(self) -> dict[str, bool]:
        """{path: is_reel} for every clip already classified (0 or 1).

        The reel flag is read once from the sidecar and never changes, so the
        feed can skip re-reading thousands of sidecar files on every request.
        Clips still unknown (-1) are left out and re-checked, in case a sidecar
        appears after the video (instaloader writes them together, but a file
        copied in by hand has neither).
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT path, is_reel FROM clips WHERE is_reel IN (0, 1)")
            return {r["path"]: bool(r["is_reel"]) for r in cur}

    def set_weight(self, path: str, direction: str) -> float:
        """direction: 'more' or 'less'. Returns the new weight."""
        with self._lock:
            cur = self._conn.execute("SELECT weight FROM clips WHERE path=?", (path,))
            row = cur.fetchone()
            if not row:
                return 0.0
            w = float(row["weight"] or 1.0)
            w = w * self.WEIGHT_STEP if direction == "more" else w / self.WEIGHT_STEP
            w = max(self.WEIGHT_MIN, min(self.WEIGHT_MAX, w))
            self._conn.execute("UPDATE clips SET weight=? WHERE path=?", (w, path))
            self._conn.commit()
            return round(w, 3)

    def _profile_weights(self) -> dict[tuple, float]:
        """{(platform, profile_lower): weight} for every profile that has one.

        Returned under the lock as a plain dict so the sampler can read it
        without holding the connection.
        """
        with self._lock:
            cur = self._conn.execute("SELECT platform, profile, weight FROM profile_weights")
            return {(r["platform"], (r["profile"] or "").lower()): float(r["weight"] or 1.0)
                    for r in cur}

    def profile_weight(self, platform: str, profile: str) -> float:
        with self._lock:
            cur = self._conn.execute(
                "SELECT weight FROM profile_weights WHERE platform=? AND profile=?",
                (platform, (profile or "").lower()))
            row = cur.fetchone()
            return round(float(row["weight"]), 3) if row else 1.0

    def set_profile_weight(self, platform: str, profile: str, direction: str) -> float:
        """Nudge how often a WHOLE profile appears. direction 'more'|'less'."""
        key = (profile or "").strip().lower()
        if not key:
            return 1.0
        with self._lock:
            cur = self._conn.execute(
                "SELECT weight FROM profile_weights WHERE platform=? AND profile=?",
                (platform, key))
            row = cur.fetchone()
            w = float(row["weight"]) if row else 1.0
            w = w * self.WEIGHT_STEP if direction == "more" else w / self.WEIGHT_STEP
            w = max(self.WEIGHT_MIN, min(self.WEIGHT_MAX, w))
            self._conn.execute(
                "INSERT INTO profile_weights (platform, profile, weight) VALUES (?,?,?) "
                "ON CONFLICT(platform, profile) DO UPDATE SET weight=excluded.weight",
                (platform, key, w))
            self._conn.commit()
            return round(w, 3)

    def set_liked(self, path: str, liked: bool):
        with self._lock:
            self._conn.execute("UPDATE clips SET liked=? WHERE path=?",
                               (1 if liked else 0, path))
            self._conn.commit()

    def set_starred(self, path: str, starred: bool = True):
        with self._lock:
            self._conn.execute("UPDATE clips SET starred=? WHERE path=?",
                               (1 if starred else 0, path))
            self._conn.commit()

    def set_trashed(self, path: str, trashed: bool = True):
        with self._lock:
            self._conn.execute("UPDATE clips SET trashed=? WHERE path=?",
                               (1 if trashed else 0, path))
            self._conn.commit()

    def clip(self, path: str) -> dict | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM clips WHERE path=?", (path,))
            row = cur.fetchone()
            return dict(row) if row else None

    def clip_stats(self) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) total, SUM(liked) liked, SUM(trashed) trashed, "
                "SUM(starred) starred FROM clips")
            r = cur.fetchone()
            return {"total": r["total"] or 0, "liked": r["liked"] or 0,
                    "trashed": r["trashed"] or 0, "starred": r["starred"] or 0}


_shared = None


def _shared_db() -> "ProfileDB":
    """One ProfileDB for the whole process.

    The engine and the HTTP layer both need it, and a second instance would
    mean two connections to the same file -- wasteful, and it hides the
    sharing from anyone reading either module.
    """
    global _shared
    if _shared is None:
        from engine import config_dir
        _shared = ProfileDB(config_dir() / "hardlywatching" / "profiles.db")
    return _shared
