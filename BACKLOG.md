# Backlog — HardlyWatching App

Converting the HardlyWatching web UI into installed apps (Linux/Windows/macOS + Android/iOS) on one
shared frontend and one Python backend, **without changing the web UI's behaviour**.

Plan: `/home/bazzite/.hermes/plans/2026-10-23_151709-hardlywatching-app.md` (v1, 2026-10-23).
Priority order below is that plan's order; the plan is the design, this file is the state.

Status: `done` / `wip` / `next` / `todo` / `blocked`

Baseline: `7419341` — full history of `hardlysleeping/HardlyWatching` plus the uncommitted
AppImage/control-panel/bookmarks work from the live checkout. The live checkout
(`~/apps/hardlywatching`) is untouched and still runs everything.

## Priority order from here (2026-10-23)

1. **Row 1 — the sandbox + smoke suite.** Nothing else is safe to build without it: every later
   milestone needs a way to exercise the API without touching the real archive.
2. **Row 6 — the instance lock.** A second launch today binds `8766` and starts a second worker on
   the same `state.json` and archive (`server.py:73-88`, `:2354`). That is a live data hazard and it
   is also the desktop shell's core correctness property.
3. **Rows 9–12 — Rust, Tauri shell, sidecar.** The desktop half is the thing that makes this an app.
4. **Row 20 — token auth.** Required before any mobile client exists, because mobile means remote
   access. Blocks on his answer to open question 1 (one-time phone pairing).
5. **Row 26 — Android loopback proxy.** The single riskiest design decision; prove it before any
   frontend work.
6. Windows/macOS (rows 16–19), then iOS (row 32+), then releases (37+).
7. **Everything in "must not regress" stays green at every step.** A dropped button or a dead
   autostart is a regression, not a taste call.

---

## M0 — Baseline and guardrails

| # | Item | Status | Evidence / notes |
|---|---|---|---|
| 1 | Sandbox harness: throwaway server on temp HOME + `INSTA_STATE`/`INSTA_CACHE_DB`/`INSTA_OUTPUT`, port 8899, stub instaloader | done | `tests/sandbox.py`; verifies it latched onto *its own* server by comparing `/api/state.output_root`, not just `/api/health` |
| 2 | API smoke suite over the 12 UI-critical routes | done | 29 cases; `/api/shorts/feed` turned out to be POST (weighted pick records what it showed), `/api/peek` 400s without `?profile=`. Parent re-ran on merged master: **46 tests OK** |
| 3 | UI baseline: screenshots + DOM landmarks, 1440px and 390px, every tab | todo | Plan task 0.3. This is the "no degraded UI" guard |
| 4 | CI: Linux job running 1–2 on push | todo | Plan task 0.4. Repo has no `.github/` at all today |
| 5 | Backlog + plan living in the repo | done | `BACKLOG.md` (this file), plan path above |

## M1 — Backend hardening

| # | Item | Status | Evidence / notes |
|---|---|---|---|
| 6 | Instance lock (`run/hardlywatching.lock` + health probe); second launch exits 0 with the URL | done | Live on master: second start prints `already running at http://127.0.0.1:8899` and exits 0; one listener; stale/unreadable lock taken over; `--force` keeps the canonical lock |
| 7 | `--attach` mode for the shell and `control-panel.py` | done | `--attach` exits 0 with the URL, 1 with nothing running; runs before Engine/bind/browser |
| 8 | `--no-browser` flag | done | Positive control in the tests: the patched `webbrowser.open` does fire without the flag |
| 20 | Token auth: `api_token` in state, loopback bypass, Bearer for non-loopback, 401 JSON | blocked | Plan 1.3. Blocked on open question 1 (phone PWA pairing) |
| 21 | Per-device tokens: pair / list / revoke | blocked | Plan 1.4. Same blocker |
| 22 | CORS/OPTIONS with an origin allowlist | todo | Plan 1.5. No `OPTIONS` handling exists; needed for the mobile fallback path only |
| 23 | `/api/version` (version, pid, port, started, api) | done | Reads `VERSION` (0.1.0), falls back to `dev`; frozen bundle reports 0.1.0 too |
| 24 | README "Reach it from your phone" updated with the token step | blocked | Plan 1.8. Same blocker as row 20 |

## M2 — Desktop shell (Linux first)

| # | Item | Status | Evidence / notes |
|---|---|---|---|
| 9 | Rust toolchain installed (rustup stable) | done | cargo 1.99.1 / rustc 1.99.1 in `~/.cargo/bin` |
| 10 | `src-tauri/` scaffold: a window onto the server URL, no bundled UI copy, no bundler | done | `src/main.rs` resolves URL before Tauri starts (env → `app-config.json` → `:8765`), probes the port, retries until up. Design changed from the plan's `frontendDist`: see `docs/desktop-shell.md` |
| 11 | Sidecar: PyInstaller onedir `hardlywatching-server` + `yt-dlp` + `ffmpeg` | done | 113 MB; `env -i`, no venv: `/`, `/shorts`, `/api/health`, `/api/version` all serve; SIGINT clean. Caveats: rows 43–45 |
| 12 | Spawn-or-attach logic + `externalBin` wiring | todo | Plan 2.4. Depends on rows 6 and 11 |
| 13 | Linux bundles: shell AppImage + DEB | todo | Plan 2.5 |
| 14 | Offline browsing/playback check against the local archive | todo | Plan 2.6 |
| 15 | systemd unit + `control-panel.py` still work, and the shell attaches to the unit | todo | Plan 2.7 |

## M3 — Windows and macOS

| # | Item | Status | Evidence / notes |
|---|---|---|---|
| 16 | CI matrix `windows-latest`, `macos-latest` building sidecar + bundle | todo | Plan 3.1. Neither platform can be built on this workstation |
| 17 | Per-OS `ffmpeg`/`yt-dlp` in the sidecar | todo | Plan 3.2 |
| 18 | DMG noted as unsigned/not notarised in the README | todo | Plan 3.3. Needs a paid Apple account otherwise |

## M4 — Remote access and pairing

| # | Item | Status | Evidence / notes |
|---|---|---|---|
| 25 | Connection screen (URL + 6-char code), shown only when no local server answers | todo | Plan 4.1 |
| 27 | QR pairing payload `hwapp://pair?u=&c=` | todo | Plan 4.2. Depends on row 21 |
| 28 | Credentials in secure storage, not `localStorage` | todo | Plan 4.3. `index.html` currently uses `localStorage` 17× for UI prefs — those stay |
| 29 | Offline banner, auto-reconnect, no false success on a failed action | todo | Plan 4.4 |
| 30 | `docs/remote.md`: HTTPS at the edge (Tailscale/Caddy), never expose the bare port | todo | Plan 4.5 |

## M5 — Android

| # | Item | Status | Evidence / notes |
|---|---|---|---|
| 26 | Loopback proxy inside the shell: UI + `/api/*` forwarding, same-origin, no CORS | todo | Plan 5.2. Risk row: prove before any frontend change |
| 31 | NDK installed, `cargo tauri android init`, debug APK builds | todo | Plan 5.1. SDK platforms 35/36 + JDK 17 present, **NDK absent** |
| 32 | Mobile integration: safe areas, back button, external links, keyboard resize | todo | Plan 5.3. `env(safe-area-inset-*)` + `viewport-fit=cover` already in place |
| 33 | WebView video playback + save-to-device via the platform file API | todo | Plan 5.4 |
| 34 | Signed APK (keystore in secrets) on GitHub Releases | todo | Plan 5.5 |

## M6 — iOS

| # | Item | Status | Evidence / notes |
|---|---|---|---|
| 35 | `cargo tauri ios init` + macos CI job, simulator build | todo | Plan 6.1 |
| 36 | Signing/sideload (AltStore) instructions | todo | Plan 6.2 |
| 37 | Signed IPA actually installing | blocked | Plan 6.3. No macOS/Xcode here; row must stay `blocked` until a signed build installs somewhere |

## M7 — Releases

| # | Item | Status | Evidence / notes |
|---|---|---|---|
| 38 | One `release.yml`: AppImage, DEB, EXE/MSI, DMG, APK (+IPA when signing exists) | todo | Plan 7.1. No artifact on a red test job |
| 39 | Release notes template: what each artifact does and does not do | todo | Plan 7.2. Mobile needs a server; say it in every release |
| 40 | Checklist: tests green + manual pass (download one profile, browse, play, trash, restore, Shorts) | todo | Plan 7.3 |

## M8 — Post-MVP

| # | Item | Status | Evidence / notes |
|---|---|---|---|
| 41 | On-device Android downloading (embedded Python or native) | todo | Plan M8. Needs a written answer on foreground services + storage before code |
| 42 | On-device iOS downloading | todo | Treated as experimental; background transfer APIs are not a general queue |

## Follow-ups found while building the first pass

| # | Item | Status | Evidence / notes |
|---|---|---|---|
| 43 | Static/trimmed `ffmpeg` in the sidecar | todo | Bundled ffmpeg is dynamically linked and needs FFmpeg 8.x sonames (`libavcodec.so.62`, `libavformat.so.62`, …). Without them only video thumbnails/previews/transcoding die — the UI and images are fine. Fix in CI with a static build, not by copying this box's libs |
| 44 | Decide `instaloader`: bundle it or document the install | todo | Not in the bundle today: the UI serves, but nothing Instagram-side downloads. The model already offers to install it at first run (`run.py`) — the frozen binary needs the same answer |
| 45 | glibc floor: 2.38 (FC44 build) | todo | Ubuntu 22.04 will not start the bundle. Fix by building in CI on an older base image |

---

## Must not regress (green at every step)

| # | Behaviour | Why it is at risk | Check |
|---|---|---|---|
| A | Full web UI at `:8765` — every tab, button, field and label | A Tauri shell tempts frontend surgery; he reads a dropped control as "degraded" | `tests/baseline/dom.txt` landmarks + manual pass |
| B | `/shorts` page and the Shorts tab | Separate file, easy to forget when the shell points at `index.html` | Open both |
| C | Linux autostart: `systemctl --user status hardlywatching` | Shell/sidecar work touches launch paths | Restart unit, `curl :8765/api/health`, shell attaches |
| D | `./start.sh` / `python3 run.py` still work from a bare checkout | Sidecar work must not become the only path | Run from a temp clone |
| E | The launcher AppImage still opens the control panel | `make-appimage.sh` may get absorbed into Tauri bundling | `dist/HardlyWatching-x86_64.AppImage` with `HW_APPIMAGE_OPEN=1 BROWSER=/bin/echo` prints the URL |
| F | Phone PWA install (icons from files, `start_url=/?tab=shorts`) | Manifest/icon routes live in `server.py` | `Page.getInstallabilityErrors` empty; icon 200s |
| G | Tailnet access from the phone | Bind drop-in plus any new auth gate | `ss -tlnp` shows the expected bind; phone loads the UI |
| H | Nothing personal in anything published | This repo is private; publishing is a later, named decision | Scan tree + full history before any publish |
