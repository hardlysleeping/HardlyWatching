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
| M0-TESTS | 1–3 | Sandbox harness, API smoke suite, UI baseline capture | `hwa/m0-tests` |
| M1-LOCK | 6, 7, 8, 23 | Instance lock, `--attach`, `--no-browser`, `/api/version` | `hwa/m1-lock` |
| M2-SCAFFOLD | 9, 10 | Rust toolchain + Tauri 2 shell loading the real UI | `hwa/m2-scaffold` |
| M2-SIDECAR | 11 | PyInstaller onedir sidecar (no system Python) | `hwa/m2-sidecar` |

## In Progress

| Card | Row | Item | Owner | Branch | Worktree |
|---|---|---|---|---|---|
| M0-TESTS | 1–2 | Sandbox harness + API smoke suite | agent sa-0 | `task/m0-tests` | `~/.hermes/worktrees/hwa/m0-tests` |
| M1-LOCK | 6–8, 23 | Instance lock, `--attach`, `--no-browser`, `/api/version` | agent sa-1 | `task/m1-lock` | `~/.hermes/worktrees/hwa/m1-lock` |
| M2-SCAFFOLD | 9–10 | Rust + Tauri 2 shell loading the real UI | agent sa-2 | `task/m2-scaffold` | `~/.hermes/worktrees/hwa/m2-scaffold` |
| M2-SIDECAR | 11 | PyInstaller onedir sidecar | agent sa-3 | `task/m2-sidecar` | `~/.hermes/worktrees/hwa/m2-sidecar` |

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

## Blocked

| Card | Row | Blocked on |
|---|---|---|
| AUTH | 20–21 | Owner: is a one-time token/pairing step on the phone PWA acceptable? |
| M2-SIDECAR-SIZE | 11 | Owner: is ~200 MB per desktop install acceptable for "no Python needed"? (Work proceeds; the answer decides whether it ships.) |
| IOS | 36–37 | No macOS/Xcode on this machine; signing path needed |

---

## Merge order (parent does this, children do not push)

1. `hwa/m0-tests` — foundation; every later card's verification leans on it.
2. `hwa/m1-lock` — `server.py` alone, so it merges without touching UI code.
3. `hwa/m2-scaffold` — new `src-tauri/` tree only, no Python files.
4. `hwa/m2-sidecar` — new `packaging/sidecar/` tree only.

Conflicts are avoided by file ownership, not by merge skill: two cards never touch the same file.
