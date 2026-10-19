#!/usr/bin/env bash
# Add Instagram Downloader to your applications menu (and desktop), so you can
# start it with a click instead of a terminal command.
#
#   ./install-desktop-icon.sh          add the icon
#   ./install-desktop-icon.sh remove   take it away

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
ENTRY="insta-downloader.desktop"

if [ "${1:-install}" = "remove" ]; then
  rm -f "$APPS_DIR/$ENTRY" "$DESKTOP_DIR/$ENTRY"
  command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
  echo "Removed the Instagram Downloader icon."
  exit 0
fi

mkdir -p "$APPS_DIR"

# Fill in the real install path and drop it in the app menu.
sed "s|__INSTALL_DIR__|$HERE|g" "$HERE/$ENTRY" > "$APPS_DIR/$ENTRY"
chmod +x "$APPS_DIR/$ENTRY"

# Also put a copy on the desktop if there is one, and trust it (GNOME).
if [ -d "$DESKTOP_DIR" ]; then
  cp "$APPS_DIR/$ENTRY" "$DESKTOP_DIR/$ENTRY"
  chmod +x "$DESKTOP_DIR/$ENTRY"
  command -v gio >/dev/null 2>&1 && gio set "$DESKTOP_DIR/$ENTRY" \
    metadata::trusted true 2>/dev/null || true
fi

command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "$APPS_DIR" 2>/dev/null || true

echo "Added 'Instagram Downloader' to your applications menu."
[ -d "$DESKTOP_DIR" ] && echo "A shortcut is on your desktop too."
echo
echo "Click it to start the app. It opens in your browser."
echo "To remove it later:  ./install-desktop-icon.sh remove"
