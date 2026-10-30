"""AI tags for the archive: who and what is in a picture.

Two different jobs, deliberately kept apart because only one of them needs a
model:

* **Who owns the post.** Every instaloader sidecar carries `owner.full_name`
  and every yt-dlp sidecar carries `uploader`; the profile folder is the
  identity. That is a fact read from metadata, so it is tagged for free and
  cannot be wrong.
* **What is in the frame.** Setting, clothing, activity, shot type -- plus any
  OTHER well-known person the model actually recognises. This is where models
  invent names: on one test picture three models answered "Serena", "Megan Thee
  Stallion" and "Nessa Barrett". So a name from a second person is only stored
  when a second, different model agrees. Everything else is dropped rather than
  guessed at.

Storage is its own SQLite file (`~/.config/hardlywatching/tags.db`), not
profiles.db: it is derived data that can be rebuilt from the archive, and a
separate file means a long tagging pass never holds the gallery's write lock.

CLI:

    python3 tagging.py --profile serena.zulewski --limit 5
    python3 tagging.py --profile serena.zulewski --workers 4
    python3 tagging.py --stats
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# Where things are
# --------------------------------------------------------------------------- #

DEFAULT_ROOTS = [
    Path("/var/mnt/wwn-0x5002303100d1f12d-part1"),
    Path.home() / "instagram",
]

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
VID_EXTS = {".mp4", ".mov", ".webm", ".mkv"}

# A 640px longest side is enough to recognise a face and keeps the request
# small: the same picture is 85 KB here versus 550 KB untouched.
BOX = 640
JPEG_Q = 82

# The model Karakeep uses, on the endpoint it uses: `openai/gpt-4o-mini` through
# OpenRouter. Same id, same provider, same key pool -- so what this pass costs is
# directly comparable to what Karakeep's tagging costs, and the numbers the
# dashboard already shows for that app apply here.
MODEL_FAST = os.environ.get("HW_TAG_MODEL", "openai/gpt-4o-mini")
# Same model by default: Karakeep runs ONE model, and asking a second one to
# confirm every name doubles the bill. When the two ids are equal the check is
# skipped and the model's names are taken as-is -- exactly Karakeep's behaviour.
# Set HW_TAG_CHECK_MODEL to a different model to buy the cross-check back.
MODEL_CHECK = os.environ.get("HW_TAG_CHECK_MODEL", MODEL_FAST)

# gpt-4o-mini through OpenRouter bills the base64 data URL as TEXT unless the
# detail level is pinned: the same 640px picture cost 25,526 input tokens at
# `auto`/`high` and 2,858 at `low`, and the count at auto scaled with the JPEG's
# byte size (64 KB -> 25.5k, 19 KB -> 8.5k). `low` is flat regardless of size, so
# it is the default; HW_TAG_DETAIL=high buys the extra fidelity back at ~9x.
DETAIL = os.environ.get("HW_TAG_DETAIL", "low")

ENDPOINT = os.environ.get("HW_TAG_ENDPOINT",
                          "https://openrouter.ai/api/v1/chat/completions")
KEY_ENV = os.environ.get("HW_TAG_KEY_ENV", "OPENROUTER_API_KEY")
# Where a key may be found, in order. Never logged, never copied anywhere new.
KEY_FILES = [Path.home() / ".hermes" / ".env",
             Path.home() / ".config" / "hardlywatching" / "ai.env"]

# Tokens actually spent, so a pass can be costed instead of estimated.
TOKENS = {"in": 0, "out": 0}


def api_key() -> str:
    """The gateway key, from the environment or a file that already has it.

    Never logged, never written anywhere new: a second copy on disk is a second
    thing to leak.
    """
    key = os.environ.get(KEY_ENV, "").strip()
    if key:
        return key
    for path in KEY_FILES:
        try:
            for line in path.read_text(errors="replace").splitlines():
                line = line.strip()
                if line.startswith(KEY_ENV + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def db_path() -> Path:
    p = os.environ.get("INSTA_TAGS_DB")
    if p:
        return Path(p)
    return Path.home() / ".config" / "hardlywatching" / "tags.db"


_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    """One connection per process, created with the schema."""
    global _CONN
    with _LOCK:
        if _CONN is None:
            p = db_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(str(p), timeout=30, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS tags (
                    path    TEXT PRIMARY KEY,   -- absolute media path
                    size    INTEGER NOT NULL DEFAULT 0,
                    mtime   REAL    NOT NULL DEFAULT 0,
                    owner   TEXT    NOT NULL DEFAULT '',
                    tags    TEXT    NOT NULL DEFAULT '',   -- comma separated
                    people  TEXT    NOT NULL DEFAULT '',   -- confirmed names only
                    caption TEXT    NOT NULL DEFAULT '',
                    model   TEXT    NOT NULL DEFAULT '',
                    checked REAL    NOT NULL DEFAULT 0,
                    failed  TEXT    NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS tag_paths (
                    tag  TEXT NOT NULL,
                    path TEXT NOT NULL,
                    PRIMARY KEY (tag, path)
                );
                CREATE INDEX IF NOT EXISTS tag_paths_tag ON tag_paths(tag);
                """
            )
            c.commit()
            _CONN = c
        return _CONN


def _split(text: str) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def _norm(tag: str) -> str:
    t = re.sub(r"\s+", " ", (tag or "").strip().lower())
    return t[:40]


# --------------------------------------------------------------------------- #
# Who owns the post -- metadata, not a model
# --------------------------------------------------------------------------- #

def owner_name(profile_dir: Path, platform: str = "instagram") -> str:
    """The profile owner's real name, from any sidecar in the folder.

    Instagram: instaloader's `<post>.json` -> node.owner.full_name.
    TikTok: yt-dlp's `<id>.info.json` -> uploader / channel.

    Returns "" when no sidecar says anything better than the handle.
    """
    try:
        if platform == "tiktok":
            for f in sorted(profile_dir.glob("*.info.json"))[:5]:
                try:
                    d = json.loads(f.read_text(errors="replace"))
                except Exception:
                    continue
                for key in ("uploader", "channel", "creator"):
                    v = (d.get(key) or "").strip()
                    if v and v.lower() != profile_dir.name.lower():
                        return v
            return ""
        for f in sorted(profile_dir.glob("*.json"))[:5]:
            try:
                d = json.loads(f.read_text(errors="replace"))
            except Exception:
                continue
            node = d.get("node") or {}
            own = node.get("owner") or {}
            name = (own.get("full_name") or "").strip()
            if name:
                return name
    except OSError:
        pass
    return ""


# --------------------------------------------------------------------------- #
# What is in the frame -- the model
# --------------------------------------------------------------------------- #

PROMPT = (
    "You label photos for a personal archive. Answer with ONLY compact JSON, no prose:\n"
    '{"people": ["Full Name"], "tags": ["tag"], "caption": "one sentence"}\n'
    "people: real, publicly known people you actually recognise (celebrities, actors, "
    "musicians, models, athletes, influencers). Common public name. If you do not "
    "recognise someone, return an empty list -- never guess.\n"
    "tags: 5 to 10 lowercase single words or short phrases for setting, clothing, "
    "activity, mood, colours and shot type.\n"
    "caption: one plain sentence."
)


def _payload_bytes(path: Path) -> bytes | None:
    """A small JPEG of the picture, or the first frame of a video."""
    if path.suffix.lower() in VID_EXTS:
        try:
            import subprocess
            r = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", "1", "-i", str(path),
                 "-frames:v", "1", "-vf", f"scale={BOX}:-2", "-f", "image2",
                 "-vcodec", "mjpeg", "-q:v", "4", "-"],
                capture_output=True, timeout=40)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception:
            return None
        return None
    try:
        from PIL import Image
        im = Image.open(path)
        im = im.convert("RGB")
        im.thumbnail((BOX, BOX))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=JPEG_Q)
        return buf.getvalue()
    except Exception:
        return None


def _ask(model: str, img: bytes, timeout: int = 90) -> dict | None:
    """One vision call. Returns the parsed JSON object, or None."""
    key = api_key()
    if not key:
        raise RuntimeError("no model key: set %s or ~/.hermes/.env" % KEY_ENV)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64," +
                                  base64.b64encode(img).decode(),
                           "detail": DETAIL}},
        ]}],
        "temperature": 0.1,
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        txt = (d["choices"][0]["message"]["content"] or "").strip()
        # Count what was actually billed, not what was estimated: the provider
        # reports it, and a pass that reports its own spend can be trusted.
        use = d.get("usage") or {}
        TOKENS["in"] += int(use.get("prompt_tokens") or 0)
        TOKENS["out"] += int(use.get("completion_tokens") or 0)
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as e:
        return {"__error__": str(e)[:200]}
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return {"__error__": "no json in answer"}
    try:
        out = json.loads(m.group(0))
    except ValueError:
        return {"__error__": "bad json in answer"}
    if not isinstance(out, dict):
        return {"__error__": "answer was not an object"}
    return out


def _names(d: dict) -> list[str]:
    v = d.get("people")
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()][:4]


def tag_media(path: Path, owner: str = "") -> dict:
    """Tags for one file. Owner is a fact; other names need two models to agree."""
    img = _payload_bytes(path)
    if img is None:
        return {"error": "no preview (unreadable or ffmpeg failed)"}
    first = _ask(MODEL_FAST, img)
    if not first:
        return {"error": "no answer"}
    if "__error__" in first:
        return {"error": first["__error__"]}
    tags = [_norm(t) for t in (first.get("tags") or []) if _norm(t)]
    caption = str(first.get("caption") or "")[:300]
    named = [n for n in _names(first) if n.lower() != (owner or "").lower()]
    confirmed: list[str] = []
    if named and MODEL_CHECK and MODEL_CHECK != MODEL_FAST:
        # A second opinion, because a wrong name is worse than no name.
        second = _ask(MODEL_CHECK, img)
        if second and "__error__" not in second:
            other = {n.lower() for n in _names(second)}
            confirmed = [n for n in named if n.lower() in other]
    elif named:
        # One model, no cross-check: Karakeep's behaviour. The name is stored as
        # the model gave it -- which is why the owner's name, read from the
        # sidecar, stays the one name that is certainly right.
        confirmed = named
    return {"tags": tags[:12], "people": confirmed, "caption": caption,
            "model": MODEL_FAST}


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #

def _media_files(folder: Path) -> list[Path]:
    out = []
    try:
        for p in sorted(folder.iterdir()):
            if p.is_file() and p.suffix.lower() in (IMG_EXTS | VID_EXTS):
                out.append(p)
    except OSError:
        pass
    return out


def section_dirs(profile_dir: Path) -> list[Path]:
    """Every folder of one profile that holds media.

    Main sits in the handle folder itself; favorites and trash are subfolders of
    it. Some archives nest the handle once more (`<handle>/<handle>/`), and a
    favourited file there is still that profile's media, so both spellings are
    walked. A picture in trash is tagged like any other -- tags are metadata, and
    deciding what to delete should not depend on having been tagged first.
    """
    dirs = [profile_dir]
    for name in ("favorites", "trash"):
        d = profile_dir / name
        if d.is_dir():
            dirs.append(d)
    nested = profile_dir / profile_dir.name
    if nested.is_dir():
        dirs.append(nested)
        for name in ("favorites", "trash"):
            d = nested / name
            if d.is_dir():
                dirs.append(d)
    return dirs


def _known(paths: list[Path]) -> dict[str, tuple[int, float]]:
    """Files already answered at the same size (so a resume skips them)."""
    if not paths:
        return {}
    c = db()
    out: dict[str, tuple[int, float]] = {}
    chunk = 400
    for i in range(0, len(paths), chunk):
        part = [str(p) for p in paths[i:i + chunk]]
        q = ("SELECT path, size, mtime FROM tags WHERE failed = '' AND path IN (%s)"
             % ",".join("?" * len(part)))
        for row in c.execute(q, part):
            out[row[0]] = (int(row[1]), float(row[2]))
    return out


def _store(path: Path, size: int, mtime: float, owner: str, res: dict) -> None:
    c = db()
    tags = ",".join(res.get("tags") or [])
    people = ",".join(res.get("people") or [])
    with _LOCK:
        c.execute(
            "INSERT INTO tags(path,size,mtime,owner,tags,people,caption,model,checked,failed)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,"
            " owner=excluded.owner, tags=excluded.tags, people=excluded.people,"
            " caption=excluded.caption, model=excluded.model, checked=excluded.checked,"
            " failed=excluded.failed",
            (str(path), size, mtime, owner, tags, people,
             res.get("caption") or "", res.get("model") or "", time.time(),
             res.get("error") or ""))
        c.execute("DELETE FROM tag_paths WHERE path = ?", (str(path),))
        every = set(_split(tags))
        if owner:
            every.add(_norm(owner))
        for name in _split(people):
            every.add(_norm(name))
        c.executemany("INSERT OR IGNORE INTO tag_paths(tag, path) VALUES(?,?)",
                      [(t, str(path)) for t in every if t])
        c.commit()


def scan_profile(folder: Path, platform: str = "instagram", limit: int = 0,
                 workers: int = 3, progress=None, stop=None) -> dict:
    """Tag every untagged file in one profile folder.

    Per profile, not per archive: it is small enough to stop between profiles,
    and every row is committed as it is produced, so an interrupted pass resumes
    where it left off.
    """
    out = {"folder": folder.name, "platform": platform, "tagged": 0, "skipped": 0,
           "failed": 0, "seconds": 0.0, "people": {}}
    files: list[Path] = []
    for d in section_dirs(folder):
        files.extend(_media_files(d))
    if not files:
        return out
    known = _known(files)
    todo: list[Path] = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        rec = known.get(str(p))
        if rec is not None and rec[0] == st.st_size:
            out["skipped"] += 1
            continue
        todo.append(p)
    if limit and limit > 0:
        todo = todo[:limit]
    if not todo:
        return out

    owner = owner_name(folder, platform)
    started = time.time()
    lock = threading.Lock()
    queue = list(todo)

    def work() -> None:
        while True:
            with lock:
                if not queue or (stop and stop()):
                    return
                p = queue.pop(0)
            try:
                st = p.stat()
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                with lock:
                    out["failed"] += 1
                continue
            res = tag_media(p, owner)
            try:
                _store(p, size, mtime, owner, res)
            except Exception as e:                      # a broken row must not stop the pass
                with lock:
                    out["failed"] += 1
                    out["error"] = str(e)[:120]
                continue
            with lock:
                if res.get("error"):
                    out["failed"] += 1
                else:
                    out["tagged"] += 1
                    for n in res.get("people") or []:
                        out["people"][n] = out["people"].get(n, 0) + 1
                if progress:
                    progress(out)

    threads = [threading.Thread(target=work, daemon=True)
               for _ in range(max(1, min(8, workers)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    out["seconds"] = round(time.time() - started, 1)
    out["owner"] = owner
    return out


# --------------------------------------------------------------------------- #
# Reading it back
# --------------------------------------------------------------------------- #

def tag_counts(prefix: str = "", limit: int = 200) -> list[tuple[str, int]]:
    c = db()
    if prefix:
        q = ("SELECT tag, COUNT(*) n FROM tag_paths WHERE tag LIKE ? "
             "GROUP BY tag ORDER BY n DESC LIMIT ?")
        return [(r[0], int(r[1])) for r in c.execute(q, (prefix.lower() + "%", limit))]
    q = "SELECT tag, COUNT(*) n FROM tag_paths GROUP BY tag ORDER BY n DESC LIMIT ?"
    return [(r[0], int(r[1])) for r in c.execute(q, (limit,))]


def search(tags: list[str], limit: int = 500, require_all: bool = True) -> list[str]:
    """Paths carrying the given tags."""
    tags = [_norm(t) for t in tags if _norm(t)]
    if not tags:
        return []
    c = db()
    q = ("SELECT path, COUNT(*) hits FROM tag_paths WHERE tag IN (%s) "
         "GROUP BY path %s ORDER BY hits DESC LIMIT ?"
         % (",".join("?" * len(tags)), "HAVING hits = %d" % len(tags) if require_all else ""))
    return [r[0] for r in c.execute(q, tags + [limit])]


def stats() -> dict:
    c = db()
    n, = c.execute("SELECT COUNT(*) FROM tags WHERE failed = ''").fetchone()
    bad, = c.execute("SELECT COUNT(*) FROM tags WHERE failed != ''").fetchone()
    tags, = c.execute("SELECT COUNT(DISTINCT tag) FROM tag_paths").fetchone()
    people = [r[0] for r in c.execute(
        "SELECT people FROM tags WHERE people != '' LIMIT 200")]
    return {"tagged": int(n), "failed": int(bad), "distinct_tags": int(tags),
            "with_people": len(people), "db": str(db_path()),
            "size_mb": round(db_path().stat().st_size / 1e6, 1) if db_path().exists() else 0,
            "tokens_in": TOKENS["in"], "tokens_out": TOKENS["out"]}


# --------------------------------------------------------------------------- #
# A pass the UI can start and watch
# --------------------------------------------------------------------------- #

_PASS = {
    "running": False, "stop": False, "profile": "", "platform": "instagram",
    "tagged": 0, "failed": 0, "skipped": 0, "total": 0,
    "profiles_done": 0, "profiles_total": 0,
    "started": 0.0, "finished": 0.0, "error": "", "last": "",
}
_PASS_LOCK = threading.Lock()


def pass_state() -> dict:
    with _PASS_LOCK:
        return dict(_PASS)


def stop_pass() -> dict:
    with _PASS_LOCK:
        _PASS["stop"] = True
    return pass_state()


def start_pass(folders: list[tuple[Path, str]], limit: int = 0,
               workers: int = 3) -> dict:
    """Tag the given profile folders in the background, one profile at a time.

    A profile is the unit: it is small enough to stop between two of them, and
    every row is committed as it is made, so a pass that is stopped or crashes
    resumes at the first file it has not answered.
    """
    with _PASS_LOCK:
        if _PASS["running"]:
            return {"ok": False, "error": "a tagging pass is already running"}
        _PASS.update({"running": True, "stop": False, "tagged": 0, "failed": 0,
                      "skipped": 0, "total": 0, "profiles_done": 0,
                      "profiles_total": len(folders), "started": time.time(),
                      "finished": 0.0, "error": "", "last": "",
                      "profile": "", "platform": ""})

    def run() -> None:
        try:
            for folder, plat in folders:
                if _PASS["stop"]:
                    break
                with _PASS_LOCK:
                    _PASS["profile"] = folder.name
                    _PASS["platform"] = plat
                try:
                    res = scan_profile(folder, plat, limit, workers,
                                       progress=_on_progress,
                                       stop=lambda: bool(_PASS["stop"]))
                except Exception as e:                  # one bad profile must not end the pass
                    with _PASS_LOCK:
                        _PASS["error"] = "%s: %s" % (folder.name, str(e)[:140])
                    continue
                with _PASS_LOCK:
                    _PASS["tagged"] += res["tagged"]
                    _PASS["failed"] += res["failed"]
                    _PASS["skipped"] += res["skipped"]
                    _PASS["total"] += res["tagged"] + res["failed"] + res["skipped"]
                    _PASS["profiles_done"] += 1
                    _PASS["last"] = folder.name
        finally:
            with _PASS_LOCK:
                _PASS["running"] = False
                _PASS["finished"] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "profiles": len(folders)}


def _on_progress(st: dict) -> None:
    """Per-file heartbeat, so the counter moves inside a big profile."""
    with _PASS_LOCK:
        _PASS["live"] = st["tagged"] + st["failed"] + st["skipped"]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _find_folder(name: str, platform: str) -> Path | None:
    for root in DEFAULT_ROOTS:
        base = root / platform / name
        if base.is_dir():
            return base
        alt = root / platform / name / name      # some layouts nest once more
        if alt.is_dir():
            return alt
    return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="AI tags for the archive")
    ap.add_argument("--profile", help="one profile folder name")
    ap.add_argument("--platform", default="instagram", choices=["instagram", "tiktok"])
    ap.add_argument("--limit", type=int, default=0, help="stop after N files (0 = all)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--tags", nargs="*", help="list files carrying these tags")
    ap.add_argument("--top", type=int, default=40, help="how many tags to list")
    a = ap.parse_args(argv)

    if a.stats:
        print(json.dumps(stats(), indent=2))
        return 0
    if a.tags:
        hits = search(a.tags)
        print(json.dumps({"tags": a.tags, "count": len(hits), "files": hits[:50]}, indent=2))
        return 0
    if not a.profile:
        for t, n in tag_counts(limit=a.top):
            print("%5d  %s" % (n, t))
        return 0

    folder = _find_folder(a.profile, a.platform)
    if folder is None:
        print("no such profile folder: %s (%s)" % (a.profile, a.platform), file=sys.stderr)
        return 2
    def show(st):
        print("  %d tagged, %d failed" % (st["tagged"], st["failed"]), flush=True)
    res = scan_profile(folder, a.platform, a.limit, a.workers, progress=show)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
