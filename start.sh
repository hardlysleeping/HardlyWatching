#!/usr/bin/env bash
# Instagram Downloader -- one-command launcher.
#
#   ./start.sh
#
# Checks what's needed, starts the app, and opens it in your browser.
# Run it from anywhere; it finds its own folder.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

say() { printf '%s\n' "$*"; }

say ""
say "Instagram Downloader"
say "--------------------"

# 1. Python present?
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  say "Python 3 is required but was not found."
  say "Install it with your package manager, then run this again:"
  say "    sudo apt install python3      (Debian/Ubuntu)"
  say "    sudo dnf install python3      (Fedora/Bazzite)"
  exit 1
fi

# 2. instaloader present? server.py offers to install it if not.
if ! command -v instaloader >/dev/null 2>&1; then
  say "instaloader is not installed yet."
  say "The app will ask if you want it installed when it starts."
  say ""
fi

# 3. Start it. server.py checks instaloader, opens the browser, and serves.
say "Starting... (press Ctrl-C to stop)"
say ""
exec "$PY" server.py
