#!/usr/bin/env bash
# Open the HardlyWatching control panel window.
#
#   ./control-panel.sh
#
# Uses the project's venv, because that is where the Qt toolkit lives; a system
# python on an immutable distro has no PyGObject and no PySide6. control-panel.py
# also re-execs itself into this venv, so launching it any other way still works.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "Python 3 is required but was not found." >&2
  exit 1
fi

exec "$PY" "$HERE/control-panel.py" "$@"
