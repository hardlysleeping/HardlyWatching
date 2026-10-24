#!/usr/bin/env python3
"""Throwaway demo instance builder for the UI baseline. Not the capture itself.

Everything lives under DEMO_ROOT (a scratch dir), every path the app can write
to points inside it, and the downloader binaries on PATH are stubs. Nothing
here may touch the live checkout, the live state or the service on 8765.
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
import time
import urllib.request
from pathlib import Path

DEMO_ROOT = Path(os.environ.get(
    "HWA_BASELINE_ROOT",
    Path.home() / ".hermes/cache/scratch/hwa-baseline"))
PORT = int(os.environ.get("HWA_BASELINE_PORT", "8899"))
FORBIDDEN_PORTS = {8765}

PERSONAS = [
    # (handle, kind, posts) -- demo names only, never a real account
    ("demo.coral.reef", "instagram", 6),
    ("demo.pixel.garden", "instagram", 5),
    ("demo.slow.mornings", "instagram", 4),
]
QUEUED = [("demo.quiet.harbor", "instagram"), ("demo.tin.robot", "tiktok")]
FINISHED_AT = 1761000000.0  # fixed epoch: cards show a stable date

# The downloader stub lives next to this file so the capture runs from any checkout;
# DEMO_STUB overrides it with another copy of the same script.
STUB = Path(os.environ.get("DEMO_STUB")
            or Path(__file__).resolve().parent / "demo-instaloader-stub.py")


def env() -> dict:
    e = {k: v for k, v in os.environ.items() if not k.startswith("INSTA_")}
    for k in list(e):
        if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            e.pop(k, None)
    e.update({
        "HOME": str(DEMO_ROOT / "home"),
        "XDG_CONFIG_HOME": str(DEMO_ROOT / "config"),
        "XDG_CACHE_HOME": str(DEMO_ROOT / "cache"),
        "INSTA_STATE": str(DEMO_ROOT / "config/hardlywatching/state.json"),
        "INSTA_CACHE_DB": str(DEMO_ROOT / "config/hardlywatching/gallery-cache.db"),
        "INSTA_OUTPUT": str(DEMO_ROOT / "instagram"),
        "INSTA_THUMBS": str(DEMO_ROOT / "cache/thumbs"),
        "INSTA_AVATARS": str(DEMO_ROOT / "cache/avatars"),
        "INSTA_HOST": "127.0.0.1",
        "INSTA_PORT": str(PORT),
        "PATH": str(DEMO_ROOT / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "BROWSER": "/bin/true",
        "PYTHONHASHSEED": "0",   # same stub image content every rebuild
        "PYTHONUNBUFFERED": "1",
    })
    return e


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def build() -> None:
    if PORT in FORBIDDEN_PORTS or PORT < 8899:
        raise SystemExit("refusing port %d" % PORT)
    if DEMO_ROOT.exists():
        shutil.rmtree(DEMO_ROOT)
    for d in ("bin", "config/hardlywatching", "cache/thumbs", "cache/avatars",
              "instagram", "home"):
        (DEMO_ROOT / d).mkdir(parents=True, exist_ok=True)

    # The downloader stub is copied verbatim, never rewritten: it is the file
    # the UI's progress parser was tested against.
    shutil.copy2(STUB, DEMO_ROOT / "bin/instaloader")
    (DEMO_ROOT / "bin/instaloader").chmod(0o755)
    (DEMO_ROOT / "bin/yt-dlp").write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        'print("[youtube] Extracting URL: demo")\n'
        'print("[download] 100% of 1.00KiB in 00:00")\n'
        "sys.exit(0)\n")
    (DEMO_ROOT / "bin/yt-dlp").chmod(0o755)

    e = env()
    for handle, _plat, count in PERSONAS:
        out = subprocess.run(
            [str(DEMO_ROOT / "bin/instaloader"),
             "--dirname-pattern=%s" % (DEMO_ROOT / "instagram" / handle),
             "--count", str(count), handle],
            env=e, capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            raise SystemExit("stub failed for %s: %s" % (handle, out.stderr[-800:]))

    _decorate_archive()
    _write_state()


def _decorate_archive() -> None:
    """Real portrait clips (the stub's mp4s are placeholders) + profile pictures."""
    import subprocess as sp
    root = DEMO_ROOT / "instagram"
    for p in list(root.rglob("*.mp4")) + list(root.rglob("*.mp4.json")):
        p.unlink()
    clips = [("demo.coral.reef", "2026-10-22_09-00-00_UTC"),
             ("demo.coral.reef", "2026-10-21_09-00-00_UTC"),
             ("demo.coral.reef", "2026-10-20_09-00-00_UTC")]
    for handle, stem in clips:
        d = root / handle
        sp.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
                "-i", "testsrc=size=360x640:rate=12", "-t", "1",
                "-pix_fmt", "yuv420p", "-c:v", "libx264", str(d / (stem + ".mp4"))],
               check=True, timeout=120)
        (d / (stem + ".mp4.json")).write_text(json.dumps(
            {"node": {"id": "%s-%s" % (handle, stem), "is_video": True}}))

    from PIL import Image, ImageDraw
    for i, d in enumerate(sorted(p for p in root.iterdir() if p.is_dir())):
        img = Image.new("RGB", (200, 200), [(34, 62, 84), (72, 44, 92), (44, 82, 64)][i % 3])
        ImageDraw.Draw(img).ellipse([50, 50, 150, 150], outline=(232, 232, 238), width=4)
        img.save(d / ("%s_profile_pic.jpg" % d.name), "JPEG", quality=80)


def _job(handle, status, platform="instagram", **kw) -> dict:
    j = {
        "username": handle, "id": "job-" + handle.replace(".", "-"),
        "status": status, "kind": "main", "platform": platform,
        "tiktok_count": 5, "private": False, "tiktok_total": 0,
        "parent_id": None, "root": "", "attempts": 1 if status != "queued" else 0,
        "max_attempts": 6, "added_at": FINISHED_AT - 1800,
        "started_at": None, "finished_at": None, "message": "",
        "posts": 0, "error": "", "tail": [], "total_files": 0, "total_bytes": 0,
        "settings": None, "last_run_seconds": 0.0, "retry_after": 0,
        "done_count": 0, "total_count": 0, "last_line": "", "heal_attempts": 0,
        "auto_retries": 0, "next_retry_at": 0.0, "fatal": False,
    }
    j.update(kw)
    return j


def _write_state() -> None:
    jobs = []
    for i, (handle, _plat, count) in enumerate(PERSONAS):
        size = sum(p.stat().st_size for p in (DEMO_ROOT / "instagram" / handle).iterdir())
        jobs.append(_job(handle, "done", posts=count, total_files=count,
                         total_bytes=size,
                         started_at=FINISHED_AT - 900 + i * 60,
                         finished_at=FINISHED_AT - 120 + i * 60,
                         message="finished", last_run_seconds=64.0 + i * 7,
                         done_count=count, total_count=count))
    for handle, plat in QUEUED:
        jobs.append(_job(handle, "queued", platform=plat))

    state = {
        "settings": {},
        "active_session": "demo.session",
        "locked_sessions": [],
        "cooldown_until": 0.0,
        "consecutive_limits": 0,
        "limit_paused": False,
        "user_paused": {"instagram": True, "tiktok": True},
        "last_autocheck": FINISHED_AT,
        "order": [j["id"] for j in jobs if j["status"] == "queued"],
        "removed": [],
        "tombstones": [],
        "output_root": str(DEMO_ROOT / "instagram"),
        "roots": [str(DEMO_ROOT / "instagram")],
        "jobs": jobs,
    }
    path = DEMO_ROOT / "config/hardlywatching/state.json"
    path.write_text(json.dumps(state, indent=1))

    # A demo session so the header says "logged in as demo.session" instead of
    # showing the login warning banner. The sessionid is a placeholder string:
    # session_diagnosis() only reads the pickle locally (the stub downloader
    # ignores sessions entirely), so nothing here can reach Instagram.
    import pickle
    sess = DEMO_ROOT / "config/instaloader"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "session-demo.session").write_bytes(pickle.dumps(
        {"sessionid": "demo-session-placeholder", "ds_user_id": "0",
         "csrftoken": "demo", "user": "demo.session"}, protocol=4))


def start() -> subprocess.Popen:
    global PORT
    e = env()
    log = open(DEMO_ROOT / "server.log", "wb")
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[2] / "server.py")],
                            cwd=str(Path(__file__).resolve().parents[2]),
                            env=e, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + 40
    while time.time() < deadline:
        # server.py finds its own free port; follow the one it announced rather
        # than insisting on ours, but never follow it to 8765.
        announced = _announced_port()
        if announced and announced != PORT:
            if announced in FORBIDDEN_PORTS:
                raise SystemExit("server announced the live port; refusing")
            PORT = announced
        try:
            with opener.open("http://127.0.0.1:%d/api/health" % PORT, timeout=1) as r:
                if json.loads(r.read()).get("ok"):
                    with opener.open("http://127.0.0.1:%d/api/state" % PORT, timeout=5) as r:
                        st = json.loads(r.read())
                    if st.get("output_root") == str(DEMO_ROOT / "instagram"):
                        return proc
        except Exception:
            pass
        time.sleep(0.15)
    raise SystemExit("demo server did not answer\n" + (DEMO_ROOT / "server.log").read_text()[-3000:])


def _announced_port() -> int | None:
    try:
        text = (DEMO_ROOT / "server.log").read_text(errors="replace")[-4000:]
    except OSError:
        return None
    m = re.search(r"running at http://127\.0\.0\.1:(\d+)", text)
    return int(m.group(1)) if m else None


def seed_ui_data(port: int) -> None:
    """Bookmarks and categories, so the gallery chips and bookmark view have rows.

    Written through the app's own API against the demo server, which is why the
    counts in dom.txt are reproducible: the rebuild drops the database.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(path: str, body: dict) -> dict:
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (port, path),
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=30) as r:
            return json.loads(r.read())

    made = {}
    for name in ("Studio refs", "Watch later"):
        made[name] = post("/api/bookmark/category", {"op": "add", "name": name})["category"]["id"]

    files = sorted((DEMO_ROOT / "instagram/demo.coral.reef").glob("*.jpg"))
    picks = [(files[0], made["Studio refs"]), (files[1], made["Watch later"]),
             (files[2], None)]
    for path, cat in picks:
        body = {"profile": "demo.coral.reef", "platform": "instagram",
                "file": path.name, "on": True}
        if cat:
            body["category"] = cat
        post("/api/bookmark", body)


def stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


if __name__ == "__main__":
    build()
    print("built", DEMO_ROOT)
