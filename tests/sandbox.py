"""A throwaway HardlyWatching environment, for tests only.

Every test that needs a running app gets one of these. It builds a temp tree,
points every path the app can write to inside it, shadows the two downloader
binaries with stubs that never touch the network, starts ``server.py`` on a
free port at or above 8899 and waits until ``GET /api/health`` answers.

    from . import sandbox

    with sandbox.Sandbox() as sb:
        assert sb.get("/api/health").json()["ok"] is True

What it guarantees, in order of importance:

1. **No real state.** ``HOME``, ``XDG_CONFIG_HOME``, ``XDG_CACHE_HOME``,
   ``INSTA_STATE``, ``INSTA_CACHE_DB``, ``INSTA_OUTPUT``, ``INSTA_THUMBS`` and
   ``INSTA_AVATARS`` all point inside the temp dir, so ``~/.config/hardlywatching``,
   ``~/instagram`` and the live ``state.json`` are unreachable even by accident.
   Anything inherited from the parent environment that starts with ``INSTA_`` is
   dropped rather than trusted.
2. **No network.** ``instaloader`` and ``yt-dlp`` on ``PATH`` are stubs written
   by this module; they exit 0 and write a fake profile folder. The real
   binaries are never resolved.
3. **Never port 8765**, which is the running service. The port is probed free
   from 8899 upward and 8765 is skipped outright.
4. **No orphans.** ``stop()`` kills the process group (the server spawns
   downloader children) and removes the tree; ``__exit__`` and ``__del__`` both
   call it, so a failing assertion cannot leave a listener behind.

Read-only helpers: ``.get()`` and ``.post()`` return a :class:`Response` even
for 4xx/5xx, so a test asserts on ``status`` instead of catching exceptions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SERVER_PY = REPO_ROOT / "server.py"

MIN_PORT = 8899
MAX_PORT_TRIES = 50
# The live service. Refusing it is not paranoia: starting a sandbox server on
# 8765 would either fail against the running app or, worse, talk to it.
FORBIDDEN_PORTS = {8765}
START_TIMEOUT = 20.0

# Instructions for whoever runs a failing test and wants to look at the tree.
KEEP_ENV = "HWA_SANDBOX_KEEP"

# A 16x16 JPEG, embedded so the stubs need no image library at all: the stub is
# started with whatever python the test used, and nothing else is guaranteed.
_TINY_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4p"
    "LSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09P"
    "T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAAQABADASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
    "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDAih9q"
    "uRQ+1TRQ+1W4ofas51RYWuf/2Q=="
)

_INSTALOADER_STUB = r'''
"""Sandbox stand-in for the instaloader CLI. Writes a fake profile, never networks.

Called the way engine.py calls the real one:
    instaloader --dirname-pattern=<profile dir> [flags] [--count N] <username>

Flags are ignored (they only choose what the real tool would fetch). What lands
on disk is a plausible archive folder: dated JPEGs, a JSON sidecar per post and
a profile picture, so the gallery, timeline and peek code has something to read.
"""
import base64
import json
import os
import sys
import time
from pathlib import Path

TINY_JPEG = base64.b64decode("""__JPEG__""")
# Flags whose next token is a value, not a positional argument.
VALUE_FLAGS = {"--slide", "--count", "--post-metadata-txt", "--stories-txt",
               "--tagged-txt", "--igtv-txt", "--sessionfile", "--user-agent"}


def main(argv):
    dirname = None
    count = 4
    names = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--dirname-pattern="):
            dirname = arg.split("=", 1)[1]
        elif arg == "--dirname-pattern" and i + 1 < len(argv):
            i += 1
            dirname = argv[i]
        elif arg in VALUE_FLAGS and i + 1 < len(argv):
            i += 1
            if arg == "--count":
                try:
                    count = int(argv[i])
                except ValueError:
                    pass
        elif arg.startswith("-"):
            pass
        else:
            names.append(arg)
        i += 1

    username = (names[0] if names else "").strip().lstrip("@")
    if not dirname:
        out = os.environ.get("INSTA_OUTPUT")
        if not out:
            print("Fatal: no --dirname-pattern and no INSTA_OUTPUT", file=sys.stderr)
            return 2
        dirname = str(Path(out) / (username or "sandbox_profile"))
    username = username or Path(dirname).name

    target = Path(dirname)
    target.mkdir(parents=True, exist_ok=True)
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    print("Using latest stamps from %s/instaloader/latest-stamps.ini." % config)
    print("Profile %s: %d posts" % (username, count))
    base = int(time.time()) - max(count, 1) * 86400
    for n in range(1, max(count, 0) + 1):
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S_UTC", time.localtime(base + n * 86400))
        media = target / (stamp + ".jpg")
        print("[%3d/%3d] %s" % (n, count, media))
        media.write_bytes(TINY_JPEG)
        media.with_suffix(media.suffix + ".json").write_text(json.dumps(
            {"node": {"id": "%s-%d" % (username, n), "date_utc": stamp[:19]}}))
    (target / ("%s_profile_pic.jpg" % username)).write_bytes(TINY_JPEG)
    print("Finished %s." % username)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

_YTDLP_STUB = r'''
"""Sandbox stand-in for yt-dlp. Prints plausible progress, downloads nothing."""
import sys


def main(argv):
    url = argv[-1] if argv else "sandbox"
    print("[youtube] Extracting URL: %s" % url)
    print("[youtube] %s: Downloading webpage" % url)
    print("[download] 100% of 1.00KiB in 00:00")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


class Response:
    """One HTTP answer, readable whether the status was 200 or 500."""

    __slots__ = ("status", "headers", "body", "url")

    def __init__(self, status: int, headers: dict, body: bytes, url: str):
        self.status = status
        # Lower-cased so a test can ask for "content-type" without guessing.
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.body = body
        self.url = url

    def json(self):
        return json.loads(self.body.decode("utf-8") or "null")

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    def __repr__(self) -> str:
        return "<Response %s %s %d bytes>" % (self.status, self.url, len(self.body))


def _free_port(start: int = MIN_PORT) -> int:
    """First port at or above `start` that can be bound on loopback.

    Same shape as server.py's find_free_port(), so the sandbox and the server
    agree about what "free" means. The socket is closed immediately; the window
    between the probe and the server's own bind is a few milliseconds and a
    collision would show up as a startup failure, not as a silent wrong port.
    """
    for port in range(start, start + MAX_PORT_TRIES):
        if port in FORBIDDEN_PORTS:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("no free port between %d and %d" % (start, start + MAX_PORT_TRIES))


class Sandbox:
    """A temp HOME, stub downloaders and a server subprocess on its own port."""

    def __init__(self, *, seed_profile: str | None = "sandbox_seed",
                 seed_count: int = 3, port: int | None = None,
                 keep: bool | None = None):
        if port is not None and port in FORBIDDEN_PORTS:
            raise ValueError("refusing to use port %d: that is the live service" % port)
        self.keep = (os.environ.get(KEEP_ENV) == "1") if keep is None else keep
        self.root = Path(tempfile.mkdtemp(prefix="hwa-sandbox-"))
        self.bin_dir = self.root / "bin"
        self.home = self.root / "home"
        self.output = self.root / "instagram"
        self.thumbs = self.root / "thumbs"
        self.avatars = self.root / "avatars"
        self.state_file = self.root / "state.json"
        self.cache_db = self.root / "gallery-cache.db"
        self.log_path = self.root / "server.log"
        self.port = port
        self._proc: subprocess.Popen | None = None
        self._log = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._seed_profile = seed_profile
        self._seed_count = seed_count
        self._started = False

        for d in (self.bin_dir, self.home, self.output, self.thumbs, self.avatars):
            d.mkdir(parents=True, exist_ok=True)
        self._write_stubs()

    # ------------------------------------------------------------------ setup

    def _write_stubs(self) -> None:
        """Two executables that shadow the real downloaders on PATH."""
        shebang = "#!%s\n" % sys.executable
        for name, body in (("instaloader", _INSTALOADER_STUB),
                           ("yt-dlp", _YTDLP_STUB)):
            path = self.bin_dir / name
            text = shebang + body.replace("__JPEG__", _TINY_JPEG_B64)
            path.write_text(text)
            path.chmod(0o755)

    @property
    def env(self) -> dict:
        """The full environment the server runs with."""
        # Inherited INSTA_* settings would otherwise leak a real path or port in.
        env = {k: v for k, v in os.environ.items() if not k.startswith("INSTA_")}
        for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
                    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            env.pop(key, None)
        env.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "INSTA_STATE": str(self.state_file),
            "INSTA_CACHE_DB": str(self.cache_db),
            "INSTA_OUTPUT": str(self.output),
            "INSTA_THUMBS": str(self.thumbs),
            "INSTA_AVATARS": str(self.avatars),
            "INSTA_HOST": "127.0.0.1",
            "INSTA_PORT": str(self.port or MIN_PORT),
            # server.py calls webbrowser.open() at startup; BROWSER=/bin/true
            # makes it a no-op instead of throwing a tab at the desktop.
            "BROWSER": "/bin/true",
            "PATH": str(self.bin_dir) + os.pathsep + env.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
        })
        return env

    def seed_profile(self, name: str, count: int = 3) -> Path:
        """Fill one archive folder by running the instaloader stub directly.

        Gives the read-only routes a profile to describe without depending on a
        job having run, so gallery/timeline/peek assertions are stable.
        """
        target = self.output / name
        env = self.env
        result = subprocess.run(
            [str(self.bin_dir / "instaloader"), "--dirname-pattern=%s" % target,
             "--count", str(count), name],
            env=env, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError("seed stub failed (%d): %s"
                               % (result.returncode, result.stderr[-2000:]))
        return target

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> "Sandbox":
        if self._started:
            return self
        if not SERVER_PY.is_file():
            raise RuntimeError("server.py not found at %s" % SERVER_PY)
        if self._seed_profile:
            self.seed_profile(self._seed_profile, self._seed_count)
        if self.port is None:
            self.port = _free_port(MIN_PORT)

        self._log = open(self.log_path, "wb")
        env = self.env
        self._proc = subprocess.Popen(
            [sys.executable, str(SERVER_PY)],
            cwd=str(REPO_ROOT), env=env, stdout=self._log,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            # Own process group: the server spawns instaloader/yt-dlp children,
            # and stop() has to take those down too.
            start_new_session=True,
        )
        self._started = True
        try:
            self._wait_healthy()
        except Exception:
            self.stop()
            raise
        return self

    def _wait_healthy(self, timeout: float = START_TIMEOUT) -> None:
        """Wait until *our* server answers, not just anything on this port.

        A `/api/health` 200 is not proof of ownership: another instance (another
        worktree's test run, or the live service if someone pointed it at 8765)
        would answer the same. So the answer also has to carry this sandbox's
        archive root. That check is what stops a test run from silently talking
        to a stranger's server and then failing when the stranger exits.
        """
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(
                    "server exited with %s before answering /api/health\n%s"
                    % (self._proc.returncode, self._log_tail()))
            # server.py calls find_free_port() itself, so if another process
            # grabbed our port between the probe and its bind it announces a
            # different one. Follow it rather than failing on the wrong port.
            announced = self._announced_port()
            if announced and announced != self.port:
                self.port = announced
            try:
                if self._answers_as_mine():
                    return
                last = "health ok but not this sandbox"
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = e
            time.sleep(0.1)
        raise RuntimeError("server did not answer /api/health on %s in %.0fs (last: %r)\n%s"
                           % (self.base_url, timeout, last, self._log_tail()))

    def _answers_as_mine(self) -> bool:
        """Health 200 and /api/state reporting this sandbox's archive root."""
        health = self.get("/api/health", timeout=1.0)
        if health.status != 200 or health.json().get("ok") is not True:
            return False
        state = self.get("/api/state", timeout=5.0)
        if state.status != 200:
            return False
        return state.json().get("output_root") == str(self.output)

    def _announced_port(self) -> int | None:
        """The port the server printed, if it has printed one yet."""
        match = re.search(r"running at http://127\.0\.0\.1:(\d+)", self._log_tail(2000))
        return int(match.group(1)) if match else None

    def _log_tail(self, limit: int = 4000) -> str:
        try:
            return self.log_path.read_text(errors="replace")[-limit:]
        except OSError:
            return "<no log>"

    def stop(self) -> None:
        """Kill the server and its children, then drop the tree."""
        proc, self._proc = self._proc, None
        self._started = False
        if proc is not None:
            pgid = proc.pid  # == the group id, because of start_new_session
            try:
                if proc.poll() is None:
                    # Signal the GROUP while the leader is still unreaped: the
                    # group id is provably ours at this moment, so this cannot
                    # reach an unrelated process that later reused the pid.
                    try:
                        os.killpg(pgid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError, OSError):
                        proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                for stream in (proc.stdout, proc.stderr):
                    try:
                        if stream:
                            stream.close()
                    except OSError:
                        pass
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None
        if not self.keep:
            shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "Sandbox":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def __del__(self) -> None:
        try:
            self.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------ client

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % (self.port or MIN_PORT)

    def request(self, method: str, path: str, body=None, timeout: float = 20.0) -> Response:
        url = self.base_url + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return Response(resp.status, dict(resp.headers), resp.read(), url)
        except urllib.error.HTTPError as e:
            # 4xx/5xx is an answer, not an exception: the test decides.
            try:
                payload = e.read()
            finally:
                e.close()
            return Response(e.code, dict(e.headers or {}), payload, url)

    def get(self, path: str, timeout: float = 20.0) -> Response:
        return self.request("GET", path, timeout=timeout)

    def post(self, path: str, body=None, timeout: float = 30.0) -> Response:
        return self.request("POST", path, body if body is not None else {}, timeout=timeout)

    def post_raw(self, path: str, data: bytes, content_type: str = "application/zip",
                 timeout: float = 60.0) -> Response:
        """POST bytes exactly as given -- for the backup zip, which is not JSON."""
        url = self.base_url + path
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": content_type,
                                              "Content-Length": str(len(data))})
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return Response(resp.status, dict(resp.headers), resp.read(), url)
        except urllib.error.HTTPError as e:
            try:
                payload = e.read()
            finally:
                e.close()
            return Response(e.code, dict(e.headers or {}), payload, url)


def _check() -> int:
    """`python -m tests.sandbox --check`: prove the sandbox works, then clean up."""
    with Sandbox() as sb:
        health = sb.get("/api/health")
        if health.status != 200 or health.json().get("ok") is not True:
            print("FAIL health=%s body=%r" % (health.status, health.body[:300]))
            return 1
        state = sb.get("/api/state")
        if state.status != 200:
            print("FAIL state=%s body=%r" % (state.status, state.body[:300]))
            return 1
        print("OK port=%d state=%s" % (sb.port, sb.state_file))
    return 0


if __name__ == "__main__":
    sys.exit(_check())
