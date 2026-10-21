# Changelog

Notable changes to HardlyWatching. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning is [SemVer](https://semver.org/).

## [Unreleased]

### Changed

- **One paste box for both platforms.** The Instagram and TikTok tabs each had
  their own box, so a mixed list meant two pastes on two tabs. There is now a
  single box at the top of the tab, with the Instagram/TikTok buttons sitting
  under it, and a mixed paste of Instagram and TikTok links goes in at once, one
  per line.

  Each line is routed by what it points at: an `instagram.com` link is an
  Instagram profile, a `tiktok.com` link is a TikTok one. A bare name has no
  domain to read and the same handle can be two different people on the two
  sites, so it follows the tab you are on rather than being guessed at. The
  TikTok grab-count select moved into the same box (it applies to TikTok lines
  only), and the "Add to queue" button now reports both counts.

### Fixed

- **The app now opens on the tab it was asked for.** The `?tab=` handler was
  nested inside `showPlatform()` by mistake, which broke it twice: it never ran at
  load, so an installed phone app (whose start URL is `/?tab=shorts`) opened on
  Download instead of Shorts; and clicking the Instagram/TikTok sub-tab ran it
  late, throwing you onto Shorts mid-use. It is now a single top-level call at the
  end of the script. Shorts lands with the whole app around it — the tabs and the
  menu are there, so leaving it is a tap rather than a back button.

### Changed

- **A TikTok profile opens straight away.** TikTok offers no profile embed, so
  the in-app panel could only say "there is nothing to show here" and ask for a
  second click on the link inside it — a click and a scroll to reach a page whose
  address we already had. Clicking a TikTok profile (its name, or the TikTok
  button in the menu) now opens it on tiktok.com immediately. Instagram keeps the
  panel, because its embed does show something.

### Fixed

- **Clearing a tombstone now sticks.** It removed the placeholder but left the
  name in the folder's `.insta-index.json`, so the next gallery load compared the
  index against the folder, saw a file it remembered but could not find, and
  concluded it had been deleted outside the app — writing the placeholder
  straight back and re-recording the tombstone. "Post will re-download" was
  untrue in exactly the case the feature exists for. The index entry is dropped
  too now, which is what makes the clear hold.

### Added

- **"+5 all" on the Finished list** (TikTok tab), which raises every finished
  TikTok profile's grab-count by 5 and puts them back in the queue in one go.
  Profiles sit at the same low count by default, so raising them one card at a
  time is hundreds of clicks for a single intention. A profile already asking for
  its whole archive is left alone, and nothing mid-download is ever disturbed.
- **A 🖼 Still videos button in the Gallery**, which finds the stories that are
  one unchanging image held for their whole length and can write a still image
  beside each one. Scanning runs as a background job with live progress and a
  Stop button, since decoding a whole archive takes far longer than a request
  can wait, and it offers to write the images from what it already found rather
  than decoding everything twice.
- **Deleting still videos**, per row or in bulk, keeping or removing the still
  image. Files go to the desktop trash so a mistake is recoverable, and each
  leaves a 0-byte placeholder so instaloader does not fetch it again.
- **`tools/static_videos.py`**, which finds videos whose frames never change —
  the stories that are several seconds of one unchanging image. By default it
  only reports; `--convert` also writes a JPEG beside each clip, never replacing
  the video and never overwriting an existing file.
- **A Settings button in the header**, visible on every tab, which switches to the
  Download tab and opens the settings section for you.
- **"Show all platforms"** in the settings toolbar. Settings belonging to the other
  platform were hidden outright, with no way to see them without switching tabs.
  They are now revealed dimmed rather than hidden, and stay editable.
- **Shorts player conveniences.** A speaker button in the rail (and **m**) turns
  sound on and off, remembered for the sitting; clips still start muted, since a
  browser refuses to autoplay audible video. Tappable **skip buttons** flank the
  seek bar; tapping either three times steps the jump through 5, 10 and 15
  seconds and back, without also skipping. On a phone — where the seek bar is
  desktop-only and there is no keyboard — **double-tap the left or right half** to
  jump by the current step, with a brief play/skip mark flashing over the clip so
  a tap visibly does something. A clip that a browser refuses to start with sound
  falls back to muted and the button shows that, instead of claiming sound is on
  over a silent video.
- **`install-service.sh` now prefers the project's venv** and checks the chosen
  interpreter can import Pillow, warning when pillow-heif is missing (no HEIC
  previews). It also writes the unit's `PATH`, which it previously omitted, so
  re-running it no longer produces a unit that cannot find `instaloader` or
  `yt-dlp`.

### Fixed

- **TikTok no longer waits four minutes between profiles.** Its gap was set to
  240s — twice Instagram's — which turned a few hundred queued profiles into
  about a day of mostly waiting. yt-dlp only reads a public page per profile:
  there is no session and no request budget it shares, so the long pause bought
  nothing. The default is now 15 seconds.
- **The between-profile gap is no longer invisible.** The countdown read
  Instagram's field only, so on the TikTok tab a gap showed as "working" with
  nothing counting down — a queue that was simply pacing looked stuck. It now
  reads the platform you are actually looking at.
- **Start now cuts the gap short.** Pressing Start on a tab drops that
  platform's remaining pause and begins the next profile at once, instead of
  appearing to do nothing for up to four minutes.
- **Uploaded videos now reach the Shorts feed.** A video you uploaded has no
  instaloader sidecar, so its reel flag was unknown and the feed left it out —
  148 uploads were excluded this way. Where the sidecar is absent the video's
  shape is measured instead: taller than it is wide means short-form, so it
  joins the feed. Square and landscape do not. Dimensions are cached against the
  file's size and mtime, so the ~140 ms probe is paid once per file rather than
  on every scan.
- **The sibling `tiktok` folder is only adopted when it is actually an archive.**
  Any directory of that name next to the downloads folder used to be read as one
  and listed in the gallery. It must now carry a `.hardlywatching-tiktok` marker,
  written the first time video is downloaded there, or hold at least one child
  folder with a video or a `.yt-dlp-archive.txt`. An unrelated folder that happens
  to share the name is left alone, and the archive stays inside the downloads
  folder instead.
- **Carousels that mix photos and videos were split into separate posts.** Parts
  are numbered in one sequence across both types, so grouping them per file
  extension broke a twenty-part post into two — a carousel looked like a single
  item, with the remaining parts appearing as unrelated tiles. 1,939 posts in a
  real archive were affected, across 19,010 files. Parts are now grouped by their
  instaloader stamp regardless of extension.
- **Files with no instaloader stamp no longer merge on a shared prefix.** They
  join a post only when the unnumbered file exists and the numbered ones run
  2, 3, 4 with no gap; otherwise each keeps its own name, so thirty tiles no
  longer all read "IMG".
- **`install-service.sh` picked an interpreter that might not have the app's
  dependencies**, and its comment claimed "the app is stdlib-only" — untrue since
  it needs Pillow for thumbnails and pillow-heif for a phone's HEIC photos. It
  now prefers the project's own venv and checks that interpreter can actually
  import Pillow, warning rather than failing when pillow-heif is absent.
- **The installer dropped the service's `PATH`.** The unit sets `PATH` explicitly
  because systemd does not inherit a login shell's, so the app can otherwise miss
  `instaloader` and `yt-dlp`. The installer's template never wrote that line, so
  re-running the documented install step produced a unit that could not find
  either downloader. Verified: the generated unit now matches the live one.
- **The settings text filter undid the platform filter.** Both wrote
  `style.display` directly, so typing in the filter box un-hid the other
  platform's rows underneath the results — on the TikTok tab, searching "stories"
  listed Instagram-only options as though they applied. Both filters now resolve
  in one place, so the two axes cannot disagree.

### Changed

- **TikTok archives moved out of the Instagram folder.** They lived in
  `<downloads>/tiktok/<handle>/`, which made TikTok look like a sub-folder of
  Instagram — the downloads folder is named after instaloader, so nesting TikTok
  inside it read as "TikTok is part of Instagram". TikTok now sits **beside** the
  downloads folder, `<downloads>/../tiktok/<handle>/`, as a peer. Existing
  archives are read from either location, so a backup or a second drive in the
  old shape still works, and the move itself is a folder rename rather than a copy.
- **The TikTok location rule is defined in exactly one place.** It had been
  written out separately in the gallery's profile scan, and that copy kept
  looking in the old spot — TikTok profiles vanished from the Gallery while the
  queue, the shorts feed and the files on disk were all fine. One guard comes
  with it: if the downloads folder *is* a drive's mount point the sibling would
  land on a different disk, so TikTok stays inside it there. Checked at runtime,
  not hardcoded.
- **Instagram and TikTok download independently.** They were sharing one worker,
  one queue position and one pause flag, so a TikTok video sat behind hundreds
  of Instagram profiles and an Instagram rate-limit pause stopped TikTok too.
  They use different programs against different servers with separate rate
  limits, so there was nothing to serialise: each platform now has its own
  worker, queue, pause, cooldown and pacing timer, and one download per platform
  runs at a time. Measured: a two-second TikTok download and a two-second
  Instagram download overlap by the full two seconds.
- **Start, Pause and Resume act on the platform on screen.** Pausing Instagram
  to sit out a block no longer stops yt-dlp, and the TikTok tab no longer shows
  Instagram's cooling-down countdown. "Start TikTok" simply starts the TikTok
  queue rather than reordering the shared one.

### Added

- **Disk space in the header, with a stop before the drive fills.** Every archive
  folder's free space and percentage used is shown at the top of every tab, amber
  from 90% and red at 95%. At 95% nothing new starts and a run that crosses the
  limit is halted mid-way — enforced as a reserve checked before a job begins, so
  a download only starts when at least 5% (or 2 GB) of the volume is free. This
  is not a failure: the job goes back to the front of its queue, is not marked
  failed, and resumes by itself once space is freed. Each folder is judged against
  its own drive, and an unplugged drive counts as absent rather than full. The
  banner names the folder, the numbers, and what to do about it.
- **Shorts is a tab.** Download / Gallery / Shorts, swapped to like the others,
  in a phone-shaped column. `/shorts` still works on its own for the installed
  phone app.
- **Start the platform you are looking at.** The start button follows the
  Instagram/TikTok sub-tab: on TikTok it is **Start TikTok** and starts that
  queue, so it no longer looks like it only ever starts Instagram.
- **A seek bar in Shorts on desktop**, plus ← / → to jump 5 seconds. Each clip
  loops, so without it there was no way to go back.
- **"More / less often" applies to the profile**, not the single clip: one video
  landing well means you want more from that person, and every clip they posted
  follows. Both are clamped so a profile cannot dominate or vanish.
- **Start on login** is a toggle in settings rather than something you have to
  remember. It reads and sets the systemd unit, so the terminal and the app
  never disagree.

### Fixed

- **Start on the TikTok tab looked like it only ever downloaded Instagram.** It
  picked whatever was next in the shared queue; it now prioritises the platform
  on screen.
- **The "grab from a browser" list in Sessions was broken** — the server called
  a method that did not exist, so the panel errored instead of listing browsers.
- **Switching tabs used to un-hide panels you had closed**, including the
  Sessions panel, which made the Download tab look like it had rearranged
  itself.
- **The Shorts feed re-read the whole archive on every batch**, about a second
  per couple of swipes. The scan is now cached briefly, so refills are ~0.1 s
  and opening the feed still reflects the current disk.
- **A corrupt `state.json` can no longer empty the queue.** Saves are written to
  a unique temp file, fsynced, validated, then swapped in, with the previous
  state kept as a backup; loading recovers the first valid object from trailing
  garbage and falls back through the backups.
- **The excluded list never appeared until you pressed Restore all.** It is read
  once when the page loads and refreshed on a slow timer, so someone you remove
  mid-session shows up without a reload.
- **Restore reported into the wrong pane.** Its message went to the TikTok import
  box, which is hidden on the Instagram sub-tab — so pressing Restore could do
  its job and say nothing. It now reports on the Excluded card itself, and a
  restore that fails leaves its "Failed — retry" on the button instead of
  repainting over it.
- **A rate-limit pause looked identical to pausing it yourself.** The queue
  reported a bare "paused" with no countdown; it now says it is rate limited and
  counts down to when it resumes by itself.
- **The Download tab's header said two different things** — one line in the
  static HTML on a fresh load, another after any tab switch.
- **The count buttons on a Finished TikTok profile lied and half-worked.**
  `groupFinished` copied `platform` through but not `tiktok_count` /
  `tiktok_total`, so every finished card read "grab newest all" and both minus
  buttons were permanently disabled — the numbers were there, the card just
  never saw them.
- **Changing a count could block for over two minutes.** `/api/tiktok/more` ran
  a full profile listing (measured: **89 s** for a 1,492-video profile) inline
  while holding the engine lock, so the click appeared dead and both workers
  stalled with it. The total is no longer looked up in the request path; it
  fills in from the background filler and shows as unknown until then.
- **Raising a count gave no sign that anything happened.** The card now moves
  into the Waiting list as soon as the server confirms it was re-queued, and the
  number goes to `…` the instant you click, so a slow round-trip cannot look
  like a dead button. A re-queued profile also goes to the **front** of its own
  platform's queue rather than the back of 377 — appending put the extra videos
  days away and made the button pointless.
- **A bad cookie setting failed every TikTok download.** Firefox forks (Zen,
  LibreWolf) were written as `zen` and `librewolf`, which yt-dlp rejects
  outright — it accepts a fixed list of browser names and exits 2 on anything
  else. Firefox-family browsers are now stored as `firefox:<profile directory>`,
  the value is checked before it is saved, an unusable stored value is ignored
  at download time rather than failing the job, and a profile path is no longer
  lowercased (`Default` was being written as `default`, which matches nothing on
  Linux).

## [0.1.0] — 2026-10-20

First tagged release. Everything below is usable and tested against a real
library; the honest gaps are listed at the end rather than left out.

### Added

- **TikTok downloads** through [yt-dlp](https://github.com/yt-dlp/yt-dlp),
  alongside Instagram through [instaloader](https://instaloader.github.io/).
  Paste plain handles, `@handles`, or profile URLs; a pasted following list is
  reduced to usernames for you.
- **Per-profile video counts.** Start with five and raise it later — `−15 −5`,
  `+5 +15 +25`, or `all`. Each profile keeps a `.yt-dlp-archive.txt`, so raising
  the number fetches only what is missing, and a finished profile goes back in
  the queue to pick up where it stopped.
- **Profile totals** are looked up when a profile is queued, so the count reads
  `5 / 120` without you having to press anything first.
- **A shorts feed** at `/shorts` — a vertical, full-screen video feed for a
  phone or as an installed app. Like, trash, download, or change how often a
  clip reappears; filter to favourites. The order reshuffles per session.
- **Reels only.** The feed shows every TikTok clip and Instagram reels, not
  ordinary Instagram videos. The distinction comes from instaloader's own post
  record, so it applies to archives that already exist.
- **Exclusions on both platforms.** Removing a profile, or deleting one from the
  gallery, stops it coming back on the next paste. Nothing is deleted; the
  Excluded list restores it.
- **Private-account detection** for TikTok, shown in red beside the queue count.
  A private profile you follow can be downloaded; one you do not follow cannot
  be downloaded by any method, so it is marked fatal rather than retried.
- **Optional proxy** for TikTok, including a bare Cloudflare Worker hostname.
- **HEIC support.** iPhone photos appear in the gallery and open full-size;
  the download button still gives the original file. Needs `pillow-heif`, which
  `run.py` installs; without it everything except HEIC keeps working.
- **Separate folders per platform** (`<root>/<username>` and
  `<root>/tiktok/<handle>`), so a TikTok handle and an Instagram profile that
  share a name cannot collide.

### Changed

- The app is now **HardlyWatching**, and the page header says so, with the
  current platform as a badge rather than in the title.
- **Relicensed to GPL-3.0** (was MIT).
- TikTok queue rows no longer fight for space: the count has its own line, the
  steppers are quieter, and cards are a consistent height.
- The rate-limit countdown gained a **Clear the wait** button, worded to warn
  that retrying early is what lengthens a block.
- The installed service runs from the project's own `.venv` with an explicit
  `PATH`, instead of a directory belonging to another tool.

### Fixed

- Videos in the shorts feed never played — they were created unmuted, which
  every browser blocks, so each one sat paused as a still frame.
- The feed showed the same order on every load.
- TikTok clips were missed by the feed once downloads moved into `tiktok/`.
- Handles containing a hyphen were silently dropped when parsing a pasted list;
  unreadable rows are now reported instead.
- A truncated paste is detected and reported, rather than quietly queueing a
  fraction of your list. Scrolling the Following list to the bottom before
  copying is the fix, and the README says so.
- The removed queue appeared on the TikTok tab, where it does not belong.
- Dead endpoints and unused imports removed.

### Known gaps

- The `/shorts` app has only been tested in a desktop browser, not on a real
  phone.
- macOS and Windows install paths are written but have not been run.
