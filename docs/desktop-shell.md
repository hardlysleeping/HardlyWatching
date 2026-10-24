# Desktop shell (Tauri 2)

The desktop app is **a window onto a URL**, not a second copy of the UI. `index.html`,
`shorts.html`, the gallery, the queue — all of it ships inside the Python server. `src-tauri/`
resolves a server URL and points a WebView at it, so:

- there is exactly one copy of the frontend, and
- the shell can never drift from the server's UI: whatever that server serves is what the window shows.

The same binary shape is what the mobile app will use later (with a loopback proxy in front, see
the plan, M5) — the shell itself has no UI code.

## What is in `src-tauri/`

| File | Purpose |
|---|---|
| `Cargo.toml` | `tauri` 2 + `tauri-build` 2, no other runtime deps (`serde_json` only to read the config file) |
| `build.rs` | `tauri_build::build()` |
| `tauri.conf.json` | productName `HardlyWatching`, identifier `ing.hardlysleep.hardlywatching`, version `0.1.0`, bundle active, targets `all`, icons under `icons/` |
| `capabilities/default.json` | one capability, **no permissions** — the shell makes no IPC calls; the page talks HTTP to the server |
| `src/main.rs` | URL resolution, reachability probe, window creation, retry-until-up |
| `icons/` | generated from the repo's `icon-512.png` (32, 128, 256, 512 PNG, `.ico`, `.icns`); no downloads |

No `package.json`, no bundler, no frontend build step — `tauri.conf.json` has no `frontendDist`
and no `beforeDevCommand`, because there is nothing to build or copy.

## Server URL resolution order

1. **`HW_SERVER_URL`** environment variable (highest priority). A bare `host:port` is accepted and
   gets `http://` prepended.
2. **`app-config.json`** in the Tauri app config dir, key `serverUrl`:

   ```json
   { "serverUrl": "http://192.168.1.20:8765" }
   ```

   Linux path: `~/.config/ing.hardlysleep.hardlywatching/app-config.json`
   (or `$XDG_CONFIG_HOME/ing.hardlysleep.hardlywatching/app-config.json`).
   A malformed file, a missing key or an empty value is ignored rather than fatal.
3. **`http://127.0.0.1:8765`** — the default, the port the systemd user unit and the existing web UI
   already use.

The shell prints the URL **and** the source it came from to stdout on every start:

```
hardlywatching: server url = http://127.0.0.1:8899
hardlywatching: url source = env HW_SERVER_URL
hardlywatching: http://127.0.0.1:8899 is listening, loading the web UI
hardlywatching: window 'main' 1280x860 (min 380x520) loading http://127.0.0.1:8899
```

That is the headless test hook: the resolution is verifiable without a display.

### When the server is not up

The shell does not show a blank window. It:

- probes the host/port (TCP connect, 900 ms timeout),
- sets the window title to `HardlyWatching — server unreachable at <url> (<reason>)`,
- loads the URL anyway (WebKit renders its own error page), and
- re-probes every 3 seconds; as soon as the server answers it navigates the window to the real UI,
  restores the title and prints `hardlywatching: server came up, loaded <url>`.

So launching the app before the backend is running is survivable, not a dead window.

### Headless behaviour

With neither `DISPLAY` nor `WAYLAND_DISPLAY` set, the shell prints the resolved URL plus
`no DISPLAY/WAYLAND_DISPLAY -- not opening a window` and exits **0** instead of letting GTK abort.
That is deliberate: CI and ssh sessions get the resolution line, not a crash.

## Running it in dev

The window needs a server to point at. Any HardlyWatching server works; for development use a
**throwaway sandbox** so nothing touches real state (`~/.config/hardlywatching`, `~/instagram`):

```bash
# terminal 1 — sandbox server on 8899 (never 8765)
export HOME=$(mktemp -d)
export INSTA_STATE="$HOME/state.json"
export INSTA_CACHE_DB="$HOME/gallery-cache.db"
export INSTA_OUTPUT="$HOME/instagram"
export INSTA_THUMBS="$HOME/thumbs"
export INSTA_AVATARS="$HOME/avatars"
export INSTA_PORT=8899 INSTA_HOST=127.0.0.1
cd /path/to/HardlyWatchingApp
.venv/bin/python3 server.py

# terminal 2 — the shell
cd /path/to/HardlyWatchingApp
export HW_SERVER_URL=http://127.0.0.1:8899
cargo tauri dev            # or: cargo run --manifest-path src-tauri/Cargo.toml
```

To use the real, already-running server (8765) instead, just drop `HW_SERVER_URL` — that is the
default:

```bash
cargo run --manifest-path src-tauri/Cargo.toml
```

The shell only issues `GET` navigation to the page it loads; that is the same read-only surface a
browser tab has.

## Pointing it at another server

```bash
HW_SERVER_URL=http://127.0.0.1:8899 cargo run --manifest-path src-tauri/Cargo.toml
HW_SERVER_URL=192.168.1.20:8765   cargo run --manifest-path src-tauri/Cargo.toml   # scheme optional
```

…or persistently, without env vars:

```bash
mkdir -p ~/.config/ing.hardlysleep.hardlywatching
printf '{"serverUrl": "http://127.0.0.1:8899"}\n' \
  > ~/.config/ing.hardlysleep.hardlywatching/app-config.json
```

Remote use needs the server to be reachable from this machine (LAN, tailnet, HTTPS edge). At the
time of writing the server has no authentication at all, so a non-loopback server URL means
"anyone who can reach that port controls the archive" — see the plan, M1 and M4.

## Building and bundling

```bash
cargo build  --manifest-path src-tauri/Cargo.toml          # debug binary
cargo build --release --manifest-path src-tauri/Cargo.toml  # release binary
cargo tauri build --bundles deb                             # needs the Tauri CLI
cargo tauri build --bundles appimage
```

Installing the CLI (either is fine; `cargo install` is slower but keeps the toolchain to one source):

```bash
cargo install tauri-cli --version '^2' --locked   # then use: cargo tauri ...
npm install -g @tauri-apps/cli                    # then use: npx tauri ... / tauri ...
```

### Tauri system dependencies (Linux)

`cargo build` needs the WebKitGTK/GTK development headers, which are **not** installed on this
machine (Bazzite, an immutable Fedora desktop, has no `-devel` packages and must not have them
layered with `rpm-ostree`). The exact package names for a Fedora-family build image:

```
cargo rust gcc gcc-c++ make pkgconf-pkg-config
webkit2gtk4.1-devel javascriptcoregtk4.1-devel gtk3-devel libsoup3-devel
openssl-devel libappindicator-gtk3-devel librsvg2-devel patchelf file
```

Ubuntu/Debian equivalents: `libwebkit2gtk-4.1-dev libgtk-3-dev libsoup-3.0-dev
libjavascriptcoregtk-4.1-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev patchelf
build-essential pkg-config`.

On this machine the honest answer is: **build inside a container** (podman is present) or in CI —
see "State of desktop bundling on this machine" below for what actually ran.

## State of desktop bundling on this machine

### What actually ran (2026-10-23, this workstation)

| Step | Result |
|---|---|
| `cargo build` on the host | **fails**: `libdbus-sys` panics, no `dbus-1.pc` — the -devel packages do not exist on Bazzite and must not be layered |
| `src-tauri/build-in-container.sh build` | **succeeds**, `Finished dev profile in 1m 48s`, binary at `src-tauri/target/debug/hardlywatching` (199 MB debug build) |
| Headless run, no `DISPLAY`/`WAYLAND_DISPLAY` | prints `server url`, `url source`, `is listening`, then `not opening a window`; exit 0 |
| Windowed run against a sandbox on 8899 | window `main` 1280x860 created; screenshot capture is refused by GNOME (`Screenshot is not allowed`), so rendering was proven by traffic instead |
| Did the page actually load and run? | **yes** — a logging proxy in front of the server recorded `GET / -> 200 369713B` (index.html), 13× `GET /api/state`, `GET /api/gallery`, `GET /api/autostart` and `POST /api/excluded` in 12 s. That is the page's own polling loop running inside the WebView |

The proxy trick is the cheap way to prove a window rendered without looking at a screenshot:

```bash
# 8898 -> 8899, logs every request line to /tmp/hwa-proxy.log
python3 /tmp/hwa-logproxy.py &
HW_SERVER_URL=http://127.0.0.1:8898 ./target/debug/hardlywatching
sort /tmp/hwa-proxy.log | uniq -c | sort -rn
```

A window that shows an error page makes one request or none; a window that rendered the UI keeps
polling `/api/state` about every 1.2 s.

### Not done yet

`cargo tauri build --bundles deb|appimage` has not run in *that* session — the container build only
produced a debug binary. Both bundles, and the Python sidecar wired in (spawn-or-attach), landed in
M2-WIRE; see "The app ships its own server" below.

## The app ships its own server (M2-WIRE)

A packaged HardlyWatching needs **no system Python, no venv and no separate server**: the frozen
backend from `packaging/sidecar/` is installed inside the package, and the shell starts it only when
nothing else is already running.

### Installed layout

```
/usr/bin/hardlywatching                                   the shell                          4.6 MB
   <server root>/hardlywatching-server                    frozen entry point                 4.7 MB
   <server root>/_internal/                               CPython 3.14, Pillow, pillow-heif   85 MB
   <server root>/bin/                                     yt-dlp, ffmpeg, ffprobe             39 MB
```

`<server root>` is `usr/lib/HardlyWatching/sidecar` in both bundles — the .deb installs it there,
and `pack-appimage.sh` puts the same directory inside the AppDir. `tauri.conf.json` states it once:

```json
"linux": { "deb": { "files": { "/usr/lib/HardlyWatching/sidecar": "../dist/sidecar" } } }
```

(The map reads destination-first, source-second; Tauri's own docs are easy to misread here. The
AppImage does not use this key — see "Building the bundles".) Nothing is configured at runtime:
`sidecar_bases()` in `main.rs` walks these candidates, first hit wins:

| candidate | for |
|---|---|
| `$HWA_SIDECAR` | development and tests; **authoritative** when set, so "use this one" and "use none" both work |
| `<exe dir>/sidecar/` | a tree staged next to the binary by hand (a plain `cargo build` no longer stages one) |
| `<exe dir>/../lib/HardlyWatching/sidecar/` | the installed .deb (`/usr/bin/hardlywatching` -> `/usr/lib/...`) |
| `<exe dir>/../share/HardlyWatching/sidecar/` | the AppImage / a prefix install |
| `$APPDIR/usr/share/HardlyWatching/sidecar/`, `$APPDIR/usr/lib/HardlyWatching/sidecar/` | the AppImage |
| `/usr/lib/HardlyWatching/sidecar/`, `/usr/share/HardlyWatching/sidecar/`, `/usr/lib/hardlywatching/`, `/usr/local/lib/HardlyWatching/sidecar/` | fixed installs |

When none of them exists the shell prints every path it tried, so a broken package says where it
looked instead of shrugging.

### Why onedir, not a onefile externalBin

Both shapes were built from the same spec and measured on this machine (2026-10-23):

| shape | files | on disk | cold start -> `/api/health` 200 | ships yt-dlp/ffmpeg |
|---|---|---|---|---|
| **onedir, `bundle.resources` (shipped)** | 132 files, 14 dirs | 128 MB (85 `_internal`, 39 `bin/`, 4.7 exe) | **0.08 s / 0.08 s / 0.09 s** (`--attach` with no instance: 0.09 s) | yes, `sidecar/bin/` |
| onefile, `bundle.externalBin` (rejected) | 1 | 32 MB | 0.31 s / 0.32 s / 0.31 s, **plus ~85 MB unpacked into `$TMPDIR` on every launch** | no |

The onefile variant is still buildable for comparison (`HWA_ONEFILE=1`, see
`packaging/sidecar/hardlywatching.spec`), but it lost on four counts:

1. **It cannot carry the external tools.** `entry.py` puts `<bundle>/bin` first on `PATH` so
   `engine.py` finds the bundled `yt-dlp`/`ffmpeg`/`ffprobe` by bare name. A onefile payload has no
   `<bundle>/bin` to put them in, so they would have to ship as separate resources *and* the bundle
   would need a new search path. Onedir already does this correctly, verified with `--self-check`.
2. **It pays on every launch, not once.** 0.31 s versus 0.08 s here, because each start unpacks the
   whole runtime into `$TMPDIR`; that also needs a writable *and executable* temp dir, which a
   hardened box with `noexec /tmp` does not have.
3. 128 MB installed versus 32 MB is the honest cost of (1). Compression narrows it: the .deb is
   76,281,736 bytes (72.75 MiB, `Installed-Size: 136430` KB) and the AppImage is 73,464,312 bytes
   (70.06 MiB).
4. Both layouts install into `usr/bin` plus one `HardlyWatching` directory, so the .deb's file list
   stays one clean subtree that `apt remove` deletes whole.

### Spawn or attach

`main()` decides **before** the GTK event loop exists (that is what keeps `--headless` honest), in
this order:

| # | condition | behaviour |
|---|---|---|
| 1 | `HW_SERVER_URL` set | attach; never spawns, never stops anything |
| 2 | `serverUrl` in `app-config.json` | same |
| 3 | `hardlywatching-server --attach` returns a URL | attach to the instance that owns the state-file lock, and leave it running |
| 4 | `/api/health` answers on the default URL (`http://$INSTA_HOST:$INSTA_PORT`, 8765 by default) | attach, spawn nothing |
| 5 | none of the above | spawn the bundled server `--no-browser --port <free>`, wait for `/api/health`, then load it |

**Autostart parity.** `hardlywatching.service` (or `run.py`, or a terminal `server.py`) owns the
lock, so steps 3 and 4 find it and the desktop app attaches to the running service instead of
starting a second one — two servers on one state file would mean two queue workers on one archive.
The app never stops a server it did not start: only the child from step 5 is terminated, and only
after it answered `/api/health`. A test that sets `HW_SERVER_URL` is therefore guaranteed to leave
whatever is on that port untouched.

**Lifecycle of the child we do own** (`OwnedChild`):

- `SIGTERM` first, so `server.py` removes its lock and closes the socket cleanly; `SIGKILL` after a
  6 s grace period.
- On window close (`RunEvent::Exit`), after `run()` returns, and from `Drop` as a backstop.
- `PR_SET_PDEATHSIG=SIGTERM` (Linux) on the child, so even `SIGKILL`ing the shell cannot leave a
  server behind holding the port or the lock.
- A child that never answered `/api/health` is killed and reaped before the error page is shown.

**Logging.** The child's stdout/stderr are piped into the app's log line by line, prefixed
`hardlywatching[server]`. That needed one fix at the source: a frozen interpreter writing to a pipe
is block-buffered, so `entry.py` reconfigures its own stdout/stderr to line buffering (`PYTHONUNBUFFERED`
does not survive the PyInstaller bootloader). Without it the shell saw the server's banner only when
the server exited.

### When it goes wrong

- No bundled server and nothing listening: a window titled
  `HardlyWatching — could not start the server` showing a readable page (written to
  `$XDG_CACHE_HOME/ing.hardlysleep.hardlywatching/startup-error.html`) that lists the paths tried and
  the server's last output. Headless, the same message goes to stderr and the process exits **1**.
- An attached server that is not listening yet: title says `server unreachable at <url>`, and the
  existing 3 s retry loads the UI as soon as it answers.
- Headless with a server available (or one it would have started): prints the resolved URL and
  `no DISPLAY/WAYLAND_DISPLAY -- not opening a window`, exit **0** — and never spawns, because a
  headless start must not leave a server running.

### Building the bundles

```bash
src-tauri/build-in-container.sh deb        # -> src-tauri/target/release/bundle/deb/*.deb
src-tauri/build-in-container.sh appimage   # -> src-tauri/target/release/bundle/appimage/*.AppImage
```

Both targets build `dist/sidecar` on the host first if it is missing (that is
`packaging/sidecar/build.sh`; `HWA_SKIP_SIDECAR=1` disables the convenience). stdout of every build
is kept in `.build-logs/` (git-ignored) for audit.

**Why the AppImage is not built by `cargo tauri build --bundles appimage`.** Tauri's AppImage step
runs `linuxdeploy`, which deploys the dependencies of every ELF it finds in the AppDir. The frozen
server is a tree of ELFs whose shared libraries PyInstaller renames with a hash
(`libheif-c26addd2.so.1.23.2`, `libavif-8a7f9d56.so.16.4.2`, …); those only resolve through the
bootloader's own `LD_LIBRARY_PATH` at run time, so the build stops at
`ERROR: Could not find dependency: libheif-c26addd2.so.1.23.2`. Everything tried, and what happened:

| attempt | result |
|---|---|
| put the tree in `usr/lib` | fails on the first hashed library (`libavif`) |
| move the tree to `usr/share` | fails the same way — linuxdeploy scans the whole AppDir |
| `LD_LIBRARY_PATH=<tree>/_internal` | resolves the hashed names, then linuxdeploy deploys a second 85 MB copy and still stops on `bin/ffmpeg`'s `libavdevice.so.62` |
| `patchelf --set-rpath $ORIGIN` on the frozen libs | modifies files PyInstaller generates; not shipped that way |
| `--exclude-library 'lib*-????????.so*'` | clears one hashed name per run, and Tauri offers no way to pass extra linuxdeploy arguments |
| `bundle.resources` + `--config` override | `--config` merges instead of replacing, so the tree is still copied |

So `src-tauri/pack-appimage.sh` packages the AppDir with `appimagetool` directly: `cargo build
--release`, the tree, a desktop file, an icon, `AppRun` -> `usr/bin/hardlywatching`. Nothing is
patched or rewritten afterwards, and the payload is byte-identical to the .deb's. What is given up:
linuxdeploy's library deployment, i.e. GTK 3 and WebKitGTK 4.1 come from the host — which is what
Tauri documents for AppImages anyway (WebKitGTK is not relocatable, and `linuxdeploy-plugin-gtk` only
copies gdk-pixbuf loaders and a theme). Both bundles run on the same machines for that reason.

`src-tauri/build-in-container.sh appimage` is therefore not a `cargo tauri build` call; `deb` and
`build` are unchanged. A plain `cargo tauri build` (all targets) still fails on the AppImage half for
the reason above — use the script.



### What was actually run for this card (2026-10-23, this machine)

| check | result |
|---|---|
| `build-in-container.sh deb` | `Finished 1 bundle at: .../bundle/deb/HardlyWatching_0.1.0_amd64.deb (72.75 MiB)`, exit 0 |
| `build-in-container.sh appimage` | `...HardlyWatching_0.1.0_amd64.AppImage (71M, 73464312 bytes)`, exit 0 |
| `.deb` payload | 165 members, `usr/bin/hardlywatching` + `usr/lib/HardlyWatching/sidecar/{hardlywatching-server,bin/yt-dlp,bin/ffmpeg,bin/ffprobe}` all `-rwxr-xr-x` |
| `.deb` installed and run (temp HOME, temp `INSTA_*`, port 8899, `HW_SERVER_URL` unset) | spawned `usr/lib/HardlyWatching/sidecar/hardlywatching-server --no-browser --port 8899` as a child, `GET /api/health` -> `200`, window created |
| AppImage run (same isolation) | found the server at `$APPDIR/usr/lib/HardlyWatching/sidecar`, spawned it, `200`, window created |
| attach (`HW_SERVER_URL=http://127.0.0.1:8899`, sandbox server already there) | log: `url source = env HW_SERVER_URL`, `window 'main' ... loading`, 0 sidecar processes started, sandbox server still answering after the app exited |
| window close (wmctrl) on the spawned server | `stopping the server we started (... SIGTERM)`, `server pid ... exited (exit status: 0)`, no `hardlywatching-server` left, port 8899 free |
| `SIGTERM` to the app (deb, self-spawned server) | child gone via `PR_SET_PDEATHSIG`, port 8899 free, no lock left |
| headless, nothing running | `server url = http://127.0.0.1:8899/`, `no DISPLAY/WAYLAND_DISPLAY -- not opening a window`, exit 0, no server started |
| `DISPLAY` set but unusable (no X authority) | `FATAL could not open a window: Failed to initialize gtk backend!`, `the server is running at ... -- open it in a browser, or check DISPLAY/WAYLAND_DISPLAY and XAUTHORITY`, exit 2, spawned server stopped |
| `scripts/ci-local.sh` | `PASS: Ran 46 tests in 5.526s (compile check + unittest discover, Python 3.14.7)` |

The windowed runs used the live `DISPLAY=:0` (XWayland) of this desktop, since `xvfb-run`, `Xvfb` and
`weston` are not installed here; each run used a throwaway `HOME`, throwaway `INSTA_*` paths, port
8899, a stub `instaloader` first on `PATH`, and never touched port 8765 or `hardlywatching.service`.
