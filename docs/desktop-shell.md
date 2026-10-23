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

`cargo tauri build --bundles deb|appimage` has not run — the container build only produced a debug
binary. Bundling, and wiring the Python sidecar in as an `externalBin` (spawn-or-attach), is card
M2-WIRE on the kanban.


