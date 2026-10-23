# Tests

A sandbox and a smoke suite for the HardlyWatching backend, so a change can be
verified without going anywhere near the real archive or the running service.

Two files matter:

| File | What it is |
|---|---|
| `sandbox.py` | Builds a throwaway app (temp `HOME`, temp state/cache/archive, stub downloaders, free port) and starts `server.py` against it |
| `test_api_smoke.py` | 29 `unittest` cases over the routes the web UI calls, plus one that tests the harness's own port handling |

No pytest, no plugins, nothing to install: stdlib `unittest` only, because the
project's venv (`/home/bazzite/apps/HardlyWatchingApp/.venv`) has Pillow and
pillow-heif and must not be added to.

## Run it

From the repo root:

```sh
/home/bazzite/apps/HardlyWatchingApp/.venv/bin/python3 -m unittest discover -s tests -v
```

One class, or one test:

```sh
/home/bazzite/apps/HardlyWatchingApp/.venv/bin/python3 -m unittest tests.test_api_smoke.GalleryRouteTests -v
/home/bazzite/apps/HardlyWatchingApp/.venv/bin/python3 -m unittest tests.test_api_smoke.WriteRouteTests.test_pause_then_resume -v
```

A one-shot proof that the sandbox itself works (this is the M0.1 check):

```sh
/home/bazzite/apps/HardlyWatchingApp/.venv/bin/python3 -m tests.sandbox --check
# OK port=8899 state=/tmp/hwa-sandbox-xxxxxxxx/state.json
```

It is repeatable: no state survives a run, so a second identical run starts from
the same empty sandbox.

## The rule: these tests never touch real state

Every test runs against its own temp tree. **Nothing in `tests/` may read or
write `~/.config/hardlywatching`, `~/instagram`, the live checkout at
`~/apps/hardlywatching`, or port 8765**, and nothing here may be pointed at them
"just to compare". If a future test needs a real-archive fixture, copy a few
files into a temp dir instead.

This is enforced, not just intended:

- `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, `INSTA_STATE`,
  `INSTA_CACHE_DB`, `INSTA_OUTPUT`, `INSTA_THUMBS` and `INSTA_AVATARS` are all
  rewritten to point inside the temp dir before the server starts. Any inherited
  `INSTA_*` variable is dropped rather than trusted.
- `instaloader` and `yt-dlp` on `PATH` are stubs written by `sandbox.py`: they
  exit 0, write a fake profile folder, and have no networking code at all. The
  server resolves `INSTALOADER` at import, so the stub is what it uses.
- The port is probed free from 8899 upward and **8765 is skipped outright**; if
  the app moves to another free port, the sandbox follows the URL the server
  prints.
- `stop()` kills the whole process group (the server spawns downloader
  children) and deletes the tree, so a failing test cannot leave a listener or a
  temp dir behind.

`test_state_is_the_sandbox_not_the_real_app` and
`test_writes_persist_inside_the_sandbox` assert these properties from the
outside: the server's own `/api/state` must report the temp archive, and its
state file must be the temp one.

## What the sandbox provides

```python
from tests import sandbox

with sandbox.Sandbox() as sb:
    sb.get("/api/state").json()          # Response: .status/.json()/.text()/.content_type
    sb.post("/api/pause", {"platform": "instagram"})
    print(sb.port, sb.root, sb.output)   # chosen port, temp dir, temp archive
```

- `.get(path)` / `.post(path, body)` return a `Response` for *any* status —
  4xx/5xx does not raise, so a test asserts on `.status`.
- One profile (`sandbox_seed`) is written into `INSTA_OUTPUT` at startup by
  running the instaloader stub directly, so the read-only browsing routes have
  something real to describe without any job having run.
- `Sandbox(seed_profile=None)` gives an empty archive; `port=` pins the port
  (8765 is refused); `keep=True` or `HWA_SANDBOX_KEEP=1` leaves the temp tree and
  `server.log` on disk for debugging.
- Startup waits up to 20 s for `GET /api/health` and, on failure, raises with the
  server's log tail rather than a bare timeout.

Sandboxes are per test *class* (`SandboxCase.setUpClass`), not per test, so the
suite runs in seconds. The classes that only read are therefore order
independent; the one class that writes says so in its name.

## What is covered

Read-only | Notes
---|---
`GET /api/health`, `/api/state` | the two the page polls constantly
`GET /api/gallery`, `/api/timeline`, `/api/timeline-month?month=all` | seeded profile must appear
`GET /api/bookmarks`, `/api/links`, `/api/partials` | shape only
`GET /api/peek?profile=…` | the handler 400s without `profile`; both paths are asserted
`GET /api/static`, `/api/autostart`, `/api/tiktok/browsers` | status/report endpoints
`GET /`, `/shorts`, `/app.webmanifest`, `/icon-192.png`, `/favicon.ico` | 200 + content type + a real marker in the bytes
`POST /api/shorts/feed` | **POST, not GET** — see below

Safe writes (temp tree only) | Notes
---|---
`POST /api/pause` → `POST /api/resume` | must flip `paused` in `/api/state` and back
`POST /api/add` | returns JSON, and the queued job is visible in `/api/state`
`POST /api/add` twice | the second is rejected as a duplicate

Assertions are about **shape** — keys present, types right, sandbox data present
— not exact counts, ports or timestamps, which would break for reasons that are
not regressions. Where a route needs a query parameter or a JSON body to answer
200, the test passes what the UI passes; no assertion is weakened to "any
status".

## Things that surprised us, recorded so nobody re-learns them

- **`/api/shorts/feed` is a `POST`**, even though it only reads. It is a weighted
  pick that remembers what it showed (`db.mark_shown`), so it takes
  `{limit, liked, exclude, profile, platform}` as a JSON body and updates the
  shorts table. Any plan or list calling it a GET is wrong.
- **`/api/peek` returns 400 without `?profile=`**, and 200 with an empty `items`
  list for a handle that is not on disk. Both behaviours are asserted.
- **`/api/autostart` shells out to `systemctl --user is-enabled hardlywatching.service`**
  (`is-enabled` is read-only, so it is allowed; it never starts or stops
  anything). The *value* therefore reflects the host, which is why only its shape
  is asserted. The service unit itself is never touched.
- **The server writes no `state.json` until something changes**, so the sandbox's
  first write is what creates it. This is why the persistence test pauses first.
- **`server.py` picks its own port** via `find_free_port()`, so a busy 8899 does
  not fail the run — the sandbox reads the announced URL from the log and
  switches to it.
- `GET /api/autostart` and `/api/state` are the only routes here that touch
  anything outside the temp tree, and both are reads.

## Not verified here

- Anything requiring a real Instagram/TikTok session, a real download, ffmpeg
  thumbnailing or the static-video scan actually running: the stubs and the empty
  queue make those paths unexercised by design. (The add test does start a real
  worker against the stub, but it never leaves the temp tree.)
- A fresh-clone run: the suite assumes the venv python above and a POSIX host
  (`/bin/true` as `BROWSER`, process groups). Linux only for now.
- The UI itself. `tests/baseline/` (M0.3) is the DOM/screenshot guard for that.
