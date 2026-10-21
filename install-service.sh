#!/usr/bin/env bash
# Install the HardlyWatching queue UI as a systemd user service so it
# starts automatically at login.
#
#   ./install-service.sh          install + start
#   ./install-service.sh remove   stop + uninstall
#
# Safe to re-run: it just rewrites the unit file and restarts the service.

set -euo pipefail

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/hardlywatching.service"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The app is NOT stdlib-only: it needs Pillow to make thumbnails, and
# pillow-heif to show the HEIC photos a phone produces. Prefer the project's own
# venv, which is where run.py installs them, and only fall back to a system
# python3 -- checking that it can actually import them rather than assuming.
if [ -x "$HERE/.venv/bin/python3" ]; then
  PYTHON="$HERE/.venv/bin/python3"
else
  PYTHON="$(command -v python3 || true)"
  if [ -z "$PYTHON" ]; then
    echo "error: no python3 found (neither $HERE/.venv nor PATH)" >&2
    exit 1
  fi
  echo "note: no venv at $HERE/.venv, falling back to $PYTHON"
fi

if ! "$PYTHON" -c 'import PIL' >/dev/null 2>&1; then
  echo "error: $PYTHON cannot import Pillow, which the app needs for thumbnails." >&2
  echo "       Fix it with:  $PYTHON -m pip install --user Pillow pillow-heif" >&2
  echo "       (or run ./run.py, which sets up the venv for you)" >&2
  exit 1
fi
if ! "$PYTHON" -c 'import pillow_heif' >/dev/null 2>&1; then
  echo "note: pillow-heif is missing, so HEIC photos from a phone will not show"
  echo "      a thumbnail or open in the viewer. Optional:"
  echo "        $PYTHON -m pip install pillow-heif"
  echo
fi

# The app is a UI around instaloader; warn (don't fail) if it is missing.
if ! command -v instaloader >/dev/null 2>&1; then
  echo "note: instaloader is not on PATH yet."
  echo "      Install it first:  pipx install instaloader"
  echo
fi

if [ "${1:-install}" = "remove" ]; then
  systemctl --user disable --now hardlywatching.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  echo "Removed. Your queue state in ~/.config/hardlywatching/ is untouched."
  exit 0
fi

mkdir -p "$UNIT_DIR"

cat > "$UNIT" <<EOF
[Unit]
Description=HardlyWatching (instaloader and yt-dlp download queue and viewer)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$HERE
ExecStart=$PYTHON $HERE/server.py
# Both downloaders are found on PATH, so it has to be set explicitly: a unit
# does not inherit the login shell's PATH, and without this the app would fall
# back to guessing at ~/.local/bin.
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now hardlywatching.service

echo
echo "Installed and started."
echo "  URL:    http://127.0.0.1:${INSTA_PORT:-8765}"
echo "  Status: systemctl --user status hardlywatching"
echo "  Logs:   journalctl --user -u hardlywatching -f"
echo
echo "It will start automatically at login from now on."
