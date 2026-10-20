#!/usr/bin/env python3
"""Instagram Downloader -- fully self-contained launcher.

Run it with any Python 3.8+:

    python3 run.py

It sets up its own private environment next to itself (in .venv), installs
instaloader into it if needed, then starts the app and opens your browser.
Nothing is installed system-wide and nothing else needs to be set up first.

This app is only a frontend for instaloader, so instaloader is installed
automatically when missing -- including when there is no terminal to answer a
prompt, which is what happens when it is launched from a desktop icon. Pass
--no-install to leave instaloader entirely to you.

    python3 run.py --service      install as a systemd user service (autostart)
    python3 run.py --service remove
    python3 run.py --port 9000    use a different port
    python3 run.py --no-install   do not install instaloader, just run
"""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
VENV_PY = VENV / "bin" / "python"


def say(*a) -> None:
    print(*a, flush=True)


def _venv_python() -> Path:
    # bin/ on POSIX, Scripts/ on Windows.
    return VENV_PY if VENV_PY.exists() else VENV / "Scripts" / "python.exe"


def ensure_venv() -> Path:
    """Create .venv if missing and return its python. Stdlib-only, no deps."""
    if _venv_python().exists():
        return _venv_python()
    say("Setting up a private environment (one time, ~10 seconds)...")
    venv.EnvBuilder(with_pip=True, clear=False).create(str(VENV))
    return _venv_python()


def have_instaloader(py: Path) -> bool:
    r = subprocess.run([str(py), "-c", "import instaloader"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def ensure_heif(py: Path) -> None:
    """Best-effort Pillow HEIC support, for phone photos.

    Not fatal: without it JPEG and PNG still work and only HEIC files fail to
    produce a thumbnail, so a failed install here must not stop the app.
    """
    r = subprocess.run([str(py), "-c", "import pillow_heif, PIL"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode == 0:
        return
    say("Adding HEIC support for phone photos (optional)...")
    subprocess.run([str(py), "-m", "pip", "install", "--quiet",
                    "pillow", "pillow-heif"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_instaloader(py: Path, auto: bool = False) -> bool:
    """Install instaloader into the venv. Returns availability.

    This app is only a frontend: without instaloader nothing can download, so an
    app that starts up looking fine but cannot fetch anything is worse than one
    that installs what it needs. It is therefore installed automatically; pass
    --no-install to skip that and manage it yourself.
    """
    if have_instaloader(py):
        return True
    say("")
    say("instaloader is not installed yet.")
    say("This app is only a front end for it -- without instaloader,")
    say("nothing will download.")
    say("")

    if not auto:
        # --no-install: say what to run rather than doing it.
        say("Skipped (--no-install). Install it yourself with:")
        say("  %s -m pip install instaloader" % py)
        say("")
        return False

    # Ask when a person is there to answer, and just do it when there is not.
    # Previously a missing TTY meant the install was silently skipped, which
    # left the app running but unable to download anything.
    if sys.stdin.isatty():
        try:
            ans = input("Install instaloader now? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            say("")
            ans = "n"
        if ans not in ("", "y", "yes"):
            say("Skipped. Install it yourself, then run this again:")
            say("  %s -m pip install instaloader" % py)
            return False

    say("Installing instaloader (one time)...")
    r = subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "instaloader"])
    if r.returncode == 0 and have_instaloader(py):
        say("instaloader installed.")
        return True
    say("Install failed. Try manually:  %s -m pip install instaloader" % py)
    return False


def main() -> int:
    args = sys.argv[1:]

    # instaloader is installed by default so a one-command start really does
    # work end to end; --no-install leaves it to the user.
    auto_install = "--no-install" not in args
    args = [a for a in args if a != "--no-install"]

    if "--service" in args:
        # Hand off to the systemd installer, using this venv's python.
        py = ensure_venv()
        if not ensure_instaloader(py, auto=auto_install):
            return 1
        return subprocess.call(["bash", str(HERE / "install-service.sh"),
                                *[a for a in args if a != "--service"]],
                               cwd=str(HERE))

    py = ensure_venv()
    if not ensure_instaloader(py, auto=auto_install):
        say("")
        say("Starting anyway -- the app will run, but downloads need instaloader.")
        say("")
    ensure_heif(py)

    env = dict(os.environ)
    # Make sure the venv's instaloader is the one the app finds.
    env["PATH"] = str(py.parent) + os.pathsep + env.get("PATH", "")
    # Only pass server-understood flags through.
    passthrough = []
    i = 0
    while i < len(args):
        if args[i] in ("--port", "--host") and i + 1 < len(args):
            passthrough += [args[i], args[i + 1]]
            i += 2
        else:
            i += 1
    return subprocess.call([str(py), str(HERE / "server.py"), *passthrough],
                           cwd=str(HERE), env=env)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
