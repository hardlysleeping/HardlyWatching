# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the HardlyWatching sidecar (onedir).

Build it with ``bash packaging/sidecar/build.sh`` -- that script makes the
``.venv-build`` interpreter, runs PyInstaller against this file and drops the
external tools (yt-dlp, ffmpeg, ffprobe) into ``dist/sidecar/bin/``.

Two things here matter more than the rest:

* ``datas`` puts the web assets at the bundle root with dest ``"."``. In
  PyInstaller 6 an onedir bundle keeps that root at ``<_bundle>/_internal``,
  which is ``sys._MEIPASS``, which is the parent of the frozen ``server.py``'s
  ``__file__``. So ``server.py``'s own ``HERE = Path(__file__).resolve().parent``
  resolves to the directory holding index.html -- no change to server.py.
  ``entry.py --self-check`` prints whether that held on this build.
* ``hiddenimports`` names the local modules and the two lazy third-party
  imports explicitly. The app imports its own modules inside functions
  (``import tiktok_engine``, ``import static_videos``, ``import PIL``), which a
  static analyser can miss when the import sits behind a try/except.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# build.sh exports HWA_ROOT; SPECPATH is the fallback when the spec is run by
# hand from the repo root (pyinstaller packaging/sidecar/hardlywatching.spec).
ROOT = os.environ.get("HWA_ROOT") or os.path.abspath(os.path.join(SPECPATH, "..", ".."))

# ---------------------------------------------------------------------------
# Static web assets. server.py serves exactly these, by name, out of HERE.
# Keep this list in step with ICON_ROUTES + the .html/.webmanifest routes.
# ---------------------------------------------------------------------------
WEB_ASSETS = [
    "index.html",
    "shorts.html",
    "app.webmanifest",
    "shorts.webmanifest",
    "favicon.ico",
    "icon-192.png",
    "icon-512.png",
    "icon-512-maskable.png",
    "apple-touch-icon.png",
    # /api/version reads this from HERE; without it a frozen bundle says "dev".
    "VERSION",
]

datas = []
for _name in WEB_ASSETS:
    _src = os.path.join(ROOT, _name)
    if os.path.isfile(_src):
        datas.append((_src, "."))
    else:
        print(f"WARNING: web asset missing from the source tree: {_src}")

# The licence travels with the binaries; the archive root already carries it.
if os.path.isfile(os.path.join(ROOT, "LICENSE")):
    datas.append((os.path.join(ROOT, "LICENSE"), "."))

# ---------------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------------
# app modules: imported by server.py/engine.py at module level or inside
# functions. profile_db and engine are top-level imports, the rest are lazy.
hiddenimports = [
    "engine",
    "shorts",
    "profile_db",
    "tiktok_engine",
    "tiktok_handles",
    "static_videos",       # tools/, imported as a bare name (sys.path insert)
]

# stdlib modules that are either imported lazily or only via a helper module;
# naming them costs nothing and survives a future refactor.
hiddenimports += [
    "sqlite3",
    "_sqlite3",
    "ssl",
    "_ssl",
    "hashlib",
    "http.server",
    "webbrowser",
    "mimetypes",
    "zipfile",
    "secrets",
    "email",
    "email.message",
    "urllib.request",
    "urllib.parse",
]

# Pillow: PyInstaller's hook-PIL pulls the plugin modules, but engine.py imports
# PIL.Image inside three functions, so collect the package outright.
try:
    hiddenimports += collect_submodules("PIL")
except Exception as exc:  # pragma: no cover
    print(f"WARNING: could not collect PIL submodules: {exc}")

# pillow-heif is optional at runtime (engine.ensure_pillow_formats() tolerates
# its absence: HEIC thumbnails just do not get made). Bundle it when the build
# venv has it, and say so when it does not, rather than failing the build.
try:
    hiddenimports += collect_submodules("pillow_heif")
    datas += collect_data_files("pillow_heif")
except Exception as exc:  # pragma: no cover
    print(f"WARNING: pillow-heif not bundled ({exc}); HEIC photos will not thumbnail")

# ---------------------------------------------------------------------------
# Analysis / bundle
# ---------------------------------------------------------------------------
a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT, os.path.join(ROOT, "tools"), SPECPATH],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing in this app uses these; excluding them keeps the bundle honest
    # about its size instead of dragging in a GUI toolkit.
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "IPython",
        "pytest",
        "setuptools",
        "pkg_resources",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="hardlywatching-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,               # the sidecar talks on stdout; the shell pipes it
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="sidecar",             # -> dist/sidecar/ (distpath comes from build.sh)
)
