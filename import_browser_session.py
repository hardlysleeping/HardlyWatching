#!/usr/bin/env python3
"""Import an Instagram session into instaloader from a Firefox-family browser.

Why this exists: instaloader's own `--login` sometimes completes "successfully"
yet writes a session file with an EMPTY sessionid, which then 401s on every
request. Instagram's docs recommend sidestepping `--login` entirely by importing
the cookie from a browser where you are already logged in.

(https://instaloader.github.io/troubleshooting.html -- "Login error")

Works with Firefox and Firefox-family browsers (Zen, LibreWolf, Waterfox),
including their Flatpak installs, whose profiles live under ~/.var/app/... .
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from engine import instaloader_config_dir

try:
    from instaloader import Instaloader
except ModuleNotFoundError:
    # instaloader may live in a pipx venv rather than this interpreter.
    import sysconfig
    import glob as _glob
    _cands = _glob.glob(str(Path.home() / ".local/share/pipx/venvs/instaloader/lib/python*/site-packages"))
    for _c in _cands:
        if _c not in sys.path:
            sys.path.insert(0, _c)
    try:
        from instaloader import Instaloader
    except ModuleNotFoundError:
        sys.exit("instaloader not found. Install it first: pipx install instaloader")


# (label, glob pattern relative to $HOME). Order = preference.
BROWSER_GLOBS = [
    ("Firefox (native)",   ".mozilla/firefox/*/cookies.sqlite"),
    ("Firefox 147+ XDG",   ".config/mozilla/firefox/*/cookies.sqlite"),
    ("Firefox (flatpak)",  ".var/app/org.mozilla.firefox/.mozilla/firefox/*/cookies.sqlite"),
    ("Zen (native)",       ".zen/*/cookies.sqlite"),
    ("Zen (flatpak)",      ".var/app/app.zen_browser.zen/.zen/*/cookies.sqlite"),
    ("LibreWolf (native)", ".librewolf/*/cookies.sqlite"),
    ("LibreWolf (flatpak)", ".var/app/io.gitlab.librewolf-community/.librewolf/*/cookies.sqlite"),
]


def find_cookie_stores() -> list[tuple[str, Path]]:
    """Every Firefox-family cookie DB on this machine, newest first."""
    home = Path.home()
    found: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for label, pattern in BROWSER_GLOBS:
        for p in home.glob(pattern):
            rp = str(p.resolve())
            if rp in seen or not p.is_file():
                continue
            seen.add(rp)
            found.append((label, p))
    # newest profile first: most likely to hold a live session
    found.sort(key=lambda t: t[1].stat().st_mtime, reverse=True)
    return found


def read_instagram_cookies(cookiefile: Path) -> dict[str, str]:
    """Pull instagram.com cookies out of a Firefox-family cookies.sqlite."""
    conn = sqlite3.connect(f"file:{cookiefile}?immutable=1", uri=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(moz_cookies)")}
        if not cols:
            raise RuntimeError("not a Firefox cookie database")
        domain_col = "baseDomain" if "baseDomain" in cols else "host"
        query = (f"SELECT name, value FROM moz_cookies "
                 f"WHERE {domain_col} LIKE '%instagram.com'")
        rows = conn.execute(query).fetchall()
    finally:
        conn.close()
    # later rows win: the most recently written cookie is the live one
    cookies: dict[str, str] = {}
    for name, value in rows:
        if value:
            cookies[name] = value
    return cookies


def import_session(cookiefile: Path, sessionfile: str | None,
                   username: str | None) -> int:
    print(f"Reading cookies from: {cookiefile}")
    try:
        cookies = read_instagram_cookies(cookiefile)
    except Exception as e:
        print(f"  could not read that file: {e}")
        return 1

    sid = cookies.get("sessionid", "")
    if not sid:
        print("  no sessionid found — you are not logged into Instagram in this browser.")
        return 1
    print(f"  found sessionid ({len(sid)} chars)")

    loader = Instaloader(max_connection_attempts=1)
    loader.context._session.cookies.update(cookies)

    print("  verifying with Instagram…")
    try:
        who = loader.test_login()
    except Exception as e:
        print(f"  verification failed: {e}")
        return 2
    if not who:
        print("  Instagram rejected the cookie. Log in again in the browser.")
        return 2

    print(f"  OK — logged in as {who}")
    if username and who != username:
        print(f"  note: you asked for {username}, cookie belongs to {who}. Using {who}.")

    loader.context.username = who
    loader.save_session_to_file(sessionfile)
    dest = sessionfile or str(instaloader_config_dir() / f"session-{who}")
    print(f"Saved session to {dest}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--cookiefile", help="explicit cookies.sqlite path")
    ap.add_argument("-f", "--sessionfile", help="where to write the instaloader session")
    ap.add_argument("-u", "--username", help="expected Instagram username")
    args = ap.parse_args()

    if args.cookiefile:
        return import_session(Path(args.cookiefile), args.sessionfile, args.username)

    stores = find_cookie_stores()
    if not stores:
        print("No Firefox-family cookie databases found.")
        print("Checked paths under your home directory, including Flatpak installs.")
        print("Log into Instagram in Firefox or Zen first, then re-run this.")
        return 1

    print("Browser cookie stores found:")
    for i, (label, p) in enumerate(stores):
        try:
            n = len(read_instagram_cookies(p))
        except Exception:
            n = -1
        marker = f"{n} instagram cookies" if n >= 0 else "unreadable"
        print(f"  [{i}] {label}: {p}  ({marker})")

    # prefer a store that actually holds a sessionid
    for label, p in stores:
        try:
            if read_instagram_cookies(p).get("sessionid"):
                print(f"\nUsing {label} (has a live sessionid).")
                return import_session(p, args.sessionfile, args.username)
        except Exception:
            continue

    print("\nNone of those browsers has an Instagram sessionid.")
    print("Log into Instagram in Firefox or Zen, then run this again.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
