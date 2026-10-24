#!/usr/bin/env python3
"""Frozen entry point for the HardlyWatching sidecar (``hardlywatching-server``).

This is the ``__main__`` of the PyInstaller bundle built by
``packaging/sidecar/build.sh``. It does four things and nothing else:

1. Reads the flags/env the app already understands (``--port``, ``--host`` plus
   the ``INSTA_*`` environment) and leaves ``sys.argv`` untouched, because
   ``server.py`` parses its own options *at import time* with ``_opt()``.
2. Puts the bundle's own ``bin/`` directory first on ``PATH`` so ``engine.py``
   finds the bundled ``yt-dlp`` / ``ffmpeg`` / ``ffprobe`` by name.
3. Imports ``server`` and makes sure the static web assets it serves resolve to
   a directory that actually contains them. ``server.py`` computes
   ``HERE = Path(__file__).resolve().parent``; in a frozen onedir build
   ``__file__`` is ``<_MEIPASS>/server.pyc``, i.e. the bundle's ``_internal``
   directory, which is exactly where the spec puts the HTML/manifests/icons.
   No edit to ``server.py`` is required. If a future layout ever puts the assets
   somewhere else, this file reassigns the module attribute ``server.HERE``
   (a runtime attribute, not a source change) and says so on stderr.
4. Calls ``server.main()``, then exits cleanly: ``KeyboardInterrupt`` and
   ``SIGTERM`` both shut the HTTP server down, and any non-daemon thread that
   outlived ``main()`` is abandoned on purpose so the process never lingers as
   an orphan for the desktop shell to have to hunt down.

Extra flags this file adds (the app's own flags pass straight through):

    --no-browser        never open a tab (also INSTA_NO_BROWSER=1)
    --self-check        print what the bundle resolved, exit 0/1, serve nothing
    --version           print the sidecar version
    -h/--help           this text

Run ``hardlywatching-server --self-check`` first when something looks wrong:
it reports which asset directory, which ffmpeg and which yt-dlp the frozen
process actually sees.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

# Bumped by hand when the bundle layout (not the app) changes. Reported by
# --version and used by the desktop shell to refuse a mismatched pair.
SIDECAR_VERSION = "0.1.0"

USAGE = """\
hardlywatching-server -- HardlyWatching, frozen (no Python install needed)

Usage: hardlywatching-server [options]

  --port N            port to bind (default $INSTA_PORT or 8765)
  --host ADDR         address to bind (default $INSTA_HOST or 127.0.0.1)
  --no-browser        never open a browser tab (also INSTA_NO_BROWSER=1)
  --self-check        print what this bundle resolved, then exit
  --version           print the sidecar version, then exit
  -h, --help          this text

Environment (same variables the Python app reads):
  INSTA_PORT, INSTA_HOST, INSTA_STATE, INSTA_CACHE_DB, INSTA_OUTPUT,
  INSTA_THUMBS, INSTA_AVATARS

The bundle carries its own CPython, Pillow, pillow-heif, yt-dlp, ffmpeg and
ffprobe. It does NOT carry instaloader; the app offers to install it with pipx
on first run if it is missing.
"""


# --------------------------------------------------------------------------
# Where am I?  (onedir: sys.executable is <bundle>/hardlywatching-server,
#              sys._MEIPASS is <bundle>/_internal)
# --------------------------------------------------------------------------

def bundle_dir() -> Path:
    """The directory the frozen runtime unpacked into (``sys._MEIPASS``)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def exe_dir() -> Path:
    """The directory the user's exe sits in (``<bundle>`` for onedir)."""
    exe = getattr(sys, "executable", None) or sys.argv[0]
    try:
        return Path(exe).resolve().parent
    except OSError:
        return Path.cwd()


def asset_dirs() -> list[Path]:
    """Candidate directories for index.html etc., most likely first.

    ``_MEIPASS`` (``_internal``) is where the spec's ``datas`` land, so it comes
    first; the exe dir and an ``assets/`` subdir are there for other layouts
    (onefile, a hand-assembled tarball) so the bundle is not brittle.
    """
    here = bundle_dir()
    cands = [here, here / "assets", exe_dir(), exe_dir() / "_internal",
             exe_dir() / "assets", Path(__file__).resolve().parent]
    seen: list[Path] = []
    for c in cands:
        if c not in seen:
            seen.append(c)
    return seen


def web_assets_present(d: Path) -> bool:
    return (d / "index.html").is_file() and (d / "shorts.html").is_file()


def prepend_bundle_bin() -> None:
    """Put the bundle's bin/ first on PATH.

    ``engine.py`` runs ``yt-dlp``, ``ffmpeg`` and ``ffprobe`` by bare name, so
    this is the whole mechanism that makes the bundled copies win over whatever
    the host happens to have installed.
    """
    dirs = []
    for d in (exe_dir() / "bin", bundle_dir() / "bin"):
        if d.is_dir() and d not in dirs:
            dirs.append(d)
    # Insert in reverse so the exe-dir bin/ ends up leftmost.
    for d in reversed(dirs):
        if str(d) not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


# --------------------------------------------------------------------------
# Argument handling.  Deliberately non-destructive: server.py reads
# sys.argv itself at import time, so nothing is removed from it.
# --------------------------------------------------------------------------

def parse_args(argv: list[str]) -> dict:
    opts = {
        "port": None, "host": None, "no_browser": False,
        "self_check": False, "help": False, "version": False,
    }

    def take_value(arg: str, name: str, i: int) -> tuple[str | None, int]:
        if arg == name:
            return (argv[i + 1] if i + 1 < len(argv) else None), i + 2
        if arg.startswith(name + "="):
            return arg[len(name) + 1:], i + 1
        return None, i

    i = 0
    while i < len(argv):
        a = argv[i]
        value, nxt = take_value(a, "--port", i)
        if value is not None:
            opts["port"] = value
            i = nxt
            continue
        value, nxt = take_value(a, "--host", i)
        if value is not None:
            opts["host"] = value
            i = nxt
            continue
        if a in ("--no-browser", "-n", "--headless"):
            opts["no_browser"] = True
        elif a == "--self-check":
            opts["self_check"] = True
        elif a in ("-h", "--help"):
            opts["help"] = True
        elif a in ("-V", "--version"):
            opts["version"] = True
        i += 1
    return opts


class _NoBrowser:
    """Stand-in for server's ``webbrowser`` module: opens nothing."""

    def open(self, *args, **kwargs):  # noqa: A003 - mirrors webbrowser.open
        return False

    def open_new(self, *args, **kwargs):
        return False

    def open_new_tab(self, *args, **kwargs):
        return False

    def get(self, *args, **kwargs):
        return None


def _install_sigterm_shutdown() -> None:
    """Turn SIGTERM into KeyboardInterrupt so serve_forever() unwinds cleanly.

    Without this a ``kill`` from the desktop shell terminates the process
    abruptly, which leaves the HTTP socket in TIME_WAIT and no chance to flush.
    """
    def _handler(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        pass  # not the main thread, or a platform without SIGTERM


def _exit_cleanly(code: int = 0) -> None:
    """Exit even if a non-daemon thread is stuck somewhere.

    The app's own workers are daemons, so normally the interpreter exits on its
    own. This is the belt-and-braces case: an orphan leaf process is worse than
    a skipped interpreter teardown, because the desktop shell's "kill the child
    on window close" guarantee depends on it.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    leftovers = [t for t in threading.enumerate()
                 if t is not threading.main_thread() and not t.daemon and t.is_alive()]
    if leftovers:
        print("sidecar: abandoning non-daemon thread(s): "
              + ", ".join(t.name for t in leftovers), file=sys.stderr)
        os._exit(code)
    raise SystemExit(code)


# --------------------------------------------------------------------------
# Self-check: the honest report of what this bundle can reach.
# --------------------------------------------------------------------------

def self_check(server) -> int:
    import shutil

    ok = True
    print(f"sidecar version : {SIDECAR_VERSION}")
    print(f"python          : {sys.version.split()[0]} (frozen={bool(getattr(sys, 'frozen', False))})")
    print(f"executable      : {sys.executable}")
    print(f"_MEIPASS        : {getattr(sys, '_MEIPASS', '(none)')}")
    print(f"server.__file__ : {getattr(server, '__file__', '(none)')}")
    print(f"server.HERE     : {server.HERE}")
    print(f"HERE == _MEIPASS: {str(server.HERE) == str(getattr(sys, '_MEIPASS', ''))}")
    for name in ("index.html", "shorts.html", "app.webmanifest", "shorts.webmanifest",
                 "favicon.ico", "icon-192.png", "icon-512.png",
                 "icon-512-maskable.png", "apple-touch-icon.png"):
        present = (Path(server.HERE) / name).is_file()
        if not present:
            ok = False
        print(f"  asset {name:<28} {'ok' if present else 'MISSING'}")
    for tool in ("yt-dlp", "ffmpeg", "ffprobe", "instaloader"):
        found = shutil.which(tool)
        print(f"  tool  {tool:<28} {found or 'not found'}")
    try:
        import PIL
        print(f"  pillow                          {PIL.__version__}")
    except Exception as e:  # pragma: no cover - defensive
        ok = False
        print(f"  pillow                          FAILED: {e}")
    try:
        import pillow_heif
        print(f"  pillow-heif                     {getattr(pillow_heif, '__version__', '?')}")
    except Exception as e:  # pillow-heif is optional for this app
        print(f"  pillow-heif                     absent ({e.__class__.__name__})")
    print(f"port / host     : {server.PORT} / {server.HOST}")
    print(f"output root     : {server.get_engine().output_root}")
    print("RESULT          : " + ("ok" if ok else "problems found"))
    return 0 if ok else 1


def _line_buffer_output() -> None:
    """Make our own logs reach the pipe as they are written.

    A frozen interpreter writing to a pipe is block-buffered, and this process is
    normally started by the desktop shell with stdout/stderr on pipes. Without
    this the shell would see the whole banner only when the server stops, which
    is exactly when it is useless -- and "already running at <url>" would never
    arrive in time. PYTHONUNBUFFERED does not survive the bootloader, so ask the
    streams directly.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True, write_through=True)
        except (AttributeError, ValueError, OSError):
            pass


def main() -> int:
    _line_buffer_output()
    opts = parse_args(sys.argv[1:])

    if opts["help"]:
        print(USAGE, end="")
        return 0
    if opts["version"]:
        print(f"hardlywatching-server {SIDECAR_VERSION} "
              f"(frozen, CPython {sys.version.split()[0]})")
        return 0

    # Mirror the flags into the env the app reads. server.py's own _opt() looks
    # at argv first, so this changes nothing today; it keeps the binary honest
    # if that parser is ever rewritten to be env-only.
    if opts["port"]:
        os.environ["INSTA_PORT"] = str(opts["port"])
    if opts["host"]:
        os.environ["INSTA_HOST"] = str(opts["host"])

    if opts["no_browser"] or os.environ.get("INSTA_NO_BROWSER", "").strip() in ("1", "true", "yes"):
        opts["no_browser"] = True

    prepend_bundle_bin()

    import server  # noqa: PLC0415 - import-time argv read is intentional

    # ---- static assets -------------------------------------------------
    # Primary path: here == _MEIPASS == _internal, exactly where the spec put
    # index.html. Only if that is empty do we go looking, and then we assign the
    # module attribute (never the source) so server's handler serves from the
    # directory that really holds the files.
    if not web_assets_present(Path(server.HERE)):
        for cand in asset_dirs():
            if web_assets_present(cand):
                print(f"sidecar: web assets not at {server.HERE}; using {cand}",
                      file=sys.stderr)
                server.HERE = cand
                break
        else:
            print("sidecar: WARNING - index.html/shorts.html were not found in the "
                  "bundle. The UI will 404. Check the spec's `datas` and run "
                  "--self-check.", file=sys.stderr)

    if opts["self_check"]:
        return self_check(server)

    if opts["no_browser"]:
        server.webbrowser = _NoBrowser()

    _install_sigterm_shutdown()

    try:
        rc = server.main()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
        rc = 0
    return rc


if __name__ == "__main__":
    _exit_cleanly(main())
