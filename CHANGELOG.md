# Changelog

Notable changes to HardlyWatching. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning is [SemVer](https://semver.org/).

## [Unreleased]

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
