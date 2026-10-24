"""Single-instance protection, --attach, --no-browser and /api/version.

Deliberately self-contained: temp HOME, port 8899, stdlib unittest only. It does
not import `tests/sandbox.py` (it may not exist yet) and it never touches the
real ~/.config/hardlywatching, the real archive or the live service on 8765.

Run from the repo root:

    python3 -m unittest tests.test_instance_lock -v
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "server.py"
PY = sys.executable
HOST = "127.0.0.1"
# Read the version instead of hard-coding it: it is bumped on every release (and by
# scripts/sync-upstream.sh from the upstream tag), and a literal here turns each bump
# into four red tests that say nothing about the lock.
try:
    EXPECTED_VERSION = (REPO / "VERSION").read_text(encoding="utf-8").strip() or "dev"
except OSError:
    EXPECTED_VERSION = "dev"

# A pid that cannot exist: /proc/sys/kernel/pid_max is the exclusive upper bound.
DEAD_PID = 999999


def wait_until(predicate, timeout: float = 30.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def port_is_free(port: int | None = None) -> bool:
    port = PORT if port is None else port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def listening_entries(port: int | None = None) -> int | None:
    """How many sockets are LISTENing on `port`, from /proc. None if unknown."""
    port = PORT if port is None else port
    table = Path("/proc/net/tcp")
    if not table.exists():
        return None
    count = 0
    for line in table.read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A":   # 0A == TCP_LISTEN
            continue
        try:
            local_port = int(fields[1].rsplit(":", 1)[1], 16)
        except (IndexError, ValueError):
            continue
        if local_port == port:
            count += 1
    return count


def read_json_url(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read()


def _pick_port(default: int = 8899, span: int = 40) -> int:
    """8899, or the next free port when something else already holds it.

    The sandbox port has to be one this suite owns. A sibling worktree's sandbox
    can be sitting on 8899, and measuring that stranger's server would make every
    assertion here meaningless -- so fall to the next free port and say so.
    `HWA_TEST_PORT` forces an exact port instead.
    """
    override = os.environ.get("HWA_TEST_PORT")
    if override:
        return int(override)
    for candidate in range(default, default + span):
        if port_is_free(candidate):
            if candidate != default:
                print(f"note: port {default} is in use, "
                      f"running the sandbox on {candidate}")
            return candidate
    return default


PORT = _pick_port()
URL = f"http://{HOST}:{PORT}"


class ServerCase(unittest.TestCase):
    """Owns one throwaway HOME per test and cleans up every process it starts."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hwa-lock-test-"))
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.addCleanup(self._stop_all)
        self.env = self._make_env()
        # Refuse to run against somebody else's listener: a foreign server on
        # this port that answers /api/health would make every assertion below a
        # statement about the wrong process.
        if not port_is_free(PORT):
            self.fail(f"port {PORT} is already in use, so these tests could "
                      f"measure the wrong server; re-run with "
                      f"HWA_TEST_PORT=<free port>")

    # ---------- sandbox ----------

    def _make_env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("INSTA_")}
        for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            env.pop(key, None)
        env.update({
            "HOME": str(self.tmp),
            "INSTA_STATE": str(self.tmp / "state.json"),
            "INSTA_CACHE_DB": str(self.tmp / "gallery-cache.db"),
            "INSTA_OUTPUT": str(self.tmp / "instagram"),
            "INSTA_THUMBS": str(self.tmp / "thumbs"),
            "INSTA_AVATARS": str(self.tmp / "avatars"),
            "INSTA_PORT": str(PORT),
            "INSTA_HOST": HOST,
            "BROWSER": "/bin/true",
        })
        env.update(extra)
        return env

    @property
    def lock_file(self) -> Path:
        return self.tmp / "run" / "hardlywatching.lock"

    def _lock_owner(self, lock: Path | None = None) -> int | None:
        lock = self.lock_file if lock is None else Path(lock)
        try:
            pid = int(json.loads(lock.read_text()).get("pid") or 0)
        except Exception:
            return None
        return pid or None

    # ---------- processes ----------

    def start(self, *args: str, env: dict | None = None,
              owns_lock: bool = True, lock: Path | None = None) -> subprocess.Popen:
        proc = subprocess.Popen(
            [PY, str(SERVER), *args], cwd=str(REPO), env=env or self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        self.procs.append(proc)
        # Ready means *this* process answering -- its lock record plus a healthy
        # probe -- not just "something on the port answers".
        ready = wait_until(
            lambda: proc.poll() is not None
            or (self.health() and (not owns_lock
                                   or self._lock_owner(lock) == proc.pid)),
            timeout=120)
        if not ready or proc.poll() is not None:
            self.fail("server never came up (or died); output:\n" + self._drain(proc))
        return proc

    def run_client(self, *args: str, env: dict | None = None,
                   timeout: float = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PY, str(SERVER), *args], cwd=str(REPO), env=env or self.env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout)

    def start_unlocked(self, ports: list[int]):
        """Start a --force instance; return (proc, the port it landed on)."""
        proc = subprocess.Popen(
            [PY, str(SERVER), "--force"], cwd=str(REPO), env=self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        self.procs.append(proc)
        found: dict[str, int] = {}

        def ready() -> bool:
            if proc.poll() is not None:
                return True
            for port in ports:
                if self.status("/api/health", port) == 200:
                    found["port"] = port
                    return True
            return False

        if not wait_until(ready, timeout=120) or proc.poll() is not None:
            self.fail(f"forced server never answered on {ports}; output:\n"
                      + self._drain(proc))
        return proc, found["port"]

    def stop(self, proc: subprocess.Popen, sig=signal.SIGINT) -> int:
        if proc.poll() is None:
            proc.send_signal(sig)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
        return proc.returncode

    def _drain(self, proc: subprocess.Popen) -> str:
        try:
            if proc.poll() is not None and proc.stdout is not None:
                return proc.stdout.read()
        except Exception:
            pass
        return ""

    def _stop_all(self) -> None:
        if not self.procs:
            return
        for proc in self.procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        for proc in self.procs:
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass
        wait_until(lambda: port_is_free(PORT), timeout=30)

    # ---------- http ----------

    def status(self, path: str, port: int | None = None) -> int | None:
        try:
            return read_json_url(f"http://{HOST}:{port or PORT}{path}")[0]
        except Exception:
            return None

    def health(self) -> bool:
        return self.status("/api/health") == 200

    def json_get(self, path: str):
        status, body = read_json_url(URL + path)
        return status, json.loads(body)


class SecondStartTests(ServerCase):
    """A second start must not become a second server."""

    def test_second_instance_exits_zero_without_binding(self) -> None:
        first = self.start()

        self.assertTrue(self.lock_file.exists(), "no lock written by the first start")
        record = json.loads(self.lock_file.read_text())
        self.assertEqual(set(record), {"pid", "port", "started", "version"})
        self.assertEqual(record["pid"], first.pid)
        self.assertEqual(record["port"], PORT)
        self.assertEqual(record["version"], EXPECTED_VERSION)
        self.assertTrue(record["started"])

        started = time.time()
        second = self.run_client()
        elapsed = time.time() - started

        self.assertEqual(second.returncode, 0,
                         f"second start exited {second.returncode}:\n{second.stdout}{second.stderr}")
        self.assertIn(f"HardlyWatching is already running at {URL}", second.stdout)
        # It stopped there: no banner, no port shuffle, no engine work.
        self.assertNotIn("Downloads land in", second.stdout)
        self.assertNotIn("was busy, so it used", second.stdout)
        self.assertLess(elapsed, 30, f"second start took {elapsed:.1f}s")

        # The lock still belongs to the first process and nothing else bound.
        self.assertEqual(json.loads(self.lock_file.read_text())["pid"], first.pid)
        self.assertIsNone(first.poll(), "the first instance died")
        self.assertEqual(listening_entries(PORT), 1)
        self.assertFalse(port_is_free(PORT), "a second socket could still bind the port")
        self.assertEqual(self.status("/api/health"), 200)

    def test_force_starts_anyway(self) -> None:
        first = self.start()
        # --force deliberately starts a second server on the next free port
        # instead of exiting, which is the old find_free_port() behaviour, now
        # only reachable on request.
        forced, side = self.start_unlocked([PORT + 1, PORT + 2, PORT + 3])
        try:
            self.assertIsNone(forced.poll())
            self.assertNotEqual(side, PORT)
            self.assertEqual(self.status("/api/health", side), 200)
            # It does not steal the lock: the canonical instance is still first.
            self.assertEqual(self._lock_owner(), first.pid)
            self.assertEqual(self.status("/api/health"), 200)
        finally:
            self.stop(forced)
        self.assertEqual(self._lock_owner(), first.pid)

    def test_stale_lock_from_dead_pid_does_not_block(self) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file.write_text(json.dumps({
            "pid": DEAD_PID, "port": PORT,
            "started": "2020-01-01T00:00:00+00:00", "version": "0.0.1"}))

        first = self.start()

        record = json.loads(self.lock_file.read_text())
        self.assertEqual(record["pid"], first.pid)
        self.assertEqual(record["version"], EXPECTED_VERSION)
        self.assertEqual(self.status("/api/health"), 200)

    def test_unreadable_lock_does_not_block(self) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file.write_text("{ this is not json")

        first = self.start()

        self.assertEqual(json.loads(self.lock_file.read_text())["pid"], first.pid)

    def test_lock_is_removed_on_keyboard_interrupt(self) -> None:
        first = self.start()
        self.assertTrue(self.lock_file.exists())

        code = self.stop(first)

        self.assertEqual(code, 0, "Ctrl-C should be a clean exit")
        self.assertFalse(self.lock_file.exists(), "lock survived a clean shutdown")
        self.assertTrue(wait_until(lambda: port_is_free(PORT), timeout=30))


class AttachTests(ServerCase):

    def test_attach_prints_url_and_exits_zero(self) -> None:
        self.start()
        out = self.run_client("--attach")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.strip(), URL)

    def test_attach_with_nothing_running_exits_one(self) -> None:
        out = self.run_client("--attach")
        self.assertEqual(out.returncode, 1)
        self.assertIn("not running", out.stdout.lower())
        # --attach starts nothing.
        self.assertTrue(port_is_free(PORT))
        self.assertEqual(listening_entries(PORT) or 0, 0)
        self.assertFalse(self.lock_file.exists())

    def test_attach_ignores_a_stale_lock(self) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file.write_text(json.dumps({
            "pid": DEAD_PID, "port": PORT,
            "started": "2020-01-01T00:00:00+00:00", "version": "0.0.1"}))
        out = self.run_client("--attach")
        self.assertEqual(out.returncode, 1)
        self.assertIn("not running", out.stdout.lower())


class BrowserTests(ServerCase):
    """--no-browser must skip webbrowser.open, proven by patching it."""

    WRAPPER = (
        "import os, pathlib, sys\n"
        "sys.path.insert(0, {repo!r})\n"
        "import server\n"
        "marker = pathlib.Path(os.environ['HWA_BROWSER_MARKER'])\n"
        "def fake_open(url, *a, **k):\n"
        "    marker.write_text(url)\n"
        "    return True\n"
        "server.webbrowser.open = fake_open\n"
        "sys.exit(server.main())\n"
    )

    def _start_wrapped(self, *args: str) -> subprocess.Popen:
        marker = self.tmp / "browser-opened.txt"
        env = dict(self.env, HWA_BROWSER_MARKER=str(marker))
        proc = subprocess.Popen(
            [PY, "-c", self.WRAPPER.format(repo=str(REPO)), *args],
            cwd=str(REPO), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(proc)
        ready = wait_until(
            lambda: proc.poll() is not None
            or (self.health() and self._lock_owner() == proc.pid), timeout=120)
        if not ready or proc.poll() is not None:
            self.fail("wrapped server never came up:\n" + self._drain(proc))
        return proc

    def test_browser_opens_by_default(self) -> None:
        # Control for the test below: without the patch proving it can fire, the
        # assertion there would pass for the wrong reason.
        self._start_wrapped()
        marker = self.tmp / "browser-opened.txt"
        self.assertTrue(wait_until(lambda: marker.exists(), timeout=15),
                        "patched webbrowser.open never ran, so the flag test proves nothing")
        self.assertEqual(marker.read_text().strip(), URL)

    def test_no_browser_skips_webbrowser_open(self) -> None:
        self._start_wrapped("--no-browser")
        marker = self.tmp / "browser-opened.txt"
        time.sleep(1.0)   # it would have fired before serve_forever
        self.assertFalse(marker.exists(), "--no-browser still opened a browser")
        self.assertEqual(self.status("/api/health"), 200)


class VersionTests(ServerCase):

    def test_version_endpoint(self) -> None:
        first = self.start()
        status, data = self.json_get("/api/version")

        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"version", "pid", "port", "started", "api"})
        self.assertEqual(data["version"], EXPECTED_VERSION)
        self.assertEqual(data["api"], 1)
        self.assertEqual(data["pid"], first.pid)
        self.assertEqual(data["port"], PORT)
        self.assertTrue(data["started"])

    def test_version_matches_version_file(self) -> None:
        self.assertEqual((REPO / "VERSION").read_text().strip(), EXPECTED_VERSION)
        self.start()
        _, data = self.json_get("/api/version")
        self.assertEqual(data["version"], EXPECTED_VERSION)

    def test_version_falls_back_to_dev_without_the_file(self) -> None:
        # A copy of the tree without VERSION -- the server must say "dev" rather
        # than fail. Only server.py, engine.py and the static files are needed.
        copy = self.tmp / "tree"
        copy.mkdir()
        shutil.copy2(SERVER, copy / "server.py")
        for extra in ("engine.py", "shorts.py", "profile_db.py", "tiktok_engine.py",
                      "index.html", "shorts.html"):
            src = REPO / extra
            if src.exists():
                shutil.copy2(src, copy / extra)
        if (REPO / "tools").exists():
            shutil.copytree(REPO / "tools", copy / "tools")
        self.assertFalse((copy / "VERSION").exists())

        env = dict(self.env)
        proc = subprocess.Popen([PY, str(copy / "server.py")], cwd=str(copy), env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        self.procs.append(proc)
        ready = wait_until(
            lambda: proc.poll() is not None
            or (self.health() and self._lock_owner() == proc.pid), timeout=120)
        if not ready or proc.poll() is not None:
            self.fail("copied tree never came up:\n" + self._drain(proc))
        _, data = self.json_get("/api/version")
        self.assertEqual(data["version"], "dev")


class LockPathTests(ServerCase):
    """Where the lock lives: beside the state file, or wherever INSTA_LOCK says."""

    def test_default_lock_is_beside_the_state_file(self) -> None:
        # No INSTA_STATE / INSTA_LOCK: HOME decides, which is ~/.config on Linux.
        env = {k: v for k, v in self.env.items() if k not in ("INSTA_STATE", "INSTA_LOCK")}
        expected = self.tmp / ".config" / "hardlywatching" / "run" / "hardlywatching.lock"

        proc = self.start(env=env, lock=expected)

        self.assertTrue(expected.exists(), f"no lock at {expected}")
        record = json.loads(expected.read_text())
        self.assertEqual(record["pid"], proc.pid)
        self.assertEqual(record["port"], PORT)
        # Nothing was written beside a state file that does not exist up there.
        self.assertFalse((self.tmp / "run" / "hardlywatching.lock").exists())

    def test_insta_lock_overrides_the_path(self) -> None:
        custom = self.tmp / "elsewhere" / "mylock.json"
        env = dict(self.env, INSTA_LOCK=str(custom))

        proc = self.start(env=env, lock=custom)

        self.assertTrue(custom.exists())
        self.assertEqual(self._lock_owner(custom), proc.pid)
        self.assertFalse(self.lock_file.exists())
        # The same override is honoured by the refusal path.
        out = self.run_client(env=env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("already running", out.stdout)


class ExistingRoutesTests(ServerCase):
    """The additions must not disturb what the UI already calls."""

    def test_pages_still_serve(self) -> None:
        self.start()
        for path in ("/", "/shorts", "/api/health"):
            self.assertEqual(self.status(path), 200, path)

    def test_state_still_answers_with_its_keys(self) -> None:
        self.start()
        status, snap = self.json_get("/api/state")
        self.assertEqual(status, 200)
        self.assertIsInstance(snap, dict)
        for key in ("current", "paused", "queued", "roots", "output_root"):
            self.assertIn(key, snap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
