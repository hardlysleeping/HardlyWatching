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
        # When it was liked. `first_seen` is a sync stamp, so every row in one
        # pass carries the same value and ordering a list of likes by it means
        # nothing. Zero means "liked before this column existed"; the liked view
        # falls back to `first_seen` for those rows.
        #
        # There used to be a second, higher-level flag here (`starred`, with its
        # own `starred_at`). There is one heart now -- see _migrate(), which
        # copies the old flag into this one and clears it.
        ("clips", "liked_at", "REAL NOT NULL DEFAULT 0"),
        # Which site the parent lives on. Blank means "the same one as the alias",
        # which is what every within-platform merge means.
        ("profile_links", "parent_platform", "TEXT NOT NULL DEFAULT ''"),
        # 1 when the same-name rule made the link, 0 when it was asked for. A
        # merge the user made by hand outranks anything inferred.
        ("profile_links", "auto", "INTEGER NOT NULL DEFAULT 0"),
    ]

    def _create_tables(self):
        # Accounts that are the same person: a main and its burner/finsta, or the
        # same handle on both sites. Gallery view only -- the archives stay
        # separate folders and the downloads stay separate jobs. This decides only
        # that one card stands for more than one account.
        #
        # `platform` is where the alias lives, `parent_platform` where it shows:
        # blank means the same site, which is what a burner merge is. They are
        # separate columns because a burner merge must not merge the other site
        # (the same handle is a different person there) while the same-name rule
        # does exactly the opposite.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS profile_links (
                   platform        TEXT NOT NULL,
                   alias           TEXT NOT NULL,
                   parent          TEXT NOT NULL,
                   parent_platform TEXT NOT NULL DEFAULT '',
                   auto            INTEGER NOT NULL DEFAULT 0,
                   created         REAL NOT NULL,
                   PRIMARY KEY (platform, alias))""")
        # Accounts told apart by hand that the same-name rule would put back
        # together. A split is an instruction, and an automatic rule that silently
        # undoes an instruction is worse than no rule.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS profile_splits (
                   handle  TEXT PRIMARY KEY,
                   created REAL NOT NULL)""")
        self._conn.commit()

    def _migrate(self):
        # Tables first, then columns: adding a column to a table that has not been
        # created yet fails on a fresh database, which is exactly what happens the
        # first time this runs somewhere new.
        self._create_tables()
        for table, column, decl in self._MIGRATIONS:
            cols = {r["name"] for r in
                    self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        # One heart, not two. `starred` was a second flag on the same clip --
        # "the one you want to find again" -- and having two controls that both
        # meant "keep this" is what made the two words (favourites, starred) for
        # one idea in the first place. Everything starred is a like now.
        #
        # The column is left in place rather than dropped: an older daemon or a
        # tab left open mid-reload may still read it, and nothing writes to it
        # after this. It is cleared as it is copied, so a clip the user unlikes
        # later is not dragged back to liked on the next start.
        clip_cols = {r["name"] for r in
                     self._conn.execute("PRAGMA table_info(clips)")}
        if "starred" in clip_cols:
            self._conn.execute("UPDATE clips SET liked=1 WHERE starred=1")
            if "starred_at" in clip_cols:
                # Keep the moment it was starred: it is the better date for a
                # clip that was starred before it was ever liked.
                self._conn.execute(
                    "UPDATE clips SET liked_at=starred_at "
                    "WHERE starred=1 AND starred_at>0 AND liked_at=0")
            self._conn.execute("UPDATE clips SET starred=0 WHERE starred=1")
        # Likes made before liked_at existed: order them by when they were seen.
        self._conn.execute(
            "UPDATE clips SET liked_at=first_seen WHERE liked=1 AND liked_at=0")
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

    def feed(self, limit: int = 20, liked_only: bool = False,
             exclude: list[str] | None = None, known_paths: list[str] | None = None,
             profile: str | None = None, platform: str | None = None,
             favourites_only: bool = False) -> list[dict]:
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
            # One heart. `favourites_only` and `starred_only` are the old names
            # callers still send; all three mean the same filter now.
            sql += " AND liked=1" if (liked_only or favourites_only) else ""
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

    def profile_weights_list(self, limit: int = 400) -> list[dict]:
        """Every profile the sampler has been told to favour or damp.

        The weights live in this table, not in the browser, so the tuning sheet
        can show exactly what the feed is working from -- and so a reload, a new
        phone or a restart does not quietly start again from nothing.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT w.platform, w.profile, w.weight,"
                " (SELECT COUNT(*) FROM clips c WHERE c.platform=w.platform"
                "    AND lower(c.profile)=w.profile AND c.trashed=0) AS clips"
                " FROM profile_weights w"
                " ORDER BY w.weight DESC, w.profile ASC LIMIT ?", (int(limit),))
            return [{"platform": r["platform"], "profile": r["profile"],
                     "weight": round(float(r["weight"]), 3),
                     "clips": int(r["clips"] or 0)} for r in cur]

    def set_profile_weight_value(self, platform: str, profile: str,
                                 weight: float) -> float:
        """Set a profile's weight outright. 1.0 means 'no opinion' and drops the row."""
        key = (profile or "").strip().lower()
        if not key:
            return 1.0
        w = max(self.WEIGHT_MIN, min(self.WEIGHT_MAX, float(weight)))
        with self._lock:
            if abs(w - 1.0) < 1e-9:
                self._conn.execute(
                    "DELETE FROM profile_weights WHERE platform=? AND profile=?",
                    (platform, key))
            else:
                self._conn.execute(
                    "INSERT INTO profile_weights (platform, profile, weight) VALUES (?,?,?) "
                    "ON CONFLICT(platform, profile) DO UPDATE SET weight=excluded.weight",
                    (platform, key, w))
            self._conn.commit()
        return round(w, 3)

    def reset_profile_weights(self) -> int:
        """Forget every favour/damp. Returns how many rows went."""
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) n FROM profile_weights")
            n = int((cur.fetchone() or {"n": 0})["n"] or 0)
            self._conn.execute("DELETE FROM profile_weights")
            self._conn.commit()
        return n

    def set_liked(self, path: str, liked: bool):
        # The timestamp is what the liked view sorts by; first_seen is a sync
        # stamp and is the same for a whole pass, so it cannot order anything.
        with self._lock:
            self._conn.execute(
                "UPDATE clips SET liked=?, liked_at=? WHERE path=?",
                (1 if liked else 0, time.time() if liked else 0.0, path))
            self._conn.commit()

    def set_trashed(self, path: str, trashed: bool = True):
        with self._lock:
            self._conn.execute("UPDATE clips SET trashed=? WHERE path=?",
                               (1 if trashed else 0, path))
            self._conn.commit()

    def profile_links(self) -> dict:
        """{(platform, alias_lower): (parent, parent_platform, auto)} per merge.

        A blank `parent_platform` means the parent lives on the alias's own site,
        which is what a burner merge is. `auto` says whether the same-name rule
        made it, which is what a split needs to know how to undo.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT platform, alias, parent, parent_platform, auto "
                "FROM profile_links")
            return {(r["platform"], r["alias"].lower()):
                    (r["parent"], r["parent_platform"] or r["platform"],
                     r["auto"] or 0) for r in cur}

    def profile_splits(self) -> set:
        """Handles told apart by hand. The same-name rule must leave these alone."""
        with self._lock:
            cur = self._conn.execute("SELECT handle FROM profile_splits")
            return {r["handle"].lower() for r in cur}

    def linked_family(self, username: str, platform: str = "instagram"
                      ) -> list[tuple[str, str]]:
        """[(handle, platform)] the merge table puts under one entry.

        This half is what was asked for by hand. The same-name rule needs to know
        what is actually on disk, which is not this table's business, so the
        caller adds that half.
        """
        plat = (platform or "instagram").strip().lower()
        me = (username or "").strip()
        if not me:
            return []
        links = self.profile_links()
        # Walk to the top of the chain. link_profiles() refuses to build one, but a
        # link added either side of a later merge could leave a stale step, and a
        # loop here would hang the gallery rather than show it wrong.
        parent, parent_plat, _auto = links.get((plat, me.lower()), (me, plat, 0))
        seen = {me.lower()}
        for _ in range(8):
            if parent.lower() in seen:
                break
            seen.add(parent.lower())
            nxt = links.get((parent_plat, parent.lower()))
            if not nxt:
                break
            parent, parent_plat = nxt[0], nxt[1]
        out = [(parent, parent_plat)]
        for (p_plat, alias), (target, target_plat, _a) in sorted(links.items()):
            if target.lower() == parent.lower() and target_plat == parent_plat:
                if alias.lower() != parent.lower():
                    out.append((alias, p_plat))
        # Keep the casing the caller passed for the entry itself; folders may be
        # spelled differently and callers match case-insensitively.
        if out[0][0].lower() == me.lower():
            out[0] = (me, out[0][1])
        return out

    def clip_path(self, profile: str, file: str, platform: str = "instagram") -> str | None:
        """The clips row that stands for a gallery file, or None.

        A like is stored against the clip, and the gallery works in
        profile+file terms, so this is the join between them. The two platforms
        store the path differently -- TikTok's carries a leading `tiktok/` -- so
        the file name is matched on the end of the path rather than the whole of
        it. None means the file is not a clip at all: a photo, not a video.
        """
        who = (profile or "").strip().lower()
        name = (file or "").strip()
        if not who or not name:
            return None
        with self._lock:
            cur = self._conn.execute(
                "SELECT path FROM clips WHERE lower(profile)=? AND platform=? "
                "AND (path=? OR path LIKE ?) LIMIT 1",
                (who, (platform or "instagram").lower(), name, "%/" + name))
            row = cur.fetchone()
            return row["path"] if row else None

    def liked_clips(self) -> list[dict]:
        """Every liked clip, newest like first. Backs the gallery's Liked view.

        A like lives on the clip, not in a folder, so this is the only list of
        the ones made in Shorts: a clip is not moved anywhere when it is liked.
        Trashed clips are left out -- they are still liked, but they are in the bin.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT path, profile, platform, first_seen, liked_at FROM clips "
                "WHERE liked=1 AND trashed=0 "
                "ORDER BY liked_at DESC, first_seen DESC")
            return [dict(r) for r in cur]

    def split_profile(self, username: str) -> dict:
        """Undo a merge, and stop the same-name rule putting it back.

        Both halves matter: dropping the link separates them now, remembering the
        handle keeps them apart later. An automatic rule that silently re-forms a
        split the user made is worse than having no rule.
        """
        me = (username or "").strip()
        if not me:
            return {"ok": False, "error": "an account is needed"}
        low = me.lower()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM profile_links WHERE lower(alias)=? OR lower(parent)=?",
                (low, low))
            self._conn.execute(
                "INSERT INTO profile_splits (handle, created) VALUES (?,?) "
                "ON CONFLICT(handle) DO UPDATE SET created=excluded.created",
                (low, time.time()))
            self._conn.commit()
        return {"ok": True, "profile": me, "unlinked": cur.rowcount}

    def link_profiles(self, alias: str, primary: str, platform: str,
                      parent_platform: str = "", auto: bool = False) -> dict:
        """Make `alias` show as part of `primary` in the gallery.

        Refuses the ways this can contradict itself: merging something into
        itself, and chains (A into B where B is already an alias of C), which would
        make what-is-shown depend on the order the links were read.
        """
        platform = (platform or "instagram").strip().lower()
        parent_platform = (parent_platform or platform).strip().lower()
        if parent_platform not in ("instagram", "tiktok"):
            return {"ok": False, "error": "unknown platform"}
        alias, primary = (alias or "").strip(), (primary or "").strip()
        if not alias or not primary:
            return {"ok": False, "error": "both accounts are needed"}
        if alias.lower() == primary.lower():
            return {"ok": False, "error": "that is the same account"}
        links = self.profile_links()
        step = links.get((platform, primary.lower()))
        if step:
            # Point at the top of the chain instead of building one.
            primary, parent_platform = step[0], step[1]
        with self._lock:
            self._conn.execute(
                "INSERT INTO profile_links "
                "(platform, alias, parent, parent_platform, auto, created) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(platform, alias) DO UPDATE SET "
                "parent=excluded.parent, parent_platform=excluded.parent_platform, "
                "auto=excluded.auto",
                (platform, alias.lower(), primary, parent_platform,
                 1 if auto else 0, time.time()))
            # Anything merged into `alias` now belongs to `primary`.
            self._conn.execute(
                "UPDATE profile_links SET parent=?, parent_platform=? "
                "WHERE platform=? AND lower(parent)=?",
                (primary, parent_platform, platform, alias.lower()))
            # Merging by hand is a later instruction than a split, so it clears the
            # note that said to keep these two apart.
            self._conn.execute(
                "DELETE FROM profile_splits WHERE handle IN (?,?)",
                (alias.lower(), primary.lower()))
            self._conn.commit()
        return {"ok": True, "alias": alias, "primary": primary, "platform": platform,
                "parent_platform": parent_platform}

    def unlink_profile(self, alias: str, platform: str) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM profile_links WHERE platform=? AND lower(alias)=?",
                ((platform or "instagram").strip().lower(), (alias or "").strip().lower()))
            self._conn.commit()
        return {"ok": bool(cur.rowcount), "alias": alias}

    def clip(self, path: str) -> dict | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM clips WHERE path=?", (path,))
            row = cur.fetchone()
            return dict(row) if row else None

    def clip_stats(self) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) total, SUM(liked) liked, SUM(trashed) trashed "
                "FROM clips")
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
