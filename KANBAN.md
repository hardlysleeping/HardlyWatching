# Kanban — HardlyWatching App

Board for the app conversion. Columns move left to right; a card carries the backlog row number so
the two documents never disagree. Backlog detail: `BACKLOG.md`. Design: plan
`~/.hermes/plans/2026-10-23_151709-hardlywatching-app.md`.

**MVP target (stable, in this order):** tests green → backend hardened (lock, version, auth) →
Linux desktop that runs with no system Python → then Android. Windows/macOS/iOS follow the same
code, they are not on the MVP path because neither can be built or verified on this machine.

**WIP limit: 4 cards in progress.** Each in-progress card is one worktree, one branch, one owner.

---

## Backlog

| Card | Row | Item |
|---|---|---|
| AUTH | 20–21 | Token auth + per-device tokens + pairing (`server.py`) — **blocked on owner's answer** |
| PHONE-DOC | 24 | README phone section rewritten for the token step |
| WIN-MAC | 16–18 | Windows/macOS bundles in CI |
| CONNECT | 25, 27–30 | Connection screen, QR pairing, secure storage, reconnect, `docs/remote.md` |
| ANDROID | 31–34 | NDK, `android init`, APK build, mobile integration, signed APK |
| IOS | 35–37 | iOS init, sideload docs, signed IPA (blocked: signing) |
| RELEASE | 38–40 | `release.yml`, notes template, release checklist |
| POST-MVP | 41–42 | On-device downloading (Android, iOS experimental) |

## Ready

| Card | Row | Item | Worktree |
|---|---|---|---|
| CI-PROVE | 4 | First push is the first real Actions run — confirm the workflow is green on GitHub, then leave it alone | — |
| RELEASE | 38–40 | `release.yml`: on a tag, build the DEB + AppImage in the container and attach both to a GitHub Release | `hwa/release` |

## In Progress

| Card | Row | Item | Owner | Branch | Worktree |
|---|---|---|---|---|---|
| — | — | (nothing in progress) | — | — | — |

## Review

| Card | Row | Item | Branch |
|---|---|---|---|
| — | — | — | — |

## Done

| Card | Row | Item | Evidence |
|---|---|---|---|
| FORK | — | New private repo + local copy with full history and the uncommitted checkpoint | `hardlysleeping/HardlyWatchingApp`, commit `7419341` |
| PLAN | — | Plan v1 + backlog | plan path above, `BACKLOG.md` `7c69c7b` |
| RULES | — | Guardrails for work in this repo (protected paths, sandbox env, done criteria) | `docs/agent-rules.md` |
| PUBLISH | — | The app published in the regular repo, internal board/rules excluded | `hardlysleeping/HardlyWatching` master `48c2801`, CI green `40058852153` |
| RELEASE-030 | 38–40 | 0.3.0 cut by hand: changelog, tag, AppImage + DEB attached | release `v0.3.0`; assets `uploaded`, 34,232,824 B / 36,995,800 B; download 200 via curl; CI on the tag green `40067374194` |
| PHONE | — | Settings → Open on your phone: LAN/tailnet URL, copy, live switch; server gates non-loopback requests | `/api/network`, `_lan_gate_allows`; 79 tests; LAN 403 → on → 200 verified from 10.0.0.10 |
| LIMIT-HOLD | — | A 429 pause stays paused after the cooldown; banner says so; `hold_after_limit` | `_release_limit_pause`, `tests/test_rate_limit_hold.py` |
| M0-TESTS | 1–2 | Sandbox harness + 29-case API smoke suite | merged `ab57e33`; re-run by parent on merged master: **46 tests OK**; sandbox proves isolation (temp HOME, stub downloaders, port ≥8899) |
| M1-LOCK | 6, 7, 8, 23 | Instance lock, `--attach`, `--no-browser`, `/api/version` | merged; 17 lock tests OK; manual on master: second start exits 0 with "already running at http://127.0.0.1:8899", `--attach` 0, `/api/version` 0.1.0, one listener, clean stop |
| M2-SCAFFOLD | 9–10 | Tauri 2 shell, built in a Fedora container, window proven to render | host `cargo build` fails (no `dbus-1.pc` on immutable Bazzite); `build-in-container.sh build` → Finished dev in 1m48s; windowed run: window `main` 1280x860, and a logging proxy recorded `GET /` (369,713 B) + 13× `/api/state` + `/api/gallery` + `/api/autostart` + `POST /api/excluded` in 12 s. Repro in `docs/desktop-shell.md` |
| CI | 4 | Actions workflow + a local twin that runs the identical steps | merged; parent ran `PYTHON=/….venv/bin/python3 bash scripts/ci-local.sh` on merged master → `PASS: Ran 46 tests in 5.365s`. Workflow itself is unrun until the first push (no runner here). Floor drilled by the agent: sabotage → exit 1 both for a failing test and a syntax error |
| DOCS-FLAGS | 24 | README/CHANGELOG: lock, `--attach`, `--no-browser`, `--force`, `INSTA_LOCK`, `/api/version` | merged; additions only (62 insertions, 0 deletions), README makes no claim about a second instance moving ports (there was no such sentence to correct) |
| UI-BASELINE | 3 | DOM landmarks + screenshots from a DEMO archive | merged; parent-verified: 4 PNGs are 1440x900 ×3 and 390x844 ×1 by header, 547-line `dom.txt`, `capture.sh` refuses 8765, leak scan of 739 real profile names against baseline text = 0 hits (only `tiktok`, a platform name), and the gallery shot shows three `demo.*` personas |
| SYNC-UPSTREAM | — | `scripts/sync-upstream.sh` + `docs/upstream-sync.md` + a daily cron job | merge → VERSION from tag → suite → push; conflict aborts and restores, red rolls back. Proven in a scratch clone with a fake upstream: clean merge (VERSION 0.2.0→0.3.0, 46 PASS, pushed), same-line conflict (exit 2, HEAD unchanged, no MERGE_HEAD), broken version (exit 3, rolled back, 0 listeners on 89xx), `--check` exit 10, current exit 0. Job `469cbec772f5`, 5am daily, silent when up to date |
| SYNC-1 | — | First real upstream sync merged (upstream `ce1abbc`: README cut to 627 lines, risk-first; CHANGELOG folded into 0.2.0) | one conflict (control-panel paragraph) resolved to upstream's shorter wording; fork's lock/flags paragraphs survived; `VERSION` 0.1.0→0.2.0 from tag `v0.2.0`; the four lock tests had the version hard-coded and now read `VERSION`; 46 tests OK |
| RUN-PY-FLAGS | follow-up | `run.py` now forwards `--attach`, `--no-browser`, `--force` | parent-verified: `run.py --attach` → `http://127.0.0.1:8897` exit 0 without installing anything; `--force` while held → second instance on 8898, first keeps the lock; `--no-browser` with `BROWSER=/bin/echo` printed 0 browser lines vs 1 without the flag |
| INSTALOADER-BUNDLE | 44 | instaloader ships inside the bundle, so Instagram downloads work with no Python on the host | merged. `entry.py --run-instaloader` calls instaloader's own CLI inside the frozen interpreter and `build.sh` writes `bin/instaloader` = `exec ../hardlywatching-server --run-instaloader "$@"` (a second EXE or a copied console script were both wrong: one needs its own build, the other needs the host's python). `--run-instaloader` is answered before the server's import-time argv parsing. Under `env -i`, no Python: `bin/instaloader --version` → 4.15.4; a real `--count 1` run resolved a profile, wrote its picture + `json.xz`, then hit Instagram's own `429` with a 664 s retry (rate limit, not a freeze problem). DEB 72.93 MiB / `Installed-Size` 136626 / 166 members extracted and checked; AppImage 73,669,112 B run with a throwaway HOME → spawned server on 8895, health ok, version 0.2.0, `/api/state` resolved instaloader *inside* the AppImage; SIGTERM to AppRun left nothing listening. +1.5 MB (129 MB installed). `tests/test_sidecar_instaloader.py` keeps spec/wrapper/entry in step — 53 tests green |
| M2-WIRE | 12–13 | Shell ships its own server; spawn-or-attach; DEB + AppImage | merged. Bundled PyInstaller onedir (128 MB, `bin/ffmpeg|ffprobe|yt-dlp` included) at `usr/lib/HardlyWatching/sidecar`; onedir chosen over onefile with measurements (0.08 s vs 0.31 s cold start, onefile can't carry `bin/`). Parent-verified end to end: `HardlyWatching_0.2.0_amd64.deb` 72.75 MiB `Version: 0.2.0`, extracted and run with a throwaway HOME → spawned its own bundled server on 8899, `/api/health` 200, `/api/version` 0.2.0, SIGTERM to the shell → no server, no listener; AppImage 73,464,312 B extracted and run the same way → 200/0.2.0. Attach path leaves a foreign server alone; only a child it spawned is reaped (PDEATHSIG as backstop). Version now one number: `VERSION` → `tauri.conf.json`/`Cargo.toml` at build, enforced by 4 new tests (50 total green) |
| BACKUP | 46–47 | Backup & restore in Settings; `INSTA_DATA` one-folder mode | merged. `backup.py` (zip of `state.json` + `profiles.db` + optional session, manifest with version/counts/root), five routes (`info`, `export`, `download`, `import`, `auto`), a collapsed Settings card with paths, sizes and the saved list. Restore validates before writing, keeps `hw-prerestore-*.zip`, remaps the archive root, reloads the engine in-process and comes back **paused**; 409 while jobs run. Parent proved it live in a browser on :8899: startup auto-backup appeared on its own, "Back up now" wrote a second zip, export downloaded `hardlywatching-backup-2026-10-23.zip` (2,764 B), restoring an older zip through the file input produced `hw-prerestore-*.zip`, reloaded the page and the gallery still listed 5 files. Round trip between two sandboxes with different roots in `tests/test_backup.py` (10 cases) — 63 green |
| M2-SIDECAR | 11 | PyInstaller onedir bundle, no system Python | merged; 113 MB; `env -i` run with no venv: `/api/health`, `/`, `/shorts`, `/api/version` all 200/0.1.0; SIGINT clean. Caveats at the time: ffmpeg is dynamically linked (needs FFmpeg 8.x sonames), instaloader not bundled (fixed by INSTALOADER-BUNDLE above), glibc ≥ 2.38 required |


## Blocked

| Card | Row | Blocked on |
|---|---|---|
| AUTH | 20–21 | Owner: is a one-time token/pairing step on the phone PWA acceptable? |
| M2-SIDECAR-SIZE | 11 | Owner: is ~113 MB per desktop install acceptable for "no Python needed"? (Built and verified; the answer only decides whether it ships.) |
| IOS | 36–37 | No macOS/Xcode on this machine; signing path needed |

## Follow-ups discovered by the sidecar/first pass (not new scope, but must not be lost)

| Card | Item |
|---|---|
| FFMPEG-STATIC | The bundled ffmpeg is dynamically linked and needs FFmpeg 8.x sonames (`libavcodec.so.62`, …). On a machine without FFmpeg 8 it dies and video thumbnails/previews go with it. Fix in CI with a static/trimmed ffmpeg, not by copying this box's libs. |
| GLIBC | The bundle needs glibc ≥ 2.38 (built here, FC44). Ubuntu 22.04 will not start it. CI on an older base image fixes that. |
| TILE-WRAP | Long profile names break mid-word in gallery tiles (`demo.slow.mornin\ngs` in the baseline shot). Cosmetic, seen in `tests/baseline/gallery-1440x900.png`, not fixed. |
| VERSION-TAURI | `VERSION` is the one number, but `src-tauri/tauri.conf.json` still says `0.1.0` and the bundle takes its version from there. Fold the two together (generate the Tauri version from `VERSION`) before the first release, or the DEB will disagree with `/api/version`. |
| CRON-GATEWAY | The daily sync job needs the Hermes gateway running (`hermes gateway start`) before it will fire at all. Local-only delivery otherwise: the run is recorded, nothing is sent. |
| APPDIR-META | `pack-appimage.sh` writes its own `.desktop` entry because Tauri's appimage step cannot run; if `productName`/description changes in `tauri.conf.json` the two drift. Derive it from the config, or accept and keep them adjacent in the file. |
| CI-DEPS | `scripts/ci-local.sh` installs `Pillow>=10.4,<12` although the suite passes with only `pip` — the list mirrors a real install so image tests have something to import. `pillow-heif` deliberately omitted. |

---

## Merge order (parent does this, children do not push)

Merged so far, each re-verified by the parent on merged `master`: `task/m0-tests`, `task/m1-lock`,
`task/m2-sidecar`, `task/m2-scaffold`, `task/ci`, `task/docs-flags`, `task/ui-baseline`.

Next: `hwa/m2-wire` — the last card between here and a no-Python Linux install.

Conflicts are avoided by file ownership, not by merge skill: two cards never touch the same file.
