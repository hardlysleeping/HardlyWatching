#!/usr/bin/env bash
#
# ci-local.sh -- the local twin of .github/workflows/ci.yml.
#
# Same steps, same order, same package list, so the workflow is verifiable on
# this machine without GitHub Actions. The steps are deliberately duplicated
# there rather than called from here: if one side drifts, the difference shows
# up in review instead of one file silently becoming the only truth.
#
#   bash scripts/ci-local.sh
#   PYTHON=/path/to/python3 bash scripts/ci-local.sh   # skip venv creation
#
# Exit 0 and a PASS line = the suite is green. Non-zero and a FAIL line
# otherwise.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR="$REPO_ROOT/.ci-venv"          # throwaway, git-ignored (.gitignore)

# The 46-case suite imports only the standard library: it passes in a venv
# that holds nothing but pip (verified -- see docs/ci.md). Pillow is installed
# anyway because every real install of the app has it and the image paths in
# server.py/engine.py import it lazily; it is not what makes the suite green.
# Keep this list identical to the one in .github/workflows/ci.yml.
PIP_PACKAGES=("Pillow>=10.4,<12")

# Exactly the modules the workflow compile-checks.
MODULES=(server.py engine.py shorts.py profile_db.py control-panel.py)

LOG="$(mktemp -t hwa-ci-local.XXXXXX.log)"
cleanup() { rm -f "$LOG"; }
trap cleanup EXIT

step() { printf '\n==> %s\n' "$*"; }

on_error() {
    local rc=$?
    printf '\nFAIL: ci-local.sh stopped at line %s (exit %s): %s\n' \
        "${BASH_LINENO[0]:-?}" "$rc" "${BASH_COMMAND}" >&2
    printf 'See the output above.\n' >&2
    exit "$rc"
}
trap on_error ERR

# ---------------------------------------------------------------- interpreter
if [[ -n "${PYTHON:-}" ]]; then
    step "Python: ${PYTHON} (PYTHON set -- venv creation and pip install skipped)"
    if [[ ! -x "${PYTHON}" ]]; then
        printf 'error: PYTHON=%s is not an executable\n' "${PYTHON}" >&2
        exit 2
    fi
    PY="${PYTHON}"
else
    step "Throwaway venv: ${VENV_DIR}"
    if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
        python3 -m venv "${VENV_DIR}"
        printf 'created %s\n' "${VENV_DIR}"
    else
        printf 'reusing %s\n' "${VENV_DIR}"
    fi
    PY="${VENV_DIR}/bin/python"

    step "Install dependencies: ${PIP_PACKAGES[*]}"
    "${PY}" -m pip install --quiet --upgrade pip
    "${PY}" -m pip install --quiet --disable-pip-version-check "${PIP_PACKAGES[@]}"
fi

step "Versions"
"${PY}" -V
"${PY}" -m pip --version

# ------------------------------------------------------------- compile check
step "Compile check: ${MODULES[*]}"
"${PY}" -m compileall -q "${MODULES[@]}"
printf 'compiled %s module(s) with no syntax errors\n' "${#MODULES[@]}"

# --------------------------------------------------------------------- tests
step "Unit tests: python -m unittest discover -s tests -v"
# Inside an `if`, errexit and the ERR trap stay quiet, and PIPESTATUS still
# holds the pipeline's statuses, so a failing test suite is reported here once
# instead of being mistaken for an unrelated script error.
if "${PY}" -m unittest discover -s tests -v 2>&1 | tee "$LOG"; then
    rc=0
else
    rc=${PIPESTATUS[0]}
fi
if (( rc != 0 )); then
    printf '\nFAIL: the test suite exited %s.\n' "$rc" >&2
    exit "$rc"
fi

# Green with zero tests is not green: discovery finding nothing would otherwise
# look like a pass. The workflow has the same guard.
ran_line="$(grep -E '^Ran [0-9]+ tests? in ' "$LOG" | tail -1 || true)"
if [[ -z "$ran_line" ]]; then
    printf '\nFAIL: no unittest summary line -- discovery found nothing to run.\n' >&2
    exit 1
fi
ran_count="$(sed -E 's/^Ran ([0-9]+) tests? in .*/\1/' <<< "$ran_line")"
if (( ran_count == 0 )); then
    printf '\nFAIL: unittest ran 0 tests.\n' >&2
    exit 1
fi

printf '\nPASS: %s (%s, Python %s)\n' "$ran_line" \
    "compile check + unittest discover" "$("${PY}" -c 'import platform; print(platform.python_version())')"
