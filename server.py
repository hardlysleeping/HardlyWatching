"""Local web server for the Instaloader queue UI.

Run:  python3 server.py
Then open http://127.0.0.1:8765

Environment:
  INSTA_PORT    port to bind (default 8765)
  INSTA_OUTPUT  where downloads go (default ~/instagram)
  INSTA_STATE   where app state is kept (default ~/.config/insta-desktop/state.json)
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from engine import Engine, OUTPUT_ROOT, clean_profile_name

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("INSTA_PORT", "8765"))
HOST = "127.0.0.1"

engine = Engine()
engine.kick_if_work()  # resume queued work after a service restart


def _open_folder(path: Path) -> None:
    if not path.exists():
        return
    for cmd in (["xdg-open", str(path)], ["gio", "open", str(path)]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except FileNotFoundError:
            continue


def _changed(job_id: str, old: dict) -> dict:
    """Diff a job against the copy the browser last saw. Returns only changed keys."""
    return old


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

    def _serve_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------- routes ----------

    def do_GET(self) -> None:
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            return self._serve_file(HERE / "index.html")
        if p == "/api/state":
            # The UI polls this every ~1.2s, so it doubles as a worker watchdog:
            # if the worker died with work still queued, revive it.
            engine.watchdog()
            return self._json(engine.snapshot())
        if p == "/api/profile-pic":
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            name = clean_profile_name((q.get("profile") or [""])[0])
            if not name:
                return self.send_error(400)
            d = OUTPUT_ROOT / name
            if not d.is_dir():
                return self.send_error(404)
            # instaloader names these <user>_<date>_UTC_profile_pic.<ext>
            hits = sorted(d.glob("*_profile_pic.*"),
                          key=lambda x: x.stat().st_mtime, reverse=True)
            if not hits:
                return self.send_error(404)
            return self._serve_file(hits[0])
        if p == "/api/health":
            return self._json({"ok": True})
        return self.send_error(404)

    def do_POST(self) -> None:
        p = urlparse(self.path).path
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
            added, rejected = engine.add(names, overrides)
            return self._json({"added": [j.username for j in added], "rejected": rejected})

        if p == "/api/remove":
            return self._json({"ok": engine.remove(body.get("id", ""))})

        if p == "/api/forget":
            return self._json({"ok": engine.forget(body.get("id", ""))})

        if p == "/api/restore":
            return self._json({"ok": engine.restore(body.get("id", ""))})

        if p == "/api/restore-all":
            return self._json({"restored": engine.restore_all()})

        if p == "/api/purge-removed":
            return self._json({"purged": engine.purge_removed()})

        if p == "/api/requeue":
            return self._json({
                "ok": engine.requeue(body.get("id", ""), bool(body.get("full")))
            })

        if p == "/api/move":
            jid = body.get("id", "")
            if "index" in body:
                ok = engine.move_to(jid, int(body["index"]))
            else:
                ok = engine.move(jid, int(body.get("delta", 0)))
            return self._json({"ok": ok})

        if p == "/api/clear-finished":
            return self._json({"removed": engine.clear_finished()})

        if p == "/api/retry-all":
            return self._json({"requeued": engine.retry_all_failed()})

        if p == "/api/pause":
            engine.pause()
            return self._json({"ok": True})

        if p == "/api/resume":
            engine.resume()
            return self._json({"ok": True})

        if p == "/api/cooldown/clear":
            engine.clear_cooldown()
            return self._json({"ok": True})

        if p == "/api/check-block":
            blocked = engine.check_ip_block()
            return self._json({"blocked": blocked})

        if p == "/api/settings":
            return self._json({"settings": engine.update_settings(body.get("settings") or {}).to_dict()})

        if p == "/api/session/select":
            ok = engine.set_session(str(body.get("username") or ""))
            return self._json({"ok": ok})

        if p == "/api/session/forget":
            ok = engine.forget_session(str(body.get("username") or ""))
            return self._json({"ok": ok})

        if p == "/api/session/refresh":
            return self._json({"sessions": engine.list_sessions()})

        if p == "/api/session/browsers":
            return self._json({"browsers": engine.browser_sessions()})

        if p == "/api/session/import-browser":
            return self._json(engine.import_from_browser(body.get("path") or None))

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
            target = (OUTPUT_ROOT / name) if name else OUTPUT_ROOT
            threading.Thread(target=_open_folder, args=(target,), daemon=True).start()
            return self._json({"ok": True, "path": str(target)})

        return self.send_error(404)


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Instaloader queue UI running at {url}")
    print(f"Downloads land in: {OUTPUT_ROOT}")
    print("Ctrl-C to stop.")
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
