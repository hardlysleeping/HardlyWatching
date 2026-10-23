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
| M2-WIRE | 12–13 | Sidecar wired into the shell as `externalBin` (spawn-or-attach), Linux DEB + AppImage bundle | `hwa/m2-wire` |
| CI | 4 | GitHub Actions: run the 46-test suite on push | `hwa/ci` |
| UI-BASELINE | 3 | DOM landmarks + screenshots captured from a DEMO archive (never the real one) | `hwa/ui-baseline` |
| DOCS-FLAGS | 24 (partial) | README: `--attach`, `--no-browser`, the lock, `/api/version` | `hwa/docs-flags` |

## In Progress

| Card | Row | Item | Owner | Branch | Worktree |
|---|---|---|---|---|---|
| CI / DOCS-FLAGS / UI-BASELINE | 3, 4, 24 | wave 2 running (CI workflow, README flags, UI baseline from a demo archive) | agents | `task/{ci,docs-flags,ui-baseline}` | `~/.hermes/worktrees/hwa/*` |

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
| M0-TESTS | 1–2 | Sandbox harness + 29-case API smoke suite | merged `ab57e33`; re-run by parent on merged master: **46 tests OK**; sandbox proves isolation (temp HOME, stub downloaders, port ≥8899) |
| M1-LOCK | 6, 7, 8, 23 | Instance lock, `--attach`, `--no-browser`, `/api/version` | merged; 17 lock tests OK; manual on master: second start exits 0 with "already running at http://127.0.0.1:8899", `--attach` 0, `/api/version` 0.1.0, one listener, clean stop |
| M2-SCAFFOLD | 9–10 | Tauri 2 shell, built in a Fedora container, window proven to render | host `cargo build` fails (no `dbus-1.pc` on immutable Bazzite); `build-in-container.sh build` → Finished dev in 1m48s; windowed run: window `main` 1280x860, and a logging proxy recorded `GET /` (369,713 B) + 13× `/api/state` + `/api/gallery` + `/api/autostart` + `POST /api/excluded` in 12 s. Repro in `docs/desktop-shell.md` |
| M2-SIDECAR | 11 | PyInstaller onedir bundle, no system Python | merged; 113 MB; `env -i` run with no venv: `/api/health`, `/`, `/shorts`, `/api/version` all 200/0.1.0; SIGINT clean. Caveats: ffmpeg is dynamically linked (needs FFmpeg 8.x sonames), instaloader not bundled, glibc ≥ 2.38 required |


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
| INSTALOADER-BUNDLE | `instaloader` is not in the bundle: the UI serves and the archive browses, but nothing Instagram-side downloads until it is installed. Decide bundle-vs-document. |
| GLIBC | The bundle needs glibc ≥ 2.38 (built here, FC44). Ubuntu 22.04 will not start it. CI on an older base image fixes that. |

---

## Merge order (parent does this, children do not push)

Merged so far, each re-verified by the parent on merged `master` (46 tests green): `task/m0-tests`,
`task/m1-lock`, `task/m2-sidecar`, `task/m2-scaffold`.

Next: `hwa/m2-wire` (needs the container build), then `hwa/ci`, `hwa/docs-flags`, `hwa/ui-baseline`.

Conflicts are avoided by file ownership, not by merge skill: two cards never touch the same file.
