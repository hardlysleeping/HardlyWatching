# CI

Two files, one set of steps:

| File | What it is |
|---|---|
| `.github/workflows/ci.yml` | GitHub Actions workflow: one Linux job on every push and on pull requests to `master` |
| `scripts/ci-local.sh` | The same steps for this machine, so the workflow is verifiable without Actions |

The steps are **duplicated on purpose**, not delegated: the workflow does not call `ci-local.sh`.
If someone edits one side and not the other, the difference is visible in review instead of a single
file becoming the only source of truth for what "green" means.

## What runs (in this order)

1. `actions/checkout@v4`
2. `actions/setup-python@v5` with **Python 3.12**
3. `python -m pip install --upgrade pip` then the package list (see below)
4. **Compile check** — `python -m compileall -q server.py engine.py shorts.py profile_db.py control-panel.py`
   Catches a syntax error in a module the suite does not import, in under a second.
5. **Tests** — `python -m unittest discover -s tests -v`, then a guard that reads the `Ran N tests`
   summary line. A green run with zero tests (broken discovery, wrong `-s` path) fails the job.
   `scripts/ci-local.sh` has the identical guard.

Job: `Unit tests (ubuntu-latest, Python 3.12)`. `permissions: contents: read`, 15-minute timeout,
concurrency group per ref with `cancel-in-progress` so a new push cancels the previous run.

## Is the suite version-sensitive?

No, not across the versions that matter here. `python -m unittest discover -s tests -v` was run on
**3.11.15, 3.12.10 and 3.14.7** and returned `Ran 46 tests ... OK` on all three. The code only uses
`from __future__ import annotations`, `dataclasses`, `http.server` and `sqlite3`, all stable since
3.9. `3.12` is pinned in the workflow because it is the oldest version with current wheels for
Pillow and is available on `ubuntu-latest` without extra setup — not because the suite needs it.

## Dependencies

The suite needs **no third-party package at all**. It was verified in a venv containing only `pip`:
`Ran 46 tests ... OK`. `PIL`, `pillow_heif`, `instaloader` and `requests` are all imported *inside*
functions in `server.py`/`engine.py`, and no test reaches those paths (`tests/sandbox.py` stubs the
downloaders with a fake binary and an embedded 16×16 JPEG precisely so no image library is needed).

`Pillow>=10.4,<12` is installed anyway. It is not what makes the suite green; it is there so CI runs
in an environment shaped like a real install, and so a future test that touches thumbnails or
uploads does not fail for want of a package nobody remembered to add. `pillow-heif` is not installed:
it is only needed for HEIC input, and its wheels pull a heavier dependency chain than the job earns.

If a test ever gains a real dependency, it must be added to **both** lists — the `PIP_PACKAGES` array
in `scripts/ci-local.sh` and the `pip install` line in `.github/workflows/ci.yml`.

## What it deliberately does not cover

- **No Windows and no macOS.** The suite is one Linux job. Those platforms are not on the MVP path
  and cannot be built or verified on this machine; they get their own job when the bundles do
  (backlog rows 16–19).
- **No real downloads.** `tests/sandbox.py` puts stub `instaloader`/`yt-dlp` binaries on `PATH` and
  points every writable path at a temp tree. Nothing here proves the real downloaders work, and it
  is not supposed to: no network, no credentials, no archive.
- **No Tauri/Rust build.** `src-tauri/` is not compiled, `cargo` is not installed, no DEB/AppImage
  is produced. That is a separate pipeline (backlog rows 9–15) and needs a container with the GTK/WebKit
  build deps.
- **No Python packaging.** No PyInstaller sidecar, no wheel, no artifact upload.
- **No linting or type checking.** The repo has no linter config; `compileall` is the only static
  check, and it only finds syntax errors.
- **No UI screenshots.** The UI baseline (backlog row 3) is a separate card; this job would stay
  green if a button disappeared from `index.html`.
- **No coverage measurement.** Nothing asserts *which* code ran, only that 46 tests passed.

## Running it locally

```bash
cd ~/.hermes/worktrees/hwa/<task>      # or any checkout of this repo
bash scripts/ci-local.sh
```

It creates a throwaway venv at `.ci-venv/` in the repo root (git-ignored), installs the same package
list as the workflow, compile-checks the same five modules, then runs the same unittest command
verbosely. It is idempotent — a second run reuses the venv. The last line is either

```
PASS: Ran 46 tests in 7.123s (compile check + unittest discover, Python 3.14.7)
```

or a `FAIL:` line, with a non-zero exit status in both the test-failure and the compile-failure case.

To use an interpreter you already trust and skip venv creation entirely:

```bash
PYTHON=/usr/bin/python3.12 bash scripts/ci-local.sh
```

`PYTHON` also skips the `pip install` step, so do not point it at a venv you care about (in
particular not `~/apps/HardlyWatchingApp/.venv` — that one is the live app's and must not be
modified). Any interpreter is fine for the suite itself, which is why this works.

### Proving the harness can actually fail

A CI script that cannot go red is decoration. To check it: copy the worktree somewhere temporary, add
`raise SystemExit(3)` at the top of one test module in the copy, and run the copy's script —

```bash
cp -r . /tmp/hwa-floor && cd /tmp/hwa-floor
printf 'raise SystemExit(3)\n' | cat - tests/test_api_smoke.py > t && mv t tests/test_api_smoke.py
bash scripts/ci-local.sh; echo "exit=$?"
```

It must exit non-zero and print `FAIL:`. Restore the file (or just delete the copy) afterwards.

## What is not verified here

The workflow file itself has never been executed. There is no GitHub runner on this machine and no
push to this repository, so `.github/workflows/ci.yml` is checked only by reading it and by the local
script that mirrors its steps. First real signal comes from the first push to `master` after merge.
