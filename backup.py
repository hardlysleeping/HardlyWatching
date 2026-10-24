"""Backup and restore: the two files that are not the archive.

The archive is just folders -- point the app at them and the gallery and the
shorts feed rebuild themselves. What does *not* come back that way is everything
the app remembers *about* those folders:

    state.json    the queue and its order, the download settings, hidden and
                  excluded items, delete tombstones, the archive-folder list,
                  pause/cooldown state
    profiles.db   the people you imported or queued (including ones never
                  downloaded), bookmarks and their categories, likes, shorts
                  weights and shown history, link/split aliases

gallery-cache.db is deliberately not in here: it is a folder-listing cache keyed
by mtime/size/inode, it rebuilds itself, and copying it across machines would only
carry stale rows.

Why a zip and not "put the files in ~/.config": that path is per-OS, so an
instruction like that cannot be followed on Windows. Here the *app* resolves where
its files go on the machine it is running on, and the only thing inside the zip
that is machine-specific is the archive path -- `roots`/`output_root` -- which
restore rewrites to the folder you point it at. Everything else is already
portable: bookmarks are keyed by platform+profile+filename, clip paths are
relative to the archive root, tombstones are username+name+platform.

The zip is plain: manifest.json, state.json, profiles.db, and (optionally) the
instaloader session so a new machine does not have to spend a rate-limited login.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import time
import zipfile
import threading
from pathlib import Path

FORMAT = 1
APP = "hardlywatching"
STATE_MEMBER = "state.json"
PROFILES_MEMBER = "profiles.db"
MANIFEST_MEMBER = "manifest.json"
SESSION_PREFIX = "instaloader/"
AUTO_PREFIX = "hw-backup-"
# One writer at a time: the startup backup, the daily check and a user pressing
# Export can all fire within the same second.
_WRITE_LOCK = threading.Lock()
DEFAULT_KEEP = 7
AUTO_EVERY_S = 24 * 60 * 60


# ---------------------------------------------------------------------------
# Where things are on *this* machine
# ---------------------------------------------------------------------------

def paths() -> dict:
    """Every file this module touches, resolved by the running app."""
    import engine  # imported here: engine imports this module's callers

    data = engine.data_dir()
    return {
        "data_dir": str(data) if data is not None else "",
        "state": str(engine.STATE_FILE),
        "profiles": str(engine.profiles_path()),
        "cache_db": str(engine.CACHE_DB),
        "session_dir": str(engine.instaloader_config_dir()),
        "backups": str(backups_dir()),
    }


def backups_dir() -> Path:
    import engine

    data = engine.data_dir()
    if data is not None:
        return data / "backups"
    return engine.config_dir() / "hardlywatching" / "backups"


def app_version() -> str:
    try:
        return (Path(__file__).resolve().parent / "VERSION").read_text().strip()
    except Exception:
        return "dev"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _sqlite_snapshot(src: Path, dest: Path) -> None:
    """Copy a live SQLite database so the copy is consistent.

    A plain file copy of a WAL-mode database can miss the newest commit (it is
    in the -wal file) or catch a torn page. sqlite's own backup API takes a
    read lock and writes a single self-contained file.
    """
    src_conn = sqlite3.connect(str(src), timeout=10)
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dest_conn)
            dest_conn.commit()
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def _counts(db_path: Path, state: dict | None) -> dict:
    out = {"profiles": 0, "bookmarks": 0, "categories": 0, "clips": 0, "liked": 0,
           "queued": 0}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            def one(sql: str) -> int:
                try:
                    row = conn.execute(sql).fetchone()
                    return int(row[0]) if row else 0
                except sqlite3.Error:
                    return 0
            out["profiles"] = one("SELECT COUNT(*) FROM profiles")
            out["bookmarks"] = one("SELECT COUNT(*) FROM bookmarks")
            out["categories"] = one("SELECT COUNT(*) FROM bookmark_categories")
            out["clips"] = one("SELECT COUNT(*) FROM clips")
            out["liked"] = one("SELECT COUNT(*) FROM clips WHERE liked=1")
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    if state:
        jobs = state.get("jobs") or []
        out["queued"] = sum(1 for j in jobs
                            if (j.get("status") or "") in ("queued", "running", "retrying"))
    return out


def _session_files() -> list[Path]:
    import engine

    d = engine.instaloader_config_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.is_file() and (p.name.startswith("session-") or p.name == "latest-stamps.ini"):
            out.append(p)
    return out


def ensure_sources() -> None:
    """Materialise state.json / profiles.db if the app has not written them yet.

    Both are written lazily -- a brand-new install that has not changed a setting
    or saved a bookmark has neither file. A backup of "as the app is now" should
    still contain them, or restoring it would silently restore nothing.
    """
    try:
        from profile_db import _shared_db
        _shared_db()          # creates the file and its schema
    except Exception:
        pass
    try:
        import server
        server.get_engine().save_state()
    except Exception:
        pass


def export_zip(dest: Path, include_session: bool = False) -> dict:
    """Write a portable backup to `dest`. Returns the manifest."""
    import engine

    with _WRITE_LOCK:
        ensure_sources()
        return _export_locked(dest, include_session, engine)


def _export_locked(dest: Path, include_session: bool, engine) -> dict:

    state_file = Path(engine.STATE_FILE)
    profiles = Path(engine.profiles_path())
    if not profiles.is_file() and not state_file.is_file():
        raise FileNotFoundError("nothing to back up yet: no state.json and no profiles.db")

    state: dict | None = None
    state_bytes: bytes | None = None
    if state_file.is_file():
        try:
            state_bytes = state_file.read_bytes()
            state = json.loads(state_bytes.decode("utf-8"))
        except (OSError, ValueError):
            # A corrupt state file must not stop the backup: profiles.db is the
            # part that cannot be rebuilt. Ship the raw bytes and say so.
            state = None

    manifest = {
        "format": FORMAT,
        "app": APP,
        "app_version": app_version(),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "created_ts": time.time(),
        "output_root": (state or {}).get("output_root") or str(engine.OUTPUT_ROOT),
        "roots": (state or {}).get("roots") or [str(engine.OUTPUT_ROOT)],
        "counts": _counts(profiles, state),
        "state_ok": state is not None,
        "session": bool(include_session),
    }

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp file: the startup backup and a user-pressed backup can land in
    # the same second, and two writers on one ".part" name sabotage each other.
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".part")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr(MANIFEST_MEMBER, json.dumps(manifest, indent=2))
            if state_bytes is not None:
                zf.writestr(STATE_MEMBER, state_bytes)
            if profiles.is_file():
                with tempfile.TemporaryDirectory() as td:
                    snap = Path(td) / PROFILES_MEMBER
                    _sqlite_snapshot(profiles, snap)
                    zf.write(snap, PROFILES_MEMBER)
            if include_session:
                for f in _session_files():
                    zf.write(f, SESSION_PREFIX + f.name)
        os.replace(tmp, dest)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    manifest["path"] = str(dest)
    manifest["bytes"] = dest.stat().st_size
    return manifest


# ---------------------------------------------------------------------------
# Read / validate
# ---------------------------------------------------------------------------

def _safe_names(zf: zipfile.ZipFile) -> list[str]:
    """Member names, refusing anything that could escape the data folder."""
    out = []
    for name in zf.namelist():
        if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
            raise ValueError(f"refusing unsafe member name: {name!r}")
        out.append(name)
    return out


def inspect(path: Path) -> dict:
    """What is inside a backup, before anything is written."""
    with zipfile.ZipFile(path) as zf:
        names = _safe_names(zf)
        if MANIFEST_MEMBER not in names:
            raise ValueError("not a HardlyWatching backup: manifest.json is missing")
        manifest = json.loads(zf.read(MANIFEST_MEMBER).decode("utf-8"))
        fmt = int(manifest.get("format") or 0)
        if fmt > FORMAT:
            raise ValueError(f"that backup is from a newer version (format {fmt}, "
                             f"this app reads up to {FORMAT})")
        if manifest.get("app") not in (APP, None):
            raise ValueError("that zip belongs to another app")
        if STATE_MEMBER not in names and PROFILES_MEMBER not in names:
            raise ValueError("the backup has neither state.json nor profiles.db")
        manifest["members"] = names
        manifest["bytes"] = path.stat().st_size
        return manifest


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def remap_state(state: dict, new_root: Path) -> tuple[dict, bool]:
    """Point a restored state at the archive folder on *this* machine.

    Only two keys hold absolute paths -- `output_root` and `roots`. Everything
    else is relative or keyed by name, so this is the whole of the migration.
    Roots that still exist (a second drive) are kept; the old downloads folder is
    replaced, not kept as a ghost.
    """
    old_root = str(state.get("output_root") or "")
    changed = old_root != str(new_root)
    state["output_root"] = str(new_root)
    roots: list[str] = [str(new_root)]
    for r in (state.get("roots") or []):
        r = str(r or "")
        if not r or r == old_root or r == str(new_root):
            continue
        if Path(r).is_dir():
            roots.append(r)
        else:
            changed = True
    state["roots"] = roots
    return state, changed


def _atomic_write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=dest.name + ".", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, dest)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def restore(path: Path, archive_root: Path | None = None,
            wanted: set[str] | None = None) -> dict:
    """Replace this app's remembered state with what is in `path`.

    Writes nothing until the zip has been validated all the way through: a
    half-restored app is worse than a refused one.
    """
    import engine

    manifest = inspect(path)
    names = set(manifest["members"])
    want = wanted or {"state", "profiles", "session"}

    state_new: dict | None = None
    with zipfile.ZipFile(path) as zf:
        if "state" in want and STATE_MEMBER in names:
            try:
                state_new = json.loads(zf.read(STATE_MEMBER).decode("utf-8"))
            except ValueError as exc:
                raise ValueError(f"state.json inside the backup is not valid JSON: {exc}")
        profiles_bytes = zf.read(PROFILES_MEMBER) if ("profiles" in want
                                                     and PROFILES_MEMBER in names) else None
        session = {n: zf.read(n) for n in names if n.startswith(SESSION_PREFIX)} \
            if "session" in want else {}

    # A database that will not open is not a database worth installing: check it
    # in a scratch file first.
    if profiles_bytes is not None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as fh:
            fh.write(profiles_bytes)
            probe = Path(fh.name)
        try:
            conn = sqlite3.connect(f"file:{probe}?mode=ro", uri=True)
            try:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()
            if "profiles" not in tables:
                raise ValueError("the profiles.db inside that backup has no profiles table")
        finally:
            probe.unlink()

    target_root = Path(archive_root) if archive_root else None
    if target_root is None:
        recorded = (state_new or {}).get("output_root") or manifest.get("output_root")
        if recorded and Path(recorded).is_dir():
            target_root = Path(recorded)
        else:
            target_root = Path(engine.OUTPUT_ROOT)
    if not target_root.is_dir():
        raise ValueError(f"{target_root} is not a folder on this machine; "
                         "pick the archive folder and try again")

    changed = False
    if state_new is not None:
        state_new, changed = remap_state(state_new, target_root)
        # A restored queue must not start fetching the moment the app reloads.
        # The people and their order are back; whether to actually download
        # hundreds of them again is the owner's decision, one click away.
        state_new["user_paused"] = {"instagram": True, "tiktok": True}
        _atomic_write(Path(engine.STATE_FILE), json.dumps(state_new, indent=1).encode())

    if profiles_bytes is not None:
        db = Path(engine.profiles_path())
        # Drop the WAL first: replacing the main file while a -wal from the old
        # database sits beside it lets sqlite replay stale pages into the new one.
        for suffix in ("-wal", "-shm"):
            try:
                Path(str(db) + suffix).unlink()
            except OSError:
                pass
        _atomic_write(db, profiles_bytes)

    if session:
        sdir = Path(engine.instaloader_config_dir())
        sdir.mkdir(parents=True, exist_ok=True)
        for name, data in session.items():
            dest = sdir / Path(name).name
            _atomic_write(dest, data)
            try:
                dest.chmod(0o600)
            except OSError:
                pass

    return {
        "ok": True,
        "restored": {
            "state": state_new is not None,
            "profiles": profiles_bytes is not None,
            "session": sorted(Path(n).name for n in session),
        },
        "archive_root": str(target_root),
        "root_changed": changed,
        "from": str(path),
        "manifest": {k: manifest.get(k) for k in
                     ("format", "app_version", "created", "output_root", "counts")},
    }


# ---------------------------------------------------------------------------
# Automatic backups
# ---------------------------------------------------------------------------

def list_backups() -> list[dict]:
    d = backups_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob(AUTO_PREFIX + "*.zip"), reverse=True):
        try:
            st = p.stat()
        except OSError:
            continue
        try:
            with zipfile.ZipFile(p) as zf:
                has_session = any(n.startswith(SESSION_PREFIX) for n in zf.namelist())
        except Exception:
            has_session = False
        out.append({"name": p.name, "path": str(p), "bytes": st.st_size,
                    "mtime": st.st_mtime, "ago": _ago(st.st_mtime),
                    "session": has_session})
    return out


def _ago(ts: float) -> str:
    """'today 14:20' / 'yesterday 09:05' / '3 days ago' -- never a raw epoch."""
    t = time.localtime(ts)
    today = time.localtime()
    if (t.tm_year, t.tm_yday) == (today.tm_year, today.tm_yday):
        return "today " + time.strftime("%H:%M", t)
    yesterday = time.localtime(time.time() - 86400)
    if (t.tm_year, t.tm_yday) == (yesterday.tm_year, yesterday.tm_yday):
        return "yesterday " + time.strftime("%H:%M", t)
    days = int(max(0, (time.time() - ts)) // 86400)
    return f"{days} days ago"


def last_backup_age() -> float | None:
    items = list_backups()
    if not items:
        return None
    return max(0.0, time.time() - items[0]["mtime"])


def auto_backup(keep: int = DEFAULT_KEEP, include_session: bool = True) -> dict:
    """Write one dated backup and prune the old ones. Safe to call repeatedly."""
    d = backups_dir()
    d.mkdir(parents=True, exist_ok=True)
    # Second resolution is not enough: the startup backup and a pressed button
    # can land in the same second, and the second write would silently replace
    # the first instead of adding to the seven that are kept.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = d / f"{AUTO_PREFIX}{stamp}-{secrets.token_hex(2)}.zip"
    manifest = export_zip(dest, include_session=include_session)
    kept, removed = [], []
    for old in sorted(d.glob(AUTO_PREFIX + "*.zip"), reverse=True)[max(1, int(keep)):]:
        try:
            old.unlink()
            removed.append(old.name)
        except OSError:
            pass
    kept = [p.name for p in sorted(d.glob(AUTO_PREFIX + "*.zip"), reverse=True)]
    manifest["name"] = dest.name
    manifest["kept"] = kept
    manifest["removed"] = removed
    return manifest


def auto_backup_if_due(every_s: int = AUTO_EVERY_S, keep: int = DEFAULT_KEEP) -> dict | None:
    age = last_backup_age()
    if age is not None and age < every_s:
        return None
    try:
        return auto_backup(keep=keep)
    except FileNotFoundError:
        return None      # a fresh install with no state yet: nothing to do
    except Exception:
        return None      # never let a backup failure stop the app from starting
