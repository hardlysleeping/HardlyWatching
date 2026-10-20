# Changelog

Notable changes to HardlyWatching. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning is [SemVer](https://semver.org/).

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
