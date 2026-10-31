#!/usr/bin/env python3
"""Build the frozen sidecar on whatever OS this runs on.

``packaging/sidecar/build.sh`` is the Linux build and stays that: it copies the
host's ffmpeg out of /usr/bin and writes a ``bin/instaloader`` shell wrapper.
Windows and macOS need the same bundle with different tools and a different
wrapper, so this script does the same steps in Python instead of shell. It is
what CI runs on ``windows-latest`` and ``macos-latest``; on Linux use build.sh.

    python3 packaging/sidecar/build_ci.py

Env:
  YTDLP_BIN    a standalone yt-dlp for this platform (yt-dlp.exe / yt-dlp_macos).
               Required: a pipx console script cannot be copied into a bundle.
  FFMPEG_BIN   an ffmpeg binary; ffprobe is expected beside it, or on PATH.
  PYTHON       interpreter used to make the build venv (default: this one).

Output: dist/sidecar/  -- hardlywatching-server[.exe] + _internal/ + bin/
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist" / "sidecar"
VENV = ROOT / ".venv-build"
SPEC = ROOT / "packaging" / "sidecar" / "hardlywatching.spec"
IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""
SERVER = f"hardlywatching-server{EXE}"

# yt-dlp is only needed at build time so the spec's collect_submodules("yt_dlp")
# has something to collect; the app shells out to the standalone binary instead.
PIP_PACKAGES = [
    "pyinstaller>=6.6",
    "pillow>=10.4,<12",
    "pillow-heif",
    "numpy",
    "onnxruntime",
    "instaloader",
    "yt-dlp",
]


def say(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def ensure_venv() -> Path:
    py = venv_python()
    if not py.is_file():
        base = os.environ.get("PYTHON") or sys.executable
        say(f"making {VENV.name} with {base}")
        subprocess.run([base, "-m", "venv", str(VENV)], check=True)
    say("installing build dependencies")
    subprocess.run(
        [str(py), "-m", "pip", "install", "--upgrade", "--quiet", "pip"], check=True
    )
    subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", *PIP_PACKAGES], check=True
    )
    return py


def run_pyinstaller(py: Path) -> None:
    say("freezing the server (PyInstaller)")
    if DIST.exists():
        shutil.rmtree(DIST)
    env = dict(os.environ, HWA_ROOT=str(ROOT))
    subprocess.run(
        [
            str(py),
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(ROOT / "dist"),
            "--workpath",
            str(VENV / "pyinstaller-work"),
            str(SPEC),
        ],
        check=True,
        env=env,
    )
    if not (DIST / SERVER).is_file():
        raise SystemExit(f"build_ci: PyInstaller produced no {SERVER}")


def is_native_binary(path: Path) -> bool:
    """A copy is portable only if it is a real executable for this platform.

    On POSIX that means ELF or Mach-O; a ``#!`` script points at one machine's
    interpreter (a pipx console script), which is useless inside a bundle. On
    Windows every standalone tool is a PE file starting with ``MZ``.
    """
    try:
        head = path.open("rb").read(4)
    except OSError:
        return False
    if IS_WIN:
        return head[:2] == b"MZ"
    return head in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")


def find_tool(env_name: str, *names: str) -> Path | None:
    explicit = os.environ.get(env_name)
    candidates = [Path(explicit)] if explicit else []
    for name in names:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
        candidates.append(ROOT / "bin" / name)
    for cand in candidates:
        if cand.is_file() and is_native_binary(cand):
            return cand
        if cand.is_file():
            print(f"WARNING: skipping {cand}: not a native binary for this platform")
    return None


def copy_tools() -> list[str]:
    """bin/ next to the server, which entry.py puts first on PATH."""
    bin_dir = DIST / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    ytdlp = find_tool("YTDLP_BIN", "yt-dlp")
    if ytdlp:
        target = bin_dir / f"yt-dlp{EXE}"
        shutil.copy2(ytdlp, target)
        os.chmod(target, 0o755)
        notes.append(f"yt-dlp <- {ytdlp}")
    else:
        print(
            "WARNING: no standalone yt-dlp found; TikTok downloads will fail "
            "unless the host has one. Set YTDLP_BIN.",
            file=sys.stderr,
        )

    ffmpeg = find_tool("FFMPEG_BIN", "ffmpeg")
    ffprobe = None
    if ffmpeg:
        beside = ffmpeg.with_name(f"ffprobe{EXE}")
        ffprobe = beside if beside.is_file() else find_tool("FFPROBE_BIN", "ffprobe")
    if ffmpeg and ffprobe:
        for src, name in ((ffmpeg, f"ffmpeg{EXE}"), (ffprobe, f"ffprobe{EXE}")):
            target = bin_dir / name
            shutil.copy2(src, target)
            os.chmod(target, 0o755)
            notes.append(f"{name} <- {src}")
    else:
        print(
            "WARNING: ffmpeg/ffprobe not both found; thumbnails and clips will fail",
            file=sys.stderr,
        )

    # engine.py resolves instaloader with shutil.which, so the bundle needs a
    # program with that name. POSIX gets a two-line shell wrapper; Windows has no
    # shebang to write, so it gets a copy of this binary under that name and
    # entry.py recognises itself by argv[0] (see the dispatch in main()).
    if IS_WIN:
        wrapper = bin_dir / "instaloader.exe"
        shutil.copy2(DIST / SERVER, wrapper)
        notes.append("instaloader.exe <- copy of hardlywatching-server.exe")
    else:
        wrapper = bin_dir / "instaloader"
        wrapper.write_text(
            "#!/bin/sh\n"
            'exec "$(dirname "$0")/hardlywatching-server" --run-instaloader "$@"\n'
        )
        os.chmod(wrapper, 0o755)
        notes.append("instaloader <- wrapper -> hardlywatching-server --run-instaloader")

    prov = bin_dir / "PROVENANCE.txt"
    prov.write_text(
        "# HardlyWatching sidecar -- external tools, and where this build found them.\n"
        f"# generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} on {sys.platform}\n"
        "# copied-by: packaging/sidecar/build_ci.py\n\n" + "\n".join(notes) + "\n"
    )
    return notes


def main() -> int:
    say(f"repo    {ROOT}")
    say(f"host    {sys.platform} ({sys.version.split()[0]})")
    py = ensure_venv()
    run_pyinstaller(py)
    notes = copy_tools()
    for line in notes:
        say(line)
    total = sum(p.stat().st_size for p in DIST.rglob("*") if p.is_file())
    say(f"laid out {DIST} ({total / 1_000_000:.0f} MB)")
    say(f"next:  {DIST / SERVER} --self-check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
