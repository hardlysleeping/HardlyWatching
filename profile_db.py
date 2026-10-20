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
                cur = self._conn.execute("SELECT path FROM clips WHERE path=?", (path,))
                if cur.fetchone():
                    self._conn.execute(
                        "UPDATE clips SET profile=?, clip_id=? WHERE path=?",
                        (e.get("profile", ""), e.get("clip_id", ""), path),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO clips (path, profile, platform, clip_id, first_seen) "
                        "VALUES (?,?,?,?,?)",
                        (path, e.get("profile", ""), e.get("platform", "tiktok"),
                         e.get("clip_id", ""), now),
                    )
                    new += 1
            self._conn.commit()
        return new

    def feed(self, limit: int = 20, favourites_only: bool = False,
             exclude: list[str] | None = None, known_paths: list[str] | None = None) -> list[dict]:
        """Pick the next clips, weighted, newest-ignored-first.

        Weighted choice is done here rather than in SQL: the set is small enough
        to fetch and sampling in Python keeps the weight logic readable and
        testable. Clips not seen for longer are nudged up so the feed rotates
        instead of looping the same few.
        """
        import random
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM clips WHERE trashed=0" +
                (" AND liked=1" if favourites_only else "")
            )
            rows = [dict(r) for r in cur]
        if known_paths:
            alive = set(known_paths)
            rows = [r for r in rows if r["path"] in alive]
        if exclude:
            skip = set(exclude)
            rows = [r for r in rows if r["path"] not in skip]
        if not rows:
            return []

        now = time.time()
        weights = []
        for r in rows:
            w = max(self.WEIGHT_MIN, float(r["weight"] or 1.0))
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

    def set_liked(self, path: str, liked: bool):
        with self._lock:
            self._conn.execute("UPDATE clips SET liked=? WHERE path=?",
                               (1 if liked else 0, path))
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
                "SELECT COUNT(*) total, SUM(liked) liked, SUM(trashed) trashed FROM clips")
            r = cur.fetchone()
            return {"total": r["total"] or 0, "liked": r["liked"] or 0,
                    "trashed": r["trashed"] or 0}


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
