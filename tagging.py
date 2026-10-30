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

# Karakeep's model is `openai/gpt-4o-mini`, but gpt-4o-mini bills a flat 2,833
# tokens per image even at low detail: measured $0.446 per 1,000 images against
# $0.065 for gemini-2.5-flash-lite on the same picture and prompt, with equal tag
# quality and half the latency. So the default is the cheap one; the Karakeep
# model stays one setting away, and the AI tab shows both prices side by side.
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"
MODEL_FAST = os.environ.get("HW_TAG_MODEL", DEFAULT_MODEL)
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

# --------------------------------------------------------------------------- #
# Settings, so the model and the provider are a choice and not a rebuild
#
# Read on every call, not at import: changing the model in the UI has to apply to
# the next picture, not to the next service restart.
# --------------------------------------------------------------------------- #

SETTINGS_FILE = Path.home() / ".config" / "hardlywatching" / "ai.json"

PROVIDERS = {
    "openrouter": {
        "label": "OpenRouter",
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "priced": True,          # bills per token, so a price per image is real
    },
    "manifest": {
        "label": "Manifest gateway (subscription credits)",
        "endpoint": "http://10.0.0.10:2099/v1/chat/completions",
        "key_env": "HERMES_CUSTOM_10_0_0_10_2099_API_KEY",
        "priced": False,         # draws on a credit quota, not a per-token rate
    },
}

DEFAULTS = {
    "enabled": True,             # tag automatically after a profile downloads
    "provider": "openrouter",
    "model": DEFAULT_MODEL,
    "detail": "low",             # low keeps the image at a flat token cost
    "check_model": "",           # "" = no cross-check, one call per picture
    "gender_tags": True,         # always tag male / female / mixed / none
    "scope": "library",          # library | profile
}


def settings() -> dict:
    """The effective settings: defaults, then the file, then the environment."""
    out = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
        if isinstance(saved, dict):
            out.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    if os.environ.get("HW_TAG_MODEL"):
        out["model"] = os.environ["HW_TAG_MODEL"]
    if os.environ.get("HW_TAG_CHECK_MODEL"):
        out["check_model"] = os.environ["HW_TAG_CHECK_MODEL"]
    if os.environ.get("HW_TAG_DETAIL"):
        out["detail"] = os.environ["HW_TAG_DETAIL"]
    if os.environ.get("HW_TAG_ENDPOINT"):
        out["endpoint"] = os.environ["HW_TAG_ENDPOINT"]
    if out.get("provider") not in PROVIDERS:
        out["provider"] = "openrouter"
    prov = PROVIDERS[out["provider"]]
    out.setdefault("endpoint", prov["endpoint"])
    out["endpoint"] = out.get("endpoint") or prov["endpoint"]
    out["key_env"] = prov["key_env"]
    out["provider_label"] = prov["label"]
    out["priced"] = prov["priced"]
    return out


def save_settings(patch: dict) -> dict:
    """Merge a change into ai.json and return the effective settings."""
    cur = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
        if isinstance(saved, dict):
            cur.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    for k, v in (patch or {}).items():
        if k in DEFAULTS:
            cur[k] = v
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cur, indent=2))
    tmp.replace(SETTINGS_FILE)
    return settings()


def api_key(key_env: str = "") -> str:
    """The key for a provider, from the environment or a file that already has it.

    Never logged, never written anywhere new: a second copy on disk is a second
    thing to leak.
    """
    name = key_env or KEY_ENV
    key = os.environ.get(name, "").strip()
    if key:
        return key
    for path in KEY_FILES:
        try:
            for line in path.read_text(errors="replace").splitlines():
                line = line.strip()
                if line.startswith(name + "="):
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
                    gender  TEXT    NOT NULL DEFAULT '',
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
            # A database made before gender was asked for has no column for it.
            cols = {r[1] for r in c.execute("PRAGMA table_info(tags)")}
            if "gender" not in cols:
                c.execute("ALTER TABLE tags ADD COLUMN gender TEXT NOT NULL DEFAULT ''")
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

# The tags ARE the people. A picture of Kendall Jenner gets the tag
# "kendall jenner" and nothing else is asked for: descriptive words (indoor,
# selfie, makeup) describe every picture in this archive and answer nothing
# when he wants to find a person.
PROMPT = (
    "You name the people in photos for a personal archive. Answer with ONLY "
    "compact JSON, no prose:\n"
    '{"people": ["full name"], "gender": "female"}\n'
    "people: every real, publicly known person you can identify in the picture -- "
    "celebrities, actors, musicians, models, athletes, influencers. One entry per "
    "person, lowercase, the full name as the person is publicly known "
    "(\"kendall jenner\", never \"kendall\" or a description). Never invent a name: "
    "return an empty list if you cannot identify anyone.\n"
    "gender: who the main subject is -- exactly one of \"male\", \"female\", "
    "\"mixed\" (both sexes present), \"none\" (no person in frame). Required on "
    "every answer."
)

GENDERS = ("male", "female", "mixed", "none")


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


def _ask(model: str, img: bytes, cfg: dict | None = None, timeout: int = 90) -> dict | None:
    """One vision call. Returns the parsed JSON object, or None."""
    cfg = cfg or settings()
    key = api_key(cfg.get("key_env", ""))
    if not key:
        raise RuntimeError("no model key: set %s or ~/.hermes/.env"
                           % (cfg.get("key_env") or KEY_ENV))
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64," +
                                  base64.b64encode(img).decode(),
                           "detail": cfg.get("detail") or "low"}},
        ]}],
        "temperature": 0.1,
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        cfg.get("endpoint") or ENDPOINT, data=json.dumps(body).encode(),
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


def tag_media(path: Path, owner: str = "", cfg: dict | None = None) -> dict:
    """Tags for one file: the people in it, by name.

    The model's names ARE the tags -- that is the whole feature. The owner's
    name is added from the sidecar by `_store()` (a fact, never a guess), so a
    picture of Kendall Jenner on her own account and the same picture on a fan
    account both carry `kendall jenner`.
    """
    cfg = cfg or settings()
    img = _payload_bytes(path)
    if img is None:
        return {"error": "no preview (unreadable or ffmpeg failed)"}
    model = cfg.get("model") or MODEL_FAST
    first = _ask(model, img, cfg)
    if not first:
        return {"error": "no answer"}
    if "__error__" in first:
        return {"error": first["__error__"]}
    # Gender is asked for on every picture and forced into the tag list, so
    # "hide male" is a filter and not a hope. The model is told it is required;
    # anything outside the four words is dropped rather than stored as-is.
    gender = str(first.get("gender") or "").strip().lower()
    if gender not in GENDERS:
        gender = ""
    named = _names(first)
    confirmed: list[str] = []
    check = cfg.get("check_model") or ""
    if named and check and check != model:
        # A second opinion, because a wrong name is worse than no name.
        second = _ask(check, img, cfg)
        if second and "__error__" not in second:
            other = {n.lower() for n in _names(second)}
            confirmed = [n for n in named if n.lower() in other]
    else:
        # One model, no cross-check: its names are taken as given.
        confirmed = named
    tags = [_norm(n) for n in confirmed if _norm(n)]
    if gender and cfg.get("gender_tags", True) and gender not in tags:
        tags.append(gender)
    return {"tags": tags[:13], "people": confirmed,
            "caption": str(first.get("caption") or "")[:300],
            "gender": gender, "model": model}


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
    gender = (res.get("gender") or "")[:12]
    with _LOCK:
        c.execute(
            "INSERT INTO tags(path,size,mtime,owner,gender,tags,people,caption,model,checked,failed)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,"
            " owner=excluded.owner, gender=excluded.gender, tags=excluded.tags,"
            " people=excluded.people, caption=excluded.caption, model=excluded.model,"
            " checked=excluded.checked, failed=excluded.failed",
            (str(path), size, mtime, owner, gender, tags, people,
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
                 workers: int = 3, progress=None, stop=None,
                 force: bool = False) -> dict:
    """Tag every untagged file in one profile folder.

    Per profile, not per archive: it is small enough to stop between profiles,
    and every row is committed as it is produced, so an interrupted pass resumes
    where it left off. `force` ignores what is already stored, which is the only
    way a changed prompt reaches files tagged under the old one.
    """
    out = {"folder": folder.name, "platform": platform, "tagged": 0, "skipped": 0,
           "failed": 0, "seconds": 0.0, "people": {}}
    files: list[Path] = []
    for d in section_dirs(folder):
        files.extend(_media_files(d))
    if not files:
        return out
    known = {} if force else _known(files)
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


# Measured, not guessed: input/output tokens for one 640px photo at detail=low,
# same prompt, from the providers' own `usage` blocks. Multiplied by the live
# list price this becomes "$ per 1,000 images", which is the only number worth
# showing when choosing a model.
MEASURED = {
    "google/gemini-2.5-flash-lite": (377, 69),
    "openai/gpt-4o-mini": (2858, 28),
    "mistralai/mistral-small-3.2-24b-instruct": (649, 69),
    "amazon/nova-lite-v1": (1266, 27),
    "qwen/qwen3-vl-8b-instruct": (407, 14),
}

_PRICES: dict = {"at": 0.0, "data": {}}


def list_prices(max_age: float = 3600.0) -> dict:
    """{model: (prompt $/M, completion $/M)} from OpenRouter's catalogue."""
    now = time.time()
    if _PRICES["data"] and (now - _PRICES["at"]) < max_age:
        return _PRICES["data"]
    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=20) as r:
            cat = json.loads(r.read())
    except Exception:
        return _PRICES["data"]
    out = {}
    for m in cat.get("data", []):
        p = m.get("pricing") or {}
        try:
            out[m["id"]] = (float(p.get("prompt", 0)) * 1e6,
                            float(p.get("completion", 0)) * 1e6)
        except (TypeError, ValueError):
            continue
    _PRICES.update({"at": now, "data": out})
    return out


def model_menu() -> list[dict]:
    """The models worth offering, with what each costs per 1,000 images."""
    prices = list_prices()
    out = []
    for model, (tin, tout) in MEASURED.items():
        pin, pout = prices.get(model, (0.0, 0.0))
        per1000 = (tin * pin + tout * pout) / 1e6 * 1000
        out.append({"id": model, "in": tin, "out": tout,
                    "price_in": round(pin, 3), "price_out": round(pout, 3),
                    "per_1000": round(per1000, 3),
                    "per_archive": round(per1000 * 46.227, 2)})
    out.sort(key=lambda m: m["per_1000"] or 9e9)
    return out


def search(tags: list[str], limit: int = 500, require_all: bool = True) -> list[str]:
    """Paths carrying the given tags. A tag written `-male` excludes instead.

    Exclusion is what makes "hide male subjects" a real filter: asking for
    `female` alone would still return every mixed group shot, so the useful
    query is `female, -male`.
    """
    want: list[str] = []
    drop: list[str] = []
    for raw in tags or []:
        t = str(raw).strip()
        if not t:
            continue
        if t.startswith("-"):
            v = _norm(t[1:])
            if v:
                drop.append(v)
        else:
            v = _norm(t)
            if v:
                want.append(v)
    if not want and not drop:
        return []
    c = db()
    if not want:
        # Only exclusions: everything that does not carry them.
        q = ("SELECT path FROM tags WHERE failed = '' AND path NOT IN ("
             "SELECT path FROM tag_paths WHERE tag IN (%s)) LIMIT ?"
             % ",".join("?" * len(drop)))
        return [r[0] for r in c.execute(q, drop + [limit])]
    q = ("SELECT path, COUNT(*) hits FROM tag_paths WHERE tag IN (%s) "
         "GROUP BY path %s ORDER BY hits DESC LIMIT ?"
         % (",".join("?" * len(want)), "HAVING hits = %d" % len(want) if require_all else ""))
    hits = [r[0] for r in c.execute(q, want + [limit * 3])]
    if drop:
        gone = set()
        chunk = 400
        for i in range(0, len(hits), chunk):
            part = hits[i:i + chunk]
            q2 = ("SELECT DISTINCT path FROM tag_paths WHERE tag IN (%s) AND path IN (%s)"
                  % (",".join("?" * len(drop)), ",".join("?" * len(part))))
            gone.update(r[0] for r in c.execute(q2, drop + part))
        hits = [h for h in hits if h not in gone]
    return hits[:limit]


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
               workers: int = 3, force: bool = False) -> dict:
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
                      "force": bool(force),
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
                                       stop=lambda: bool(_PASS["stop"]),
                                       force=force)
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
