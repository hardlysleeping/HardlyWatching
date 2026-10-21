"""Local web server for the Instaloader queue UI.

Run:  python3 server.py
Then open http://127.0.0.1:8765

Environment:
  INSTA_PORT    port to bind (default 8765)
  INSTA_HOST    address to bind (default 127.0.0.1; use 0.0.0.0 for LAN/tailnet)
  INSTA_OUTPUT  where downloads go (default ~/instagram)
  INSTA_STATE   where app state is kept (default ~/.config/hardlywatching/state.json)
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from engine import (Engine, UPLOAD_EXTS, clean_profile_name, ensure_pillow_formats,
                    make_preview, make_thumb, _cache, _media_cache_forget)

HERE = Path(__file__).resolve().parent


def _opt(flag: str, env: str, default: str) -> str:
    """Value for a setting: --flag on the command line beats the env var."""
    argv = sys.argv[1:]
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return os.environ.get(env, default)


PORT = int(_opt("--port", "INSTA_PORT", "8765"))
HOST = _opt("--host", "INSTA_HOST", "127.0.0.1")


def find_free_port(start: int, host: str = "127.0.0.1") -> int:
    """Return the first free port at or after `start`.

    Lets the app just work when the default port is taken by something else,
    instead of dying with 'Address already in use'.
    """
    import socket
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    return start  # give up; let the real bind produce the error

# The Engine is created lazily so that importing this module (tests, tooling,
# anything that just wants Handler) never constructs a worker or touches the
# user's real state file. Use get_engine() everywhere instead of a global.
_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine


# Local SQLite record of imported profiles. Created lazily and shared, so the
# connection is not reopened per request.
_profile_db_inst = None


def _profile_db():
    # Delegates to the single shared instance so the engine and the HTTP layer
    # never hold two connections to the same database.
    from profile_db import _shared_db
    return _shared_db()


# The shorts feed rescans the archive on every request (~1 s for ~19k files on
# an external drive), and the feed refills after only a couple of swipes -- so
# without a cache each swipe cost a full directory walk. The result is held
# briefly: a clip downloaded a moment ago shows up within the TTL, and an
# explicit refresh bypasses it entirely.
_SCAN_TTL = 15.0
_scan_cache: dict = {"t": 0.0, "root": None, "found": None}
_scan_lock = threading.Lock()


def _scan_clips(root, force: bool = False):
    import shorts
    now = time.time()
    with _scan_lock:
        c = _scan_cache
        if (not force and c["found"] is not None and c["root"] == str(root)
                and (now - c["t"]) < _SCAN_TTL):
            return c["found"]
        db = _profile_db()
        found = shorts.scan(root, known_reels=db.known_reels())
        c.update(t=now, root=str(root), found=found)
        return found


def _invalidate_scan():
    """Drop the cached scan, e.g. just after a download finishes."""
    with _scan_lock:
        _scan_cache["found"] = None


def _systemctl(*args) -> tuple[int, str]:
    try:
        r = subprocess.run(["systemctl", "--user", *args],
                           capture_output=True, text=True, timeout=10)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


# The unit this app is normally installed as. "Start on login" is exactly
# systemd's idea of that unit being enabled, so it is read and set through
# systemctl instead of kept in our own settings: a second copy would drift the
# moment anyone used the terminal.
SERVICE_UNIT = "hardlywatching.service"


def autostart_state() -> dict:
    rc, out = _systemctl("is-enabled", SERVICE_UNIT)
    state = (out.splitlines() or [""])[0].strip()
    if state in ("", "not-found") or "No such" in out or "not found" in out.lower():
        return {"ok": True, "supported": False, "enabled": False, "state": state or "absent"}
    return {"ok": True, "supported": True, "enabled": state == "enabled", "state": state}


def set_autostart(want: bool) -> dict:
    rc, out = _systemctl("enable" if want else "disable", SERVICE_UNIT)
    st = autostart_state()
    if rc != 0 and not st.get("supported"):
        return {**st, "ok": False,
                "error": "Not running as a service, so there is nothing to enable. "
                         "Install it with install-service.sh first."}
    return {**st, "ok": rc == 0, "error": "" if rc == 0 else out}


def _open_folder(path: Path) -> None:
    """Reveal a folder in the desktop's file manager, on any OS."""
    if not path.exists():
        return
    if sys.platform == "darwin":
        cmds = [["open", str(path)]]
    elif os.name == "nt":
        cmds = [["explorer", str(path)]]
    else:
        cmds = [["xdg-open", str(path)], ["gio", "open", str(path)]]
    for cmd in cmds:
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except FileNotFoundError:
            continue


def _find_terminal() -> list[str] | None:
    """Return argv prefix for the available terminal, or None.

    Each emulator takes a different flag for 'run this command', so the
    mapping matters -- ptyxis/gnome-terminal use `--`, most others use `-e`.
    """
    import shutil as _sh
    # (binary, how to pass a command)
    candidates = [
        ("ptyxis", "--"),
        ("kgx", "--"),
        ("gnome-terminal", "--"),
        ("konsole", "-e"),
        ("xfce4-terminal", "-e"),
        ("tilix", "-e"),
        ("mate-terminal", "-e"),
        ("lxterminal", "-e"),
        ("alacritty", "-e"),
        ("kitty", "-e"),
        ("wezterm", "start", "--"),
        ("foot", "-e"),
        ("xterm", "-e"),
    ]
    for binary, mode in candidates:
        path = _sh.which(binary)
        if path:
            return [path, mode]
    return None


class QuietServer(ThreadingHTTPServer):
    """A threaded server that does not shout when a browser walks away.

    Closing a tab, navigating mid-poll, or a tool cancelling a request closes the
    socket while a response is still being written. socketserver reports that as
    a full traceback ending in BrokenPipeError/ConnectionResetError, which reads
    in the journal exactly like a crash. It is not one, so drop it.
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "InstaQueue/2.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    # ---------- upload ----------

    # A single file is capped so one enormous upload cannot exhaust memory: the
    # body is read into RAM before being written out. Raise via the env var if
    # you ever need to push something bigger.
    MAX_UPLOAD = int(os.environ.get("INSTA_MAX_UPLOAD", 512 * 1024 * 1024))

    def _upload(self) -> None:
        """Accept files from the browser and write them into a profile folder.

        multipart/form-data is parsed here by hand rather than with `cgi`, which
        is gone in Python 3.13+. The body is read whole, split on the boundary,
        and each part's filename and bytes are taken out. Files are written to a
        temporary name in the destination and renamed on success, so an
        interrupted or cancelled upload can never leave a half file that the
        gallery would show as a truncated image.
        """
        ctype = self.headers.get("Content-Type") or ""
        m = re.search(r'boundary="?([^";]+)"?', ctype)
        if "multipart/form-data" not in ctype or not m:
            return self._json({"ok": False,
                               "message": "Expected a multipart upload."}, 400)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return self._json({"ok": False, "message": "Empty upload."}, 400)
        if n > self.MAX_UPLOAD:
            return self._json({"ok": False, "message":
                f"That upload is larger than the {self.MAX_UPLOAD // (1024*1024)}MB "
                "limit."}, 413)

        # Stream the body to a scratch file rather than read(n) into RAM: the
        # size cap allows hundreds of MB, and reading it whole doubles that in
        # memory once the boundary split runs. The read here stays bounded to
        # one chunk at a time; the body is then parsed from disk.
        import tempfile
        scratch = tempfile.NamedTemporaryFile(prefix="hw-up-", delete=False)
        scratch_name = scratch.name
        try:
            remaining = n
            while remaining > 0:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                scratch.write(chunk)
                remaining -= len(chunk)
        finally:
            scratch.close()
        try:
            with open(scratch_name, "rb") as fh:
                raw = fh.read()
        finally:
            try:
                os.unlink(scratch_name)
            except OSError:
                pass
        boundary = b"--" + m.group(1).encode()

        # Parts are: headers, blank line, content. Split on the boundary and
        # keep only the pieces that actually carry a filename.
        profile = ""
        section = "main"
        files: list[tuple[str, bytes]] = []
        for chunk in raw.split(boundary):
            if not chunk or chunk in (b"--", b"--\r\n", b"\r\n"):
                continue
            if b"\r\n\r\n" not in chunk:
                continue
            head, _, content = chunk.partition(b"\r\n\r\n")
            # Trim the CRLF that belongs to the boundary, not the file.
            if content.endswith(b"\r\n"):
                content = content[:-2]
            head_text = head.decode("utf-8", "replace")
            fm = re.search(r'filename="([^"]*)"', head_text)
            nm = re.search(r'name="([^"]*)"', head_text)
            field = nm.group(1) if nm else ""
            if fm is not None:
                name = os.path.basename(fm.group(1) or "")
                if name:
                    files.append((name, content))
            elif field == "profile":
                profile = content.decode("utf-8", "replace").strip()
            elif field == "section":
                section = content.decode("utf-8", "replace").strip() or "main"

        if not files:
            return self._json({"ok": False, "message": "No files in that upload."}, 400)
        if not profile:
            return self._json({"ok": False, "message": "Pick a profile first."}, 400)

        eng = get_engine()
        username = clean_profile_name(profile)
        if not username:
            return self._json({"ok": False, "message": "That profile name is not usable."}, 400)

        # Write into the folder that already holds this profile, so uploading to
        # an existing archive adds to it. A brand new name goes to the root that
        # receives downloads, which must be mounted -- otherwise files would
        # land in a directory that is not there.
        base = eng.root_for(username)
        if base is None:
            root = eng.output_root
            try:
                if not root.is_dir():
                    return self._json({"ok": False, "message":
                        f"{root} is not there right now. Is the drive mounted?"}, 400)
                base = root / username
                base.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return self._json({"ok": False,
                                   "message": f"Could not create {base}: {e}"}, 400)
        if section in ("favorites", "trash"):
            base = base / section
            try:
                base.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return self._json({"ok": False,
                                   "message": f"Could not create {base}: {e}"}, 400)

        saved, skipped, failed = [], [], []
        for name, data in files:
            # Only media the gallery can actually display, and never a path.
            if name != os.path.basename(name):
                failed.append((name, "unusable filename"))
                continue
            if os.path.splitext(name)[1].lower() not in UPLOAD_EXTS:
                skipped.append(name)
                continue
            dest = base / name
            # Do not silently overwrite something already archived: keep both,
            # with a suffix, so an upload can never destroy a download.
            if dest.exists():
                stem, ext = os.path.splitext(name)
                k = 2
                while (base / f"{stem}_{k}{ext}").exists() and k < 1000:
                    k += 1
                dest = base / f"{stem}_{k}{ext}"
            tmp = base / (dest.name + ".uploading")
            try:
                tmp.write_bytes(data)
                tmp.replace(dest)      # atomic: appears complete or not at all
                saved.append(dest.name)
            except OSError as e:
                failed.append((name, str(e)))
                try:
                    tmp.unlink()
                except OSError:
                    pass

        # The folder changed, so the listing cache for it is now wrong.
        _cache.drop(str(base))
        _media_cache_forget(base)

        return self._json({"ok": bool(saved), "profile": username,
                           "saved": saved, "skipped": skipped,
                           "failed": [{"file": f, "why": w} for f, w in failed],
                           "section": section})

    def _serve_file(self, path: Path, cache: bool = False) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        size = path.stat().st_size

        # Honour a single byte range so a video can be seeked: the browser asks
        # for `bytes=N-` and expects 206 with Content-Range. Without this the
        # whole clip streams from the start, so scrubbing does nothing.
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.split(",")[0].strip())
            if m:
                a, b = m.group(1), m.group(2)
                if a == "" and b and int(b) > 0:
                    # suffix form `bytes=-N`: the last N bytes
                    start, end, status = max(0, size - int(b)), size - 1, 206
                elif a != "":
                    start = int(a)
                    end = min(int(b), size - 1) if b else size - 1
                    if 0 <= start <= end < size:
                        status = 206

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
        else:
            self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control",
                         "private, max-age=86400" if cache else "no-store")
        self.end_headers()
        # Stream rather than read_bytes(): a clip can be hundreds of MB, and
        # loading one whole per request is how a couple of concurrent videos
        # exhaust RAM. A range seeks and copies exactly that window.
        with open(path, "rb") as fh:
            fh.seek(start)
            shutil.copyfileobj(fh, self.wfile, length=end - start + 1)

    # ---------- routes ----------

    def do_GET(self) -> None:
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            return self._serve_file(HERE / "index.html")
        if p in ("/shorts", "/shorts/"):
            # Phone-shaped feed. Served from the same app so it shares the
            # output root, the state file and the clips table.
            return self._serve_file(HERE / "shorts.html")
        if p == "/shorts.webmanifest":
            return self._serve_file(HERE / "shorts.webmanifest")
        if p == "/api/autostart":
            # Read-only: whether the service starts itself at login.
            return self._json(autostart_state())
        if p == "/api/tiktok/browsers":
            # Which browsers are signed into TikTok (yt-dlp takes a name, not a
            # cookie file, so this is a plain list).
            return self._json({"browsers": get_engine().tiktok_browsers()})
        if p.startswith("/api/shorts/file/"):
            # Stream a clip from the archive. resolve() refuses anything that
            # escapes the output root, so a traversal attempt 404s.
            import shorts
            rel = p[len("/api/shorts/file/"):]
            from urllib.parse import unquote
            target = shorts.resolve(get_engine().output_root, unquote(rel))
            if target is None:
                return self.send_error(404)
            return self._serve_file(target, cache=True)
        if p == "/api/state":
            # The UI polls this every ~1.2s, so it doubles as a worker watchdog:
            # if the worker died with work still queued, revive it.
            get_engine().watchdog()
            snap = get_engine().snapshot()
            snap["roots"] = get_engine().roots_status()
            # Surface the on-disk listing cache so its state is inspectable
            # rather than a silent performance mystery.
            snap["cache"] = _cache.stats()
            # What the automatic retry sweep is waiting on, so a red card can
            # say "retrying in 40 min" instead of looking abandoned.
            try:
                snap["auto_retry"] = get_engine().retry_status()
            except Exception:
                snap["auto_retry"] = {"enabled": False, "waiting": []}
            return self._json(snap)
        if p == "/api/profile-pic":
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            name = clean_profile_name((q.get("profile") or [""])[0])
            if not name:
                return self.send_error(400)
            # Resolve across every root: this used to read output_root only, so
            # changing the downloads folder made every older profile 404 here.
            d = get_engine().root_for(name)
            if d is None:
                return self.send_error(404)
            # instaloader names these <user>_<date>_UTC_profile_pic.<ext>, so
            # that pattern is tried first when it exists.
            hits = sorted(d.glob("*_profile_pic.*"),
                          key=lambda x: x.stat().st_mtime, reverse=True)
            if hits:
                return self._serve_file(hits[0])
            # A folder that did not come from instaloader -- your own uploaded
            # photos -- has no such file, so fall back to the newest image in
            # the folder. That is the "picture of them" a hand-made profile
            # folder should show, instead of a broken avatar.
            try:
                imgs = [f for f in d.iterdir()
                        if f.is_file()
                        and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
                        and f.stat().st_size > 0]
            except OSError:
                return self.send_error(404)
            if not imgs:
                return self.send_error(404)
            imgs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            return self._serve_file(imgs[0])
        if p == "/api/health":
            return self._json({"ok": True})
        if p == "/api/job-log":
            # Full log for one job. The state poll carries logs only for
            # running/failed jobs, so an opened log panel fetches the rest here.
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            jid = (q.get("id") or [""])[0]
            if not jid:
                return self.send_error(400)
            return self._json(get_engine().job_log(jid))
        if p == "/api/partials":
            # Preview of interrupted-download leftovers, so the UI can say what
            # it would free before deleting anything.
            return self._json(get_engine().sweep_partials(dry_run=True))

        if p == "/api/gallery":
            # Profiles whose archive is finished and still on disk.
            return self._json({"profiles": get_engine().gallery_profiles()})
        if p == "/api/timeline":
            # The months present and how many posts each holds. Deliberately not
            # the posts themselves: that was 7.6MB for 30k posts to draw one
            # screen. The UI asks for a single month with /api/timeline-month.
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            favs = (q.get("favorites") or ["0"])[0] in ("1", "true", "yes")
            return self._json(get_engine().timeline(only_favorites=favs))
        if p == "/api/timeline-month":
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            month = (q.get("month") or [""])[0] or "all"
            try:
                offset = int((q.get("offset") or ["0"])[0] or 0)
                limit = int((q.get("limit") or ["200"])[0] or 200)
            except ValueError:
                offset, limit = 0, 200
            favs = (q.get("favorites") or ["0"])[0] in ("1", "true", "yes")
            return self._json(get_engine().timeline_month(
                month, offset, limit, only_favorites=favs))
        if p == "/api/gallery/all":
            # Overarching Favourites / Trash across every profile.
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            section = (q.get("section") or [""])[0]
            if section not in ("favorites", "trash"):
                return self.send_error(400)
            return self._json({"section": section,
                               "profiles": get_engine().gallery_profiles(),
                               "posts": get_engine().gallery_all(section)})
        if p == "/api/photos":
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            name = clean_profile_name((q.get("profile") or [""])[0])
            if not name:
                return self.send_error(400)
            plat = (q.get("platform") or ["instagram"])[0]
            if plat not in ("instagram", "tiktok"):
                plat = "instagram"
            return self._json({"profile": name,
                               "sections": get_engine().gallery_sections(name, plat)})
        if p == "/media":
            # Serve a file, but only one inside a finished profile's folder.
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            name = clean_profile_name((q.get("profile") or [""])[0])
            rel = (q.get("file") or [""])[0]
            sub = (q.get("section") or [""])[0]
            plat = (q.get("platform") or ["instagram"])[0]
            if not name or not rel:
                return self.send_error(400)
            target = get_engine().resolve_media(name, rel, sub, plat)
            if target is None:
                return self.send_error(404)
            # Allow the browser to cache: these files never change in place.
            return self._serve_file(target, cache=True)
        if p == "/view":
            # Full-size view of one image. HEIC is what phones write and only
            # Safari can draw it, so it is converted to JPEG here; everything
            # else is served as-is and never re-encoded.
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            name = clean_profile_name((q.get("profile") or [""])[0])
            rel = (q.get("file") or [""])[0]
            sub = (q.get("section") or [""])[0]
            plat = (q.get("platform") or ["instagram"])[0]
            if not name or not rel:
                return self.send_error(400)
            target = get_engine().resolve_media(name, rel, sub, plat)
            if target is None:
                return self.send_error(404)
            if target.suffix.lower() not in {".heic", ".heif"}:
                return self._serve_file(target, cache=True)
            try:
                ensure_pillow_formats()
                from PIL import Image
                import io
                with Image.open(target) as im:
                    im = im.convert("RGB")
                    # Cap the long edge: a phone photo is bigger than any
                    # screen, and this is a viewing copy, not the archive.
                    if max(im.size) > 2400:
                        im.thumbnail((2400, 2400))
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=88)
                data = buf.getvalue()
            except Exception as e:
                print(f"heic view failed for {target.name}: {e}")
                return self.send_error(415, "Cannot convert this image")
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return

        if p == "/thumb":
            # Small cached JPEG for a grid tile. Originals average ~1.9 MB, so
            # this is what keeps a large gallery scrollable.
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            name = clean_profile_name((q.get("profile") or [""])[0])
            rel = (q.get("file") or [""])[0]
            sub = (q.get("section") or [""])[0]
            plat = (q.get("platform") or ["instagram"])[0]
            if not name or not rel:
                return self.send_error(400)
            target = get_engine().resolve_media(name, rel, sub, plat)
            if target is None:
                return self.send_error(404)
            thumb = make_thumb(target, target.suffix.lower())
            if thumb is None:
                # No thumbnail possible (odd codec, corrupt file): fall back to
                # the original so the tile still shows something.
                return self._serve_file(target, cache=True)
            return self._serve_file(thumb, cache=True)
        if p == "/preview":
            # Short silent clip for hover playback on a video tile. Falls back
            # to the original when a preview cannot be made.
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            name = clean_profile_name((q.get("profile") or [""])[0])
            rel = (q.get("file") or [""])[0]
            sub = (q.get("section") or [""])[0]
            plat = (q.get("platform") or ["instagram"])[0]
            if not name or not rel:
                return self.send_error(400)
            target = get_engine().resolve_media(name, rel, sub, plat)
            if target is None:
                return self.send_error(404)
            prev = make_preview(target)
            if prev is None:
                return self.send_error(404)
            return self._serve_file(prev, cache=True)
        return self.send_error(404)

    def do_POST(self) -> None:
        p = urlparse(self.path).path

        # Uploads are multipart/form-data and can be large, so they are handled
        # before the JSON body is read: _read_json() would try to parse the
        # whole upload as JSON.
        if p == "/api/upload":
            return self._upload()

        body = self._read_json()

        if p == "/api/add":
            raw = body.get("profiles")
            if isinstance(raw, str):
                names = raw.replace(",", "\n").splitlines()
            elif isinstance(raw, list):
                names = [str(x) for x in raw]
            else:
                names = []
            names = [n for n in names if n.strip()]
            overrides = body.get("overrides") or None
            added, rejected = get_engine().add(names, overrides)
            return self._json({"added": [j.username for j in added], "rejected": rejected})

        if p == "/api/tiktok/parse":
            # Dry run: parse a pasted dump and report what would be queued,
            # without touching the queue. Lets the user see the count first.
            import tiktok_handles
            text = str(body.get("text") or "")
            handles, source, dropped = tiktok_handles.parse(text)
            db = _profile_db()
            known = db.known("tiktok")
            excluded = db.excluded("tiktok")
            # Excluded are counted on their own: they are "known" but will not
            # be queued, so lumping them in with already_known would be a lie.
            fresh = [h for h in handles
                     if h.lower() not in known and h.lower() not in excluded]
            n_excl = sum(1 for h in handles if h.lower() in excluded)
            # A paste taken before scrolling to the bottom of the list holds
            # only the rows that were on screen. The page prints the real total,
            # so flag the mismatch instead of letting it look complete.
            total = tiktok_handles.stated_total(text)
            short = (total and len(handles) < total * 0.9)
            return self._json({
                "parsed": len(handles),
                "source": source,
                "new": len(fresh),
                "already_known": len(handles) - len(fresh) - n_excl,
                "excluded": n_excl,
                "stated_total": total,
                "short": bool(short),
                "dropped": dropped,
                "sample": handles[:10],
            })

        if p in ("/api/excluded", "/api/tiktok/excluded"):
            # One list covering both platforms. Rows carry their platform
            # because the same handle on Instagram and TikTok is usually a
            # different person, and restoring one must not restore the other.
            #
            # This lives on the Download tab but below the queue, so it is
            # available without being in the way of normal use.
            db = _profile_db()
            want = str(body.get("platform") or "all")
            platforms = ["instagram", "tiktok"] if want == "all" else [want]
            rows = []
            for plat in platforms:
                for r in db.excluded_list(plat):
                    rows.append({
                        "handle": r["handle"],
                        "platform": plat,
                        "display": r.get("display_name") or "",
                        "reason": r.get("reason") or "",
                        "excluded_at": r.get("excluded_at") or 0,
                    })
            rows.sort(key=lambda r: r["excluded_at"], reverse=True)
            return self._json({"count": len(rows), "profiles": rows})

        if p in ("/api/restore", "/api/tiktok/restore"):
            # Put profiles back into the queue. Both tools skip what is already
            # on disk, so a restored profile is cheap to re-run.
            db = _profile_db()
            plat = str(body.get("platform") or "tiktok")
            handles = [str(h).strip().lstrip('@')
                       for h in (body.get("handles") or []) if str(h).strip()]
            if not handles:
                return self._json({"ok": False, "error": "no handles given"})
            restored = [h for h in handles if db.restore(plat, h)]
            if plat == "tiktok":
                added, _ = get_engine().add_tiktok(
                    restored, count=int(body.get("count") or 5), start=False)
            else:
                added, _ = get_engine().add(restored)
            db.mark_imported(plat, [j.username for j in added])
            return self._json({"ok": True, "restored": len(restored),
                               "queued": len(added), "handles": restored})

        if p == "/api/tiktok/import":
            import tiktok_handles
            text = str(body.get("text") or "")
            count = int(body.get("count") or 5)
            handles, source, dropped = tiktok_handles.parse(text)
            if not handles:
                return self._json({"ok": False, "error": "No usernames found in that text."})
            db = _profile_db()
            excluded = db.excluded("tiktok")
            blocked = [h for h in handles if h.lower() in excluded]
            # Only record people who are actually eligible, so an excluded
            # handle is never quietly touched by an import.
            for h in handles:
                if h.lower() not in excluded:
                    db.record("tiktok", h, source=source or "following")
            try:
                added, skipped = get_engine().add_tiktok(
                    handles, count=count, start=False, excluded=excluded)
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
            db.mark_imported("tiktok", [j.username for j in added])
            # Ask TikTok whether each newly queued profile is actually visible,
            # and fill in each profile's video total, so the count reads
            # "5 / 120" without pressing a button. Both cost one request per
            # profile -- exactly the traffic that gets an IP noticed -- so they
            # are capped and run in the background: a large paste returns at
            # once instead of blocking for minutes, and the totals and private
            # flags fill in on the next poll as they complete.
            total_cap = int(body.get("totals") or 25)
            do_probe = bool(body.get("probe"))
            if total_cap > 0 or do_probe:
                def _fill_tiktok_meta() -> None:
                    import tiktok_engine as _tt
                    eng = get_engine()
                    targets = list(added[:max(0, total_cap)])
                    for job in targets:
                        try:
                            total = _tt.profile_total(job.username)
                        except Exception:
                            total = 0
                        with eng.lock:
                            job.tiktok_total = total
                    if do_probe:
                        for job in added:
                            try:
                                verdict = _tt.probe_private(job.username)
                            except Exception:
                                verdict = None
                            if verdict is True:
                                with eng.lock:
                                    job.private = True
                    eng.save_state()

                threading.Thread(target=_fill_tiktok_meta, daemon=True).start()

            return self._json({
                "ok": True,
                "parsed": len(handles),
                "queued": len(added),
                "skipped": len(handles) - len(added),
                "excluded": len(blocked),
                "excluded_handles": blocked[:20],
                "urls": tiktok_handles.to_urls(handles, "tiktok"),
            })

        if p == "/api/tiktok/more":
            # Raise how many videos one TikTok profile should have, and queue it
            # again if it had already finished. yt-dlp's archive file records
            # what was fetched, so only the missing ones are downloaded: going
            # from 5 to 35 pulls 30 more, not 35 again.
            #
            # `add` is how many more to aim for (+5, +15 ...). `all` means no
            # limit at all (count 0 in the job, which is how "all" is stored).
            db = _profile_db()
            job_id = str(body.get("id") or "")
            eng = get_engine()
            with eng.lock:
                job = eng.jobs.get(job_id)
                if job is None or getattr(job, "platform", "") != "tiktok":
                    return self._json({"ok": False, "error": "no such TikTok profile"})
                current = int(getattr(job, "tiktok_count", 0) or 0)
                # Never look the profile's total up here. A flat listing of a
                # real profile measures ~89 seconds (1492 videos) and it would
                # run synchronously while holding the engine lock -- which also
                # stalls both workers and every other API call. The click has to
                # answer immediately; the total is filled in the background by
                # the import-time filler and is simply shown as unknown until
                # then.
                total = int(getattr(job, "tiktok_total", 0) or 0)

                if body.get("all"):
                    new_count = 0
                else:
                    step = abs(int(body.get("add") or 5))
                    if body.get("minus") or body.get("subtract"):
                        # Never below zero, and 0 already means "all", so a
                        # subtract that would reach 0 stops at 1 instead.
                        base = current if current else (total or 1)
                        new_count = max(1, base - step)
                    else:
                        new_count = (current or 0) + step
                        if total and new_count > total:
                            # Asking for more than exists just means all of it.
                            new_count = total
                job.tiktok_count = new_count
                revived = False
                if job.status in ("done", "failed", "cancelled", "skipped"):
                    # Anything already on disk stays; the run fetches the rest.
                    job.status = "queued"
                    job.finished_at = None
                    job.error = ""
                    job.message = "queued for more videos"
                    job.fatal = False
                    revived = True
                    # Front of the queue, not the back. You just acted on this
                    # profile, so it should be next for its own platform rather
                    # than behind every other profile -- at a few minutes of
                    # pacing per profile, appending would put it days away and
                    # make the button look like it did nothing.
                    if job.id in eng.pending:
                        eng.pending.remove(job.id)
                    eng.pending.insert(0, job.id)
            eng.save_state()
            return self._json({"ok": True, "count": new_count, "total": job.tiktok_total,
                               "revived": revived, "username": job.username})

        if p == "/api/shorts/feed":
            # Newest-first scan of what is actually on disk, synced into the
            # clips table, then a weighted pick. Verifying against disk each
            # call is what makes a deleted file drop out of the feed; the scan
            # itself is cached for a few seconds so a swipe does not re-walk
            # the whole archive.
            limit = int(body.get("limit") or 20)
            fav = bool(body.get("favourites"))
            seen = body.get("exclude") or []
            db = _profile_db()
            found = _scan_clips(get_engine().output_root, force=bool(body.get("refresh")))
            db.sync_clips(found)
            alive = [f["path"] for f in found]
            by_path = {f["path"]: f for f in found}
            rows = db.feed(limit, favourites_only=fav, exclude=seen, known_paths=alive)
            out = []
            for r in rows:
                meta = by_path.get(r["path"], {})
                out.append({**r, **{k: meta.get(k) for k in ("url", "mtime", "bytes")}})
            db.mark_shown([r["path"] for r in out])
            return self._json({"clips": out, "stats": db.clip_stats()})

        if p == "/api/shorts/action":
            db = _profile_db()
            path = str(body.get("path") or "")
            action = str(body.get("action") or "")
            if action == "like":
                db.set_liked(path, bool(body.get("value", True)))
                return self._json({"ok": True, "liked": db.clip(path)["liked"]})
            if action == "trash":
                db.set_trashed(path, True)
                return self._json({"ok": True})
            if action in ("more", "less"):
                # "Show more/less often" is about the PERSON, not this one
                # clip: one video landing well means you want more from them.
                # Falls back to the clip when its profile is unknown.
                c = db.clip(path) or {}
                target = str(body.get("target") or "profile")
                if target == "profile" and c.get("profile"):
                    w = db.set_profile_weight(c.get("platform") or "instagram",
                                              c.get("profile"), action)
                    return self._json({"ok": True, "scope": "profile",
                                       "profile": c.get("profile"), "weight": w})
                return self._json({"ok": True, "scope": "clip",
                                   "weight": db.set_weight(path, action)})
            return self._json({"ok": False, "error": "unknown action"})

        if p == "/api/tiktok/cookies-from-browser":
            # Do not lowercase here: a Firefox-fork spec carries a profile PATH
            # (firefox:/home/... ), and lowercasing it points at nothing.
            browser = str(body.get("browser") or "").strip()
            if not browser:
                # Auto-pick: the best detected browser with TikTok cookies.
                eng = get_engine()
                cands = eng.tiktok_browsers()
                if not cands:
                    return self._json({"ok": False,
                                       "error": "No browser with a TikTok login was found. "
                                               "Sign in to TikTok in Firefox or Zen, then retry."})
                browser = cands[0]
            return self._json(get_engine().set_tiktok_cookies_browser(browser))

        if p == "/api/remove":
            return self._json({"ok": get_engine().remove(body.get("id", ""))})
        if p == "/api/forget":
            return self._json({"ok": get_engine().forget(body.get("id", ""))})

        if p == "/api/extra-pass":
            return self._json(get_engine().add_extra_pass(
                str(body.get("id") or body.get("username") or ""),
                str(body.get("kind") or "")))

        if p == "/api/restore":
            return self._json({"ok": get_engine().restore(body.get("id", ""))})

        if p == "/api/restore-all":
            return self._json({"restored": get_engine().restore_all()})

        if p == "/api/purge-removed":
            return self._json({"purged": get_engine().purge_removed()})

        if p == "/api/partials/clear":
            return self._json(get_engine().sweep_partials(dry_run=False))

        if p == "/api/requeue":
            return self._json({
                "ok": get_engine().requeue(body.get("id", ""))
            })

        if p == "/api/move":
            jid = body.get("id", "")
            if "index" in body:
                ok = get_engine().move_to(jid, int(body["index"]))
            else:
                ok = get_engine().move(jid, int(body.get("delta", 0)))
            return self._json({"ok": ok})

        if p == "/api/gallery/trash/clear":
            # Permanent. Requires an explicit confirm token in the body.
            if body.get("confirm") != "DELETE":
                return self._json({"ok": False, "error": "confirmation required"})
            n, errors = get_engine().clear_trash()
            return self._json({"ok": True, "removed": n, "errors": errors[:20]})

        if p == "/api/gallery/tombstones":
            # What has been permanently deleted, and the 0-byte placeholders
            # that stop instaloader re-downloading it.
            return self._json({"tombstones": get_engine().list_tombstones()})

        if p == "/api/gallery/tombstones/remove":
            # Drop a tombstone: removes the placeholder so the post is fetched
            # again on the next run.
            ok, msg = get_engine().remove_tombstone(
                str(body.get("profile") or ""), str(body.get("file") or ""))
            return self._json({"ok": ok, "message": msg})

        if p == "/api/roots/add":
            ok, msg = get_engine().add_root(str(body.get("path") or ""))
            return self._json({"ok": ok, "message": msg})

        if p == "/api/roots/remove":
            ok, msg = get_engine().remove_root(str(body.get("path") or ""))
            return self._json({"ok": ok, "message": msg})

        if p == "/api/set-output-root":
            # Point downloads at a different folder. Body: {path, migrate}
            # migrate=true moves the existing archives across; false just
            # switches where new downloads go.
            ok, msg = get_engine().set_output_root(
                str(body.get("path") or ""), bool(body.get("migrate")))
            return self._json({"ok": ok, "message": msg})

        if p == "/api/gallery/move":
            # Favourite / trash / restore a single photo. Moves, never deletes.
            section = str(body.get("section") or "")
            if section not in ("main", "favorites", "trash"):
                return self._json({"ok": False, "error": "bad section"})
            ok, msg = get_engine().move_media(
                clean_profile_name(str(body.get("profile") or "")) or "",
                str(body.get("file") or ""),
                section)
            return self._json({"ok": ok, "message": msg})

        if p == "/api/clear-finished":
            return self._json({"removed": get_engine().clear_finished()})

        if p == "/api/retry-all":
            return self._json({"requeued": get_engine().retry_all_failed()})

        if p == "/api/pause":
            # Per platform: pausing Instagram (usually to sit out a block) must
            # not stop yt-dlp, which shares nothing with it.
            eng = get_engine()
            who = str(body.get("platform") or "instagram").strip().lower()
            eng.pause(who)
            return self._json({"ok": True, "platform": who})

        if p == "/api/resume":
            eng = get_engine()
            who = str(body.get("platform") or "instagram").strip().lower()
            eng.resume(who)
            return self._json({"ok": True, "platform": who})

        if p == "/api/start":
            # Start one platform's worker.
            return self._json(get_engine().start_platform(str(body.get("platform") or "")))

        if p == "/api/autostart":
            return self._json(set_autostart(bool(body.get("enabled"))))

        if p == "/api/cooldown/clear":
            # The countdown is a safety brake, not a lock: Instagram's own wait
            # is a guess, and someone who can see it has cleared (a fresh IP, a
            # long gap) should not have to wait out a timer the app invented.
            get_engine().clear_cooldown()
            return self._json({"ok": True})

        if p == "/api/check-block":
            blocked = get_engine().check_ip_block()
            return self._json({"blocked": blocked})

        if p == "/api/settings":
            return self._json({"settings": get_engine().update_settings(body.get("settings") or {}).to_dict()})

        if p == "/api/session/select":
            ok = get_engine().set_session(str(body.get("username") or ""))
            return self._json({"ok": ok})

        if p == "/api/session/forget":
            ok = get_engine().forget_session(str(body.get("username") or ""))
            return self._json({"ok": ok})

        if p == "/api/session/browsers":
            return self._json({"browsers": get_engine().browser_sessions()})

        if p == "/api/session/import-browser":
            return self._json(get_engine().import_from_browser(body.get("path") or None))

        if p == "/api/session/login":
            # Run instaloader's interactive login in the user's own terminal.
            username = clean_profile_name(str(body.get("username") or ""))
            if not username:
                return self._json({"ok": False, "error": "Enter a username first."})
            term = _find_terminal()
            if not term:
                return self._json({
                    "ok": False,
                    "error": "No terminal emulator found. Run manually: "
                             f"instaloader --login={username}",
                })
            try:
                subprocess.Popen(
                    term + [f"instaloader --login={username}; echo; "
                            f"echo 'Done - press Enter to close.'; read"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as e:
                return self._json({"ok": False, "error": f"Could not open terminal: {e}"})
            return self._json({"ok": True, "launched": True})

        if p == "/api/open-folder":
            name = clean_profile_name(body.get("profile", ""))
            plat = str(body.get("platform") or "instagram")
            if plat not in ("instagram", "tiktok"):
                plat = "instagram"
            eng = get_engine()
            if name:
                # Open the folder the profile is actually in, not just the
                # downloads folder -- it may live on another drive.
                target = eng.root_for(name, plat) or eng.output_root
            elif plat == "tiktok":
                # The whole TikTok tree, which sits BESIDE the downloads folder
                # rather than inside it, so opening the downloads folder never
                # shows it and the videos look like they went somewhere else.
                import engine as _eng
                target = _eng.tiktok_roots(eng.output_root)[0]
                try:
                    target.mkdir(parents=True, exist_ok=True)
                except OSError:
                    target = eng.output_root
            else:
                target = eng.output_root
            threading.Thread(target=_open_folder, args=(target,), daemon=True).start()
            return self._json({"ok": True, "path": str(target)})

        return self.send_error(404)


def _ensure_instaloader() -> bool:
    """Make sure instaloader is present, offering to install it if not.

    Returns True if it is (or becomes) available. Says plainly that the app
    will not work without it, and never installs anything without a yes.
    """
    import engine
    if engine.instaloader_available():
        return True

    print()
    print("instaloader is not installed.")
    print("This app is only a front end for it -- without instaloader,")
    print("nothing will download. It cannot work without it.")
    print()
    if not sys.stdin.isatty():
        print("Install it with one of:")
        print("    pipx install instaloader")
        print("    pip install --user instaloader")
        print()
        return False

    try:
        answer = input("Install instaloader now? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if answer not in ("", "y", "yes"):
        print("Skipped. Install it yourself with: pipx install instaloader")
        return False

    # Prefer pipx (isolated); fall back to pip --user. Try both, report honestly.
    for cmd in (["pipx", "install", "instaloader"],
                [sys.executable, "-m", "pip", "install", "--user", "instaloader"]):
        if shutil.which(cmd[0]) or cmd[0] == sys.executable:
            print(f"running: {' '.join(cmd)}")
            try:
                rc = subprocess.call(cmd)
            except OSError as e:
                print(f"could not run it: {e}")
                continue
            if rc == 0 and engine.instaloader_available():
                print("instaloader installed.")
                return True
    print()
    print("Automatic install did not succeed.")
    print("Please install instaloader yourself, then start this app again:")
    print("    pipx install instaloader")
    return False


def main() -> None:
    eng = get_engine()
    eng.output_root.mkdir(parents=True, exist_ok=True)
    _ensure_instaloader()
    # Clear interrupted-download leftovers from a previous run. Backgrounded so
    # housekeeping never delays the UI.
    eng.cleanup_partials_at_startup()
    # Resume queued work -- only when actually run as the server. Importing
    # this module must never start a worker against the user's real queue.
    eng.kick_if_work()

    global PORT
    want = PORT
    PORT = find_free_port(want, "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST)
    srv = QuietServer((HOST, PORT), Handler)
    url = f"http://{'127.0.0.1' if HOST in ('0.0.0.0', '::') else HOST}:{PORT}"
    print()
    print("=" * 56)
    print(f"  HardlyWatching is running at {url}")
    if PORT != want:
        print(f"  (port {want} was busy, so it used {PORT})")
    print("  It should have opened in your browser.")
    print("  If not, copy that address into your browser.")
    print(f"  Downloads land in: {eng.output_root}")
    print("  Press Ctrl-C here to stop.")
    print("=" * 56)
    if HOST in ("0.0.0.0", "::"):
        print(f"Reachable from other devices at http://<this-box-ip>:{PORT}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()