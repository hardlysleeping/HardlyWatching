#!/usr/bin/env bash
# Install the Instagram Downloader queue UI as a systemd user service so it
# starts automatically at login.
#
#   ./install-service.sh          install + start
#   ./install-service.sh remove   stop + uninstall
#
# Safe to re-run: it just rewrites the unit file and restarts the service.

set -euo pipefail

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/insta-downloader.service"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Pick a real interpreter, not a venv that might vanish. The app is stdlib-only.
PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
  echo "error: python3 not found on PATH" >&2
  exit 1
fi

if [ "${1:-install}" = "remove" ]; then
  systemctl --user disable --now insta-downloader.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  echo "Removed. Your queue state in ~/.config/insta-desktop/ is untouched."
  exit 0
fi

mkdir -p "$UNIT_DIR"

cat > "$UNIT" <<EOF
[Unit]
Description=Instagram Downloader (instaloader queue UI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$HERE
ExecStart=$PYTHON $HERE/server.py
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now insta-downloader.service

echo
echo "Installed and started."
echo "  URL:    http://127.0.0.1:${INSTA_PORT:-8765}"
echo "  Status: systemctl --user status insta-downloader"
echo "  Logs:   journalctl --user -u insta-downloader -f"
echo
echo "It will start automatically at login from now on."
