#!/usr/bin/env bash
# Pull upstream HardlyWatching into this app fork, but only when it is safe.
#
#   scripts/sync-upstream.sh              # merge, test, push
#   scripts/sync-upstream.sh --no-push    # merge + test, leave it for review
#   scripts/sync-upstream.sh --check      # is there anything to sync? (exit 10 = yes)
#
# Exit codes:
#   0  up to date, or merged + tested green (+ pushed)
#   2  conflict: nothing was changed, resolve by hand
#   3  the merge went in, the tests went red: rolled back, nothing changed
#   4  dirty tree / not on master: nothing was changed
#  10  --check only: upstream has commits we do not have
#
# Why a script and not a plain `git merge`: the app fork carries src-tauri/,
# packaging/sidecar/ and tests/baseline/, which upstream has no opinion about, so
# most syncs are trivial. The two files that DO both sides touch are README.md and
# CHANGELOG.md, and the rule for them is additions only (see docs/upstream-sync.md).
# When they do collide the merge is stopped, never guessed at, and the tree is
# restored exactly as it was, so the live app is never left in a half-merged state
# wearing a green build.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO=$(pwd)

UPSTREAM=${UPSTREAM_REMOTE:-upstream}
BRANCH=${BRANCH:-master}
MODE=push
for arg in "$@"; do
  case "$arg" in
    --no-push) MODE=nopush ;;
    --check)   MODE=check ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
die() { printf 'SYNC-FAILED: %s\n' "$*" >&2; exit "${2:-1}"; }

# --- preconditions -----------------------------------------------------------
[ -z "$(git status --porcelain)" ] || die "working tree is dirty; commit or stash first (nothing changed)" 4
[ "$(git rev-parse --abbrev-ref HEAD)" = "$BRANCH" ] || die "not on $BRANCH (nothing changed)" 4
git remote get-url "$UPSTREAM" >/dev/null 2>&1 || die "no '$UPSTREAM' remote; add it: git remote add $UPSTREAM git@github.com:hardlysleeping/HardlyWatching.git" 4

# A merge commit needs an identity. The repo here has one; a fresh clone may not, and
# "Committer identity unknown" would otherwise be reported as if it were a conflict.
ID_NAME=$(git config user.name || true)
ID_EMAIL=$(git config user.email || true)
[ -n "$ID_NAME" ] || ID_NAME=hardlysleeping
[ -n "$ID_EMAIL" ] || ID_EMAIL=67934624+hardlysleeping@users.noreply.github.com

git fetch --tags --quiet "$UPSTREAM"
git rev-parse --verify --quiet "$UPSTREAM/$BRANCH" >/dev/null || die "$UPSTREAM/$BRANCH does not exist" 4

BEHIND=$(git rev-list --count "HEAD..$UPSTREAM/$BRANCH")
if [ "$BEHIND" -eq 0 ]; then
  say "up to date: $BRANCH already contains every commit on $UPSTREAM/$BRANCH"
  exit 0
fi
say "upstream has $BEHIND commit(s) we do not:"
git --no-pager log --oneline "HEAD..$UPSTREAM/$BRANCH" | sed 's/^/  /'
if [ "$MODE" = check ]; then exit 10; fi

# --- the merge ---------------------------------------------------------------
BEFORE=$(git rev-parse HEAD)
MERGED=0
PUSHED=0
# If this script is killed between the merge and the verdict (Ctrl-C, a cron timeout, a
# closed terminal) the tree would be left merged but never tested. Put it back instead:
# a half-finished sync must never look like a finished one.
rollback() {
  if [ "$MERGED" = 1 ] && [ "$PUSHED" = 0 ]; then
    git reset --hard "$BEFORE" >/dev/null 2>&1 || true
    printf 'SYNC-FAILED: interrupted after the merge; rolled back to %s\n' "$BEFORE" >&2
  fi
}
trap 'rollback' INT TERM HUP
if ! git -c user.name="$ID_NAME" -c user.email="$ID_EMAIL" \
     merge --no-ff --no-edit "$UPSTREAM/$BRANCH" >/tmp/hwa-sync-merge.log 2>&1; then
  CONFLICTS=$(git diff --name-only --diff-filter=U | tr '\n' ' ')
  git merge --abort 2>/dev/null || true
  say "conflicting files: $CONFLICTS"
  say "the tree is back to $BEFORE - nothing changed."
  say "resolve by hand:  git merge --no-ff $UPSTREAM/$BRANCH"
  say "rule for README.md/CHANGELOG.md: keep upstream's structure, re-place our additions."
  exit 2
fi
MERGED=1
say "merged $UPSTREAM/$BRANCH (clean)"

# --- version follows the upstream tag ----------------------------------------
# Upstream keeps the version in the tag and CHANGELOG; the fork needs a VERSION
# file for /api/version and for the desktop bundle, so derive it and never let a
# release sync ship a stale number.
TAG=$(git describe --tags --abbrev=0 "$UPSTREAM/$BRANCH" 2>/dev/null || true)
if [ -n "$TAG" ]; then
  WANT=${TAG#v}
  HAVE=$(cat VERSION 2>/dev/null | tr -d '[:space:]' || true)
  if [ "$WANT" != "$HAVE" ]; then
    printf '%s\n' "$WANT" > VERSION
    git add VERSION
    git -c user.name="$ID_NAME" -c user.email="$ID_EMAIL" \
        commit -q -m "Version: follow upstream tag $TAG"
    say "VERSION $HAVE -> $WANT (from tag $TAG)"
  fi
fi

# --- tests gate the push -----------------------------------------------------
# The repo's own venv first: a PYTHON left in the environment by something else (another
# worktree, another project) should not decide which interpreter proves this tree green.
if [ -x "$REPO/.venv/bin/python3" ]; then
  PY="$REPO/.venv/bin/python3"
elif [ -n "${PYTHON:-}" ] && [ -x "${PYTHON}" ]; then
  PY="$PYTHON"
else
  PY=$(command -v python3)
fi
say "running the suite with $PY"
# A hung test run is a failed test run. 900s is generous: the suite takes ~6s green,
# and the slowest red case (a backend that never answers /api/health) is bounded by the
# sandbox's own 30s wait per test.
TEST_TIMEOUT=${TEST_TIMEOUT:-900}
# Servers the suite started inherit this marker, so a killed run can clean up exactly its
# own strays and nothing else. Matching on "server.py" would be how you kill the user's
# real app, which is the one thing this script must never do.
MARK="hwa-sync-$$"
clean_strays() {
  for envfile in /proc/[0-9]*/environ; do
    if grep -qa "HWA_SYNC_RUN=$MARK" "$envfile" 2>/dev/null; then
      pid=${envfile#/proc/}; pid=${pid%%/*}
      kill "$pid" 2>/dev/null || true
    fi
  done
}
HWA_SYNC_RUN="$MARK" PYTHON="$PY" timeout "$TEST_TIMEOUT" bash scripts/ci-local.sh >/tmp/hwa-sync-tests.log 2>&1 || RC=$?
clean_strays
RC=${RC:-0}
if [ "$RC" -ne 0 ]; then
  tail -15 /tmp/hwa-sync-tests.log | sed 's/^/  /'
  git reset --hard "$BEFORE" >/dev/null
  MERGED=0
  git clean -fd VERSION >/dev/null 2>&1 || true
  if [ "$RC" = 124 ]; then say "the suite timed out after ${TEST_TIMEOUT}s"; fi
  say "rolled back to $BEFORE - nothing changed."
  say "the merged tree failed its own tests; see /tmp/hwa-sync-tests.log"
  exit 3
fi
tail -3 /tmp/hwa-sync-tests.log | sed 's/^/  /'

# --- push --------------------------------------------------------------------
if [ "$MODE" = nopush ]; then
  say "merged and green, not pushed (--no-push). Look, then: git push origin $BRANCH"
  exit 0
fi
git push --quiet origin "$BRANCH"
PUSHED=1
say "pushed to origin/$BRANCH: $(git rev-list --count "$BEFORE..HEAD") new commit(s) from upstream"
