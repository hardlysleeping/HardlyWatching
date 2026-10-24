# UI baseline

A snapshot of what the web UI actually renders, so a later change cannot quietly
drop a tab, a toolbar button, a settings field or a gallery chip.

**Every file here comes from a fake archive.** `capture.py` builds a throwaway
demo instance with three invented personas (`demo.coral.reef`,
`demo.pixel.garden`, `demo.slow.mornings`), generated images and three synthetic
portrait clips. No real handle, path or session is involved, and the capture
refuses to touch `127.0.0.1:8765` at all. The rule from
`docs/agent-rules.md` applies: if an artefact needs the real archive to look
right, it does not go in this directory.

## Re-capture

```bash
tests/baseline/capture.sh                # rebuild the demo tree, capture, verify
tests/baseline/capture.sh --reuse        # keep the existing demo tree
tests/baseline/capture.sh --out /tmp/ui  # write the artefacts somewhere else
```

Takes about 45 s (the downloader stub sleeps 1.6 s per generated post). It needs
Chromium at `~/.hermes/tools/chromium-1208/chrome-linux64/chrome` (override
with `HWA_CHROME`) and `ffmpeg` for the demo clips; the Python is the project
venv, which has Pillow.

Environment knobs: `HWA_BASELINE_ROOT` (demo tree, default
`~/.hermes/cache/scratch/hwa-baseline`), `HWA_BASELINE_PORT` (default 8899,
never 8765), `HWA_CHROME`.

## What is asserted

Presence and count of every landmark and control — **not pixels**.

`dom.txt` is one row per `tab @viewport | selector-or-role | visible label | count`,
sorted. The capture greps the live DOM (what Chrome laid out, after JS ran), not
`index.html`; a control that exists in the markup but is hidden or never
rendered does not get a row, and a row with a count of 4 means four visible
instances (e.g. three finished cards plus the gallery preview each draw a
"Gallery" button).

To check a change, capture beside the baseline and diff the text:

```bash
tests/baseline/capture.sh --reuse --out /tmp/ui
diff tests/baseline/dom.txt /tmp/ui/dom.txt
```

- a line with `<` only = a control that disappeared (a regression)
- a line with `>` only = a control that appeared (say why in the PR)
- a changed count on an existing selector = instances appeared or vanished
- an empty diff = nothing was dropped

The four PNGs are for a human to eyeball (layout, spacing, labels that clip).
They are **not** byte-stable: the free-space chip, the animated Shorts frame and
the relative timestamps move between runs, so a pixel diff will always show
noise. Dimensions are checked at capture time (`1440x900`, `390x844`) by reading
the PNG header back from the file, not from the browser's report.

## Scope

| tab (column 1) | viewport | screenshot |
|---|---|---|
| `Download` | 1440x900, 390x844 | `download-1440x900.png` |
| `Download > TikTok` | 1440x900, 390x844 | — |
| `Gallery` | 1440x900, 390x844 | `gallery-1440x900.png` |
| `Gallery > Bookmarks` | 1440x900, 390x844 | — |
| `Shorts` | 1440x900, 390x844 | `shorts-1440x900.png`, `shorts-390x844.png` |

`Download` is the Queue tab (its button reads "Download"). `Download > TikTok`
is the same tab with the TikTok sub-tab open, because that pane swaps the notes,
the settings platform filter and the session list. `Gallery > Bookmarks` is the
gallery with the bookmark chip open, which is the only state that renders the
category chips. `frame:`-prefixed rows are controls inside the Shorts iframe
(`/shorts?embed=1`), read through `contentDocument` because that is where the
like/bookmark/trash/download controls live.

The Download scopes are harvested with the collapsed sections **open**
(`Download settings`, `Excluded`) and the Sessions panel open, so the ~40
setting fields are counted. Screenshots are always taken from a fresh load in
the default state, so the images show what a user sees on arrival.

## Determinism

Two full runs on the same machine produce a byte-identical `dom.txt`. Three
things are rewritten on the way out, all of them environmental rather than
structural:

| rewrite | why |
|---|---|
| the demo tree path → `<demo-root>` | a real path must not reach git |
| `320.3 GB`, `66.2%` → `<size>`, `<pct>` | free space moves between runs |
| `shown 3×` → `shown <n>×` | the Shorts feed counts what it has shown |

Every scope also gets a **new browser tab**. The Shorts page keeps its sitting
("which clips have I shown") in `sessionStorage`, which survives reloads in the
same tab: reusing a tab made the feed shrink load by load (3 ÷ 3, 2 ÷ 2, 1 ÷ 1)
and the count label came out different in every run. A fresh tab is the only
honest way to capture a page that deliberately remembers where you were.

## Isolation, and how it is proven

`capture.py` builds `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `INSTA_STATE`,
`INSTA_CACHE_DB`, `INSTA_OUTPUT`, `INSTA_THUMBS`, `INSTA_AVATARS` inside the demo
root, copies the skill's `demo-instaloader-stub.py` to `<root>/bin/instaloader`
**before the server starts** (`engine.py` resolves the binary at import) and
puts that directory first on `PATH`. It refuses any port that is 8765 or below
8899, and re-checks the page URL immediately before every shutter.

Each run prints the proof, by content rather than by timestamp:

```
-- isolation proof (read-only on the live state) --
   real jobs before: 629  after: 629  (same set: True)
   demo personas in the real state: 0
   real handles in the demo tree: 0
-- leak check: committed text artefacts vs real identifiers
   dom.txt                      CLEAN
```

The live `state.json` is opened read-only for that comparison and nothing else.
The leak check refuses to shoot if any real username, hostname or home path
appears in the page text just before a capture, and fails the run if one reaches
`dom.txt`.

## Limits — what a DOM list cannot capture

- **Canvas and video.** The Shorts player, any `<video>` frame and CSS gradients
  have no DOM text. The stills show they exist; they cannot assert what they
  draw. The three demo clips are a `testsrc` pattern, not real media.
- **CSS-only affordances.** Hover states, `:focus-visible` outlines, transitions
  and anything that only appears on `:hover` never show up in either artefact.
  A control that becomes invisible-but-focusable via CSS alone is only caught if
  it has zero size (the harvest drops zero-area elements).
- **Hidden-by-default UI.** The image viewer, the bookmark picker, the context
  menu (`⋯`), the "Change folder" dialog and toasts are opened by a click and
  closed again; they are not in this baseline. A change inside the viewer or the
  picker would pass the diff.
- **Behaviour.** Counts and labels only. Nothing here proves a button *works* —
  that is the 46-case API suite's job, and pressing the buttons is a manual
  pass.
- **Pixels.** Fonts, antialiasing, disk figure and video frames vary; only
  dimensions are asserted.
- **The demo data itself.** Three profiles, one waiting item, three clips. A
  layout that only breaks at 40 profiles, long handles or a profile with one
  photo will not be caught.
- **Phone layout overflows.** At 390x844 the Shorts pane's 9:16 embed runs past
  the fold (the page scrolls). The screenshot shows it; the DOM rows do not
  measure it, and nothing here fails if that gets worse.

## Files

| file | what |
|---|---|
| `capture.sh` / `capture.py` | the capture; one command, refuses 8765 |
| `demo_env.py` | builds/starts/stops the throwaway instance, seeds bookmarks |
| `cdp.py` | minimal stdlib Chrome DevTools Protocol client (WebSocket + frames) |
| `dom.txt` | the baseline: 542 rows, 112 landmarks, ten tab/viewport scopes |
| `*.png` | four screenshots, each under 300 KB |
