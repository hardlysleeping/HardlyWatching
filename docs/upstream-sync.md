# Keeping the app fork in step with upstream

Two repositories exist because the desktop app could not be built where the live app runs:

| | repo | what it is | who touches it |
|---|---|---|---|
| upstream | `hardlysleeping/HardlyWatching` | the live app: the checkout at `~/apps/hardlywatching`, the `hardlywatching.service` unit, the real archive | you, by hand |
| fork | `hardlysleeping/HardlyWatchingApp` | the same history plus `src-tauri/`, `packaging/sidecar/`, `tests/`, `scripts/` | this work |

The fork is not a rewrite. Every file that makes the app download and browse is upstream's
file; the fork adds files beside them. That is what makes syncing cheap, and it is the only
rule that matters: **add files, don't rewrite them.**

## Syncing

```bash
cd ~/apps/HardlyWatchingApp
scripts/sync-upstream.sh --check    # anything new upstream? exit 10 = yes
scripts/sync-upstream.sh            # merge, test, push
scripts/sync-upstream.sh --no-push  # merge and test, leave it to look at
```

What it does, in order, and what it refuses to do:

1. Refuses to run on a dirty tree or off `master` — exit 4, nothing touched.
2. `git fetch --tags upstream`, and stops at "up to date" (exit 0) when there is nothing new.
3. Merges `upstream/master` as a merge commit. **On a conflict it aborts the merge and puts the
   tree back exactly where it was** (exit 2), listing the files. It never resolves a conflict
   itself: a wrong guess in `README.md` or `server.py` is how the app quietly stops matching its
   own documentation.
4. Sets `VERSION` from the newest tag on `upstream/master` (`v0.2.0` → `0.2.0`). Upstream keeps
   the version in the tag and the CHANGELOG; the fork needs a file for `/api/version` and for the
   bundle, so the number is derived rather than remembered.
5. Runs the suite (`scripts/ci-local.sh`) with a timeout. **Green or the merge is rolled back**
   (exit 3) — a red merge never reaches `origin`, and never lingers in the working tree.
6. Pushes. Only after 4 and 5.

If the script is interrupted between the merge and the verdict (Ctrl-C, a cron timeout, a closed
terminal) a trap rolls the tree back, and any server the test run left behind is killed by an
environment marker it inherited — not by matching `server.py`, which is how you would kill the
real app.

## The conflict surface, honestly

Most syncs are trivial because the two sides own different files. These are the files both sides
have touched, i.e. where a conflict can actually happen:

| file | upstream does | the fork does | rule |
|---|---|---|---|
| `README.md` | rewrites sections freely; it was cut from 1061 to 627 lines for 0.2.0 | adds the lock / `--attach` / `--no-browser` / `--force` / `/api/version` paragraphs | keep upstream's structure, re-place the additions in the section that now owns the topic; prefer a shorter addition over a reflowed README |
| `CHANGELOG.md` | folds `Unreleased` into a release with a date | adds bullets under `Unreleased → Added` | let upstream cut the release; the fork's bullets belong in the same list, in the same voice |
| `server.py` | the downloader, the routes, the UI's data | the instance lock, `--attach`, `/api/version` | upstream wins on behaviour; the fork's additions sit at the top of the file and in the route table, so they rarely collide |
| `run.py` | the launcher | flag passthrough for `--attach` / `--no-browser` / `--force` | keep upstream's structure; the fork's `--attach` short-circuit must stay before venv/instaloader setup |
| `.gitignore` | grows | app build dirs | append only, both ways |
| `VERSION` | does not exist upstream | exists, derived from the upstream tag | the script owns it; don't hand-edit except to match a new release |

Everything else — `src-tauri/`, `packaging/sidecar/`, `tests/`, `scripts/`, `docs/desktop-shell.md`,
`docs/ci.md`, `KANBAN.md`, `BACKLOG.md`, `.github/` — upstream has no opinion about, so changes
there cannot conflict.

Two habits keep that true:

- App-only behaviour goes in app-only files. The desktop shell's spawn-or-attach logic lives in
  `src-tauri/src/main.rs`; nothing about it is added to `server.py` if a flag will do.
- Anything longer than a paragraph about the app belongs in `docs/`, with one sentence and a link
  in `README.md`. A link survives a README rewrite; a 30-line section does not.

## When this becomes the only repository

The plan is not to maintain two. Once the desktop build is stable and you would rather run this
copy everywhere:

1. Point the live checkout at this repo and fast-forward it:
   ```bash
   cd ~/apps/hardlywatching
   git remote set-url origin git@github.com:hardlysleeping/HardlyWatchingApp.git
   git fetch origin && git merge --ff-only origin/master    # the fork contains all upstream history
   git status                                               # the 10 uncommitted files are already committed in the fork as 7419341
   ```
   Nothing is lost: the fork's history contains upstream's, so the old checkout becomes a clone of
   the new canonical repo with the same commits, plus the app.
2. Keep `hardlywatching.service` as it is — it runs `server.py`, which is unchanged in behaviour.
   The desktop app attaches to that running instance instead of starting a second one.
3. Archive `hardlysleeping/HardlyWatching` on GitHub (read-only) rather than deleting it, and drop
   the `upstream` remote from the fork: from then on `origin` is the only place changes come from.
4. `scripts/sync-upstream.sh` becomes a no-op ("up to date") and can be left in place or deleted.

Step 1 is the point of no return, so it happens when the app is proven, not before. Until then the
live app is upstream's, untouched, and this fork follows it.

## What is not automated

- Nothing runs `sync-upstream.sh` for you on a machine you are not using; a scheduled job does (see
  below). A sync that finds a conflict stops and waits for a human — by design.
- The GitHub Actions workflow in the fork does not sync. It would need a token with read access to
  a private sibling repository, which is a credential to leak for very little; the local script
  already has ssh access.
