# Rules for anyone (human or agent) working in this repo

This repo is a **fork used to build the app**. The stable, running app lives elsewhere and must not
be touched by work done here.

## The two protected things

| Protected | Where |
|---|---|
| The live checkout | `~/apps/hardlywatching` — **no writes, no commits, no file edits, ever** |
| The running service | `hardlywatching.service` (user unit) on `127.0.0.1:8765` — **no start/stop/restart/enable/disable** |

Also protected, because they are the running app's data:

- `~/.config/hardlywatching/` (`state.json`, `gallery-cache.db`, sessions) — never read-write, never
  created, never deleted.
- `~/instagram/` and any other archive root — no downloads, no deletes, no moves.
- Any non-GET request to `127.0.0.1:8765` or to the tailnet IP on 8765. Read-only `GET` is allowed
  only when a task explicitly asks for a baseline comparison.

If a task appears to need any of the above, stop and report it — do not improvise a workaround.

## Where work happens

One `git worktree` per task, on its own branch, so parallel work cannot collide:

    /home/bazzite/.hermes/worktrees/hwa/<task-name>     branch: task/<task-name>

Rules:

- Edit only inside your own worktree. Never edit another worktree.
- Touch only the files your task lists. If a fix needs a file outside that list, report it instead
  of editing it — another task owns it and the parent merges.
- Commit on your branch as you go. **Do not push** and do not touch other branches. The parent
  reviews and merges.
- Python for tests: `/home/bazzite/apps/HardlyWatchingApp/.venv/bin/python3` (has Pillow and
  pillow-heif). Do not `pip install` into it, and never install into the live checkout's venv.

## Sandbox only — no writes to real state

Every run, test and manual exercise uses a throwaway environment:

    export HOME=$(mktemp -d)                       # nothing lands in the real ~/.config
    export INSTA_STATE="$HOME/state.json"
    export INSTA_CACHE_DB="$HOME/gallery-cache.db"
    export INSTA_OUTPUT="$HOME/instagram"
    export INSTA_THUMBS="$HOME/thumbs"
    export INSTA_AVATARS="$HOME/avatars"
    export INSTA_PORT=8899                          # never 8765
    export INSTA_HOST=127.0.0.1

Downloads in tests go to a **stub** `instaloader` / `yt-dlp` on `PATH`, never the real binaries.
The stub pattern already exists in the skill's `scripts/demo-instaloader-stub.py`.

## What "done" means here

- The verification command in the task actually ran, and the output is in the report. Not "should
  work".
- The web UI still does everything it did before. Dropping a button, tab, field or label to tidy a
  layout counts as a regression.
- `git status` in your worktree is clean (everything committed).
- Report: branch name, files changed, the exact commands run with their real output, anything you
  could not verify, and anything you had to guess.

## Publishing

Nothing here is published. No repo is made public, no release is cut, no artifact uploaded anywhere
until the owner names the target explicitly.
