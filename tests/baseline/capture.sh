#!/usr/bin/env bash
# One command: rebuild the demo instance and re-capture the UI baseline.
#
#   tests/baseline/capture.sh                 # rebuild + capture + verify
#   tests/baseline/capture.sh --reuse         # reuse the demo tree
#   tests/baseline/capture.sh --out /tmp/ui   # write elsewhere, then diff dom.txt
#
# The Python script owns the rules (port floor >= 8899, refuses 8765, demo-only
# data); this wrapper only picks an interpreter.
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ -x .venv/bin/python3 ]; then
  PY=.venv/bin/python3
elif [ -x ../.venv/bin/python3 ]; then
  # A worktree of this checkout shares the checkout's venv.
  PY=../.venv/bin/python3
else
  PY=$(command -v python3)
fi

exec "$PY" tests/baseline/capture.py "$@"
