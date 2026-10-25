# Changelog

Notable changes to HardlyWatching. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning is [SemVer](https://semver.org/).

## [Unreleased]

## [0.3.6] — 2026-10-24

### Fixed

- **The Gallery tab did nothing from inside a profile.** It redrew the profile you
  were already looking at, because `renderGallery()` reopens whatever `gProfile`
  holds, so the only way back to the list was the profile view's own
  *← All profiles* button. Pressing Gallery while a profile is open now clears it
  and shows all profiles — the list is what the tab promises. Coming back to the
  Gallery from another tab still returns to the profile you were viewing.

## [0.3.5] — 2026-10-24

### Fixed

- **0.3.4 shipped the count bar and nothing else.** The zoom fix and the page-side
  retry for a missing profile picture were written after the tree had been copied
  into the app repo, so the bundles carried one of the three fixes. This release
  carries all of them; the count bar is unchanged from 0.3.4.
- **Wheel-zoom cropped the picture instead of enlarging the frame**, as described
  in 0.3.4 — the frame now opens to the window while zoomed and closes again at
  100%, with the pan offset cleared rather than leaving the picture off-centre.
- **A missing profile picture was remembered for the whole session**, as described
  in 0.3.4 — a miss is now retried after 90 seconds, so a picture that the
  background fetcher saves later appears without a reload.

## [0.3.4] — 2026-10-24

### Fixed

- **The TikTok count bar, properly this time.** 0.3.3 let the row wrap, which
  stopped `all` falling off the card but left a ragged two- or three-line block
  with a different height on every card. It is now two fixed rows: *grab newest*
  and the number on one line, then `−15 −5 +5 +15 +25 all` in six equal columns
  that divide whatever width the card has. Equal fractions cannot overflow, so
  `all` is on the card at every window width, and every card is the same height.
- **Wheel-zoom cropped the picture instead of enlarging the frame.** The viewer
  capped the picture at 76vh inside a 1000px-wide box, so past plain fit every
  step of zoom pushed more of the image under the stage's edge — zooming in
  showed *less*. Zooming now opens the frame to the window and lets the picture
  fill the stage, so the whole frame grows; returning to 100% closes it again and
  clears the pan offset rather than leaving the picture off-centre.
- **TikTok profile pictures were missing on any install that had not done the
  downloading.** The picture cache lives per install, outside the archive, and
  was only filled when that install downloaded a profile. A second install (the
  flatpak beside a system checkout, a new machine) showed faces for Instagram and
  none for TikTok. A picture request with nothing cached now queues the handle,
  and one background worker fetches them one at a time, 0.8s apart — a gallery
  load asks for hundreds of pictures at once and a burst at tiktok.com is how an
  address earns a block. The page also stops remembering "no picture" forever: a
  miss is retried after 90 seconds, so a picture that arrives later appears
  without a reload.

### Known gaps

- The count bar and the frame still reflow at very narrow card widths (below
  ~200px) because six columns of two-digit numbers cannot be narrower than the
  digits; the buttons shrink evenly instead of clipping, which is the trade.

## [0.3.3] — 2026-10-24

### Fixed

- **"all" was invisible on every finished TikTok card.** The count bar —
  *grab newest*, the number, `−15 −5 +5 +15 +25`, *all* — is ~300px of controls,
  and it was laid out as one line inside a horizontal scroller whose scrollbar was
  hidden. In the finished column the card is ~218px, so the last control sat ~85px
  past the card edge: `all` could not be seen or clicked, and nothing on screen
  said there was anything more to reach. The bar wraps now, with the five steppers
  kept together as one block, so `all` moves to its own line whole instead of being
  cut off. Wide queue cards are unchanged (one line); finished cards are two.

## [0.3.2] — 2026-10-24

### Added

- **Duplicate finder (Options → Duplicates).** A joint post on Instagram is
  served from every collaborator's profile, so the same carousel is downloaded
  into two folders and nothing in the app noticed — the gallery is per profile by
  design. The panel compares files by content (size first, then a checksum)
  *within one platform*, because the same picture on Instagram and TikTok is not
  a duplicate, and lists the identical groups with the space they waste. Copies
  in Favourites or Trash are shown but never offered. Scanning only reads;
  removing is one explicit press and moves the chosen copies to that profile's
  Trash, where the gallery's Restore brings them back. Nothing is unlinked.
- **A flatpak repository, so the app installs like an app.** The releases carried
  a `.flatpak` file, which is a download with an install command; there is now a
  signed repository at <https://hardlysleeping.github.io/HardlyWatching/> and a
  `.flatpakref` that opens in a software installer from one click. `flatpak
  update` works from then on.

### Fixed

- **The Flatpak listens on 8777, not 8765.** The bundled shell attaches to
  anything already answering on its default port, and a flatpak shares the host
  network, so on 8765 it would have driven a separately running install instead
  of its own copy. The flatpak now has its own port; queue, settings and gallery
  cache were already separate.
- **The two queue headings' rows are the same height.** "Waiting" and "Finished"
  started on the same line, but the finished head carries sort and Clear, so the
  search boxes under them sat 8px apart. A common minimum height levels the rows
  below the headings.

## [0.3.1] — 2026-10-24

Layout only: the app did everything, but the page had grown a stack of
collapsed panels between the paste box and the queue.

### Changed

- **The occasional panels moved behind one Options button.** Download settings,
  backup, phone access and the excluded list used to stack between the paste box
  and the queue, which pushed the queue itself down and made the top of the page
  a wall of collapsed cards. They are the same `<details>` elements with the same
  ids, now switched inside a dialog from a tab strip in it. Settings that belong
  to adding to the queue are one click away instead of in the flow.
- **The queue columns line up.** "Waiting" and "Finished" both sat 24px down
  from where they should have been — the first-child rule counted a hidden
  "Needs attention" as the first child — so the two headings started at different
  heights. Section heads are now flush with the layout, and the gap between
  stacked sections is owned by the section above it, which a hidden section takes
  with it.

## [0.3.0] — 2026-10-24

The web app is the same app. This is the release that makes it installable:
a window, its own server inside, and installers you can hand to another
computer.

### Added

- **A desktop app.** A Tauri 2 window (`src-tauri/`) that shows the server's own
  UI — no second frontend, no second download engine. On start it either
  attaches to a server that is already running or spawns the one bundled inside
  the app; on close it stops only a server it spawned itself (SIGTERM, then
  SIGKILL, with `PR_SET_PDEATHSIG` as a backstop), so a server you started by
  hand keeps running.
- **No system Python needed.** The backend ships as a PyInstaller onedir tree
  (`packaging/sidecar/`) with `ffmpeg`, `ffprobe`, `yt-dlp` and
  **instaloader 4.15.4** inside it. `bin/instaloader` runs instaloader through
  the same frozen interpreter, so Instagram downloads work on a machine that
  has never had Python installed.
- **Installers.** A `.deb` and an `.AppImage`, both built in a container
  (`src-tauri/build-in-container.sh`) because an immutable Fedora host has no
  GTK/WebKit `-devel` packages. The AppImage is packed with `appimagetool`
  because linuxdeploy cannot handle the hashed private libraries PyInstaller
  emits.
- **Backup and restore, in Settings.** One zip with the queue, download
  settings, hidden and excluded items, bookmarks, likes and the people you
  imported — the things an archive cannot rebuild — plus an optional instaloader
  session. The gallery cache, thumbnails and avatars are deliberately left out:
  they are keyed by inode and rebuild themselves. Restore validates the whole
  zip before writing, keeps a pre-restore copy, remaps the archive root onto the
  machine it lands on, reloads the engine in place, comes back **paused**, and
  refuses while jobs are running. One automatic backup a day, seven kept.
- **`INSTA_DATA=/some/folder`** puts the state, database, caches, thumbnails,
  avatars, backups and the login in one directory, so the app's memory travels
  with a folder. Unset, every path stays where it was and all the existing
  `INSTA_*` overrides still win.
- **Phone access, with a switch.** The server listens on every interface so the
  switch can work without a restart; a request from another machine is refused
  — a small page telling you where the switch is, or a 403 for the API — until
  Settings → *Open on your phone* → **Reachable from your phone** is on. That
  card also shows the address to type on the phone (your LAN address, plus any
  tailnet address, with docker bridges filtered out) and copies it. There is no
  password: on means anyone on this network can use the app.
- **A rate-limit pause waits for you.** A 429 used to resume the queue the
  second Instagram's cooldown ran out, which walked straight back into the same
  limit. The pause now stays, the banner says *rate limit over — press Resume*,
  and the setting `hold_after_limit` (on by default) is what says so; turning it
  off restores resuming at zero.
- **One instance, on purpose.** A second launch attaches to the running server
  instead of starting a rival queue (`--attach`, `--force`, `--no-browser`), and
  `/api/version` reports the same number as the installed app.

### Changed

- The version is one file: `VERSION` feeds the server, `tauri.conf.json` and
  `Cargo.toml`, and four tests fail the build if they disagree.
- New documentation: `docs/desktop-shell.md`, `docs/sidecar.md`,
  `docs/backup.md`, `docs/ci.md`, `docs/upstream-sync.md`.
- Tests: a sandbox harness (throwaway `HOME`, stub downloaders, a free port) and
  63 `unittest` cases, a UI baseline (DOM landmarks plus screenshots) and a CI
  workflow with a local twin script, so a red run can be reproduced here.

### Known gaps

- The bundled `ffmpeg` is dynamically linked and wants FFmpeg 8.x sonames
  (`libavcodec.so.62`, …). Without them, video thumbnails and previews fail
  while everything else keeps working.
- The bundled backend needs glibc 2.38 or newer; Ubuntu 22.04 will not start it.
- The AppImage's *window* needs GTK 3 and WebKitGTK on the host. The server
  inside it does not, so a machine without those can still run the backend and
  use the browser UI.
- There is no authentication on the HTTP API. The phone switch is the only
  control, so "reachable from your phone" means reachable by everyone on that
  network — keep it off on shared Wi-Fi.
- macOS and Windows builds are written but have not been run, and the phone app
  is still the browser's install-to-home-screen.

## [0.2.0] — 2026-10-23

### Added
- **Bookmarks, with categories you name.** A bookmark is a second, higher
  intention than the heart: the heart says *I like this*, the bookmark says *find
  this again*. `🔖` saves a post from a profile grid, the enlarged view, or the
  Shorts rail (`b`), and saving one never likes it or moves a file out of place.
  The `🔖 Bookmarks` chip in the Gallery shows everything saved across profiles,
  newest first, with chips for **All**, **Uncategorised** and each category you
  make — `＋ Category` adds one, `✎ Rename` and `🗑 Delete category` act on the
  one being shown. `🏷` on a tile (or in the enlarged view) files that bookmark
  under a category, or takes it back out. Deleting a category never deletes
  bookmarks: they return to Uncategorised. The saved state travels with the post,
  so a bookmark made in Shorts shows filled in its profile's grid and in the
  timeline; a bookmark whose file is gone from the archive is forgotten rather
  than left as a tile that cannot open, while an unplugged drive merely hides it
  until it is back. Stored in the same SQLite database as everything else
  (`bookmarks`, `bookmark_categories`).
- **The enlarged view zooms with the wheel.** Over the picture, the wheel grows
  or shrinks the whole frame around the pointer, from 40% to 400%, with the
  percentage shown in the corner while it moves. It scales the picture rather
  than cropping to a magnified patch, so nothing is cut off until the frame is
  larger than the stage; past that, drag to see the edges. Double-click returns
  to the plain fit, stepping to another post starts fresh, and the wheel over
  the caption still scrolls the caption.
- **Finished can be sorted by how much it got.** The finished list has a `sort:`
  button that cycles *as added* → *most files* → *fewest files*. "Most" reads the
  folder count taken when each run finished (falling back to the posts that run
  grabbed), so the list answers "who did I actually get a lot from" instead of
  making you scroll 41 pages to find out. A grouped profile takes the largest
  count across its passes — each pass recounts the folder, and the fullest is the
  archive's size. "As added" is the default and is exactly the old order, so the
  list does not move unless you ask it to. The choice is remembered between
  visits, and re-sorting jumps back to page 1.
- **A profile view says whose profile it is.** The grid used to open with a row
  of controls and no name anywhere: a plain card, a merged one and either site
  all looked alike. It now leads with the avatar, the handle, and a badge naming
  the site — Instagram, TikTok, or *Instagram + TikTok* when the card folds the
  same handle from both. A merged card also spells out its split ("Same handle
  on both sites · 3 Instagram · 379 TikTok"), which is the answer to the obvious
  puzzle of why one grid holds two kinds of tile. Merged cards in the profile
  list name the other site too (`+ @handle · TikTok`). Which badge shows is
  counted from the posts actually loaded, not from the tab you arrived on, since
  a merged card appears under both tabs.
- **A timeline of one profile.** The Timeline chip does what the Shorts tab
  already did: opened from inside a profile's grid it shows that profile alone —
  their months on the slider, their posts, their year list — with a chip naming
  them that goes back to everyone. Opened from the profile list it is still the
  whole archive. The scope is carried in the API too (`/api/timeline` and
  `/api/timeline-month` take `profile=`), and each scope is cached separately, so
  a month request can never be answered from another scope's list. A merged card
  is one card, so its timeline walks the family's Instagram handles; the same-name
  twin on the other site is skipped rather than counted twice.
- **One instance per state file, and a way to ask which one.** A second start used
  to move itself to the next free port and run a second worker over the same queue
  and the same archive. It now finds the lock the first instance wrote beside its
  state file, checks that the recorded process is alive and answering
  `/api/health`, prints `HardlyWatching is already running at <url>` and exits 0
  with nothing started. A lock left behind by a killed process is stale and taken
  over on the next start, so a crash never keeps the app down. `--attach` prints
  the running instance's URL and exits (0 when one is up, 1 when none is) without
  starting anything, `--no-browser` serves without opening a tab, and `--force`
  starts a second copy deliberately, on the next free port, leaving the lock with
  the first so `--attach` still names the canonical one. `INSTA_LOCK` moves the
  lock file itself. `GET /api/version` answers `{version, pid, port, started,
  api}`, so a shell or a script can identify the build holding a port before
  trusting the rest of the API.

### Changed
- **One heart, one word: everything is "Liked".** There were two names for the
  thing the ♥ does — *Favourites* for the posts it files away and *Starred* for the
  bookmark it put on a clip, the second one under a ★ of its own — so the same idea
  had a chip, a heading, a filter and a bulk button under each name. It is liked
  everywhere now: `♥ Liked` in the gallery bar, `♥ Liked (n)` over the group,
  *Add to liked* / *Remove from liked* in the viewer, *Liked* / *Unliked* in the
  toasts, *♥ Liked only* in Shorts, and the archive-wide list is one chip instead
  of two. The star is gone: its button in Shorts' rail, the `★ Star` bulk button
  and the `★ Starred` chip are all removed.
- **Starred clips were folded into Liked, not thrown away.** A star was a flag on
  the clip rather than a folder, so it had its own list; the gallery's Liked view
  is now the favourites folder **plus** every clip liked in the viewer or in
  Shorts, with one entry per file (a clip can be both). On first start the old
  column is copied into `liked` — keeping the star's own timestamp for ordering —
  and then cleared, so anything that was starred is liked now and nothing has to be
  re-marked. The column is left in the database rather than dropped, and the old
  `star` action names still answer as likes, so a page cached from before this
  change keeps working.
- **The header is the name and the drive space, and nothing else.** The line under
  the title ("paste profiles → queue runs one at a time…") restated what the paste
  box says, cost a row on every tab and was the thing sitting over the menu, so it
  is gone. So is the header **Settings** button: its whole job was to open the
  Download settings section and scroll the page down to it, and that section
  already leads with its own summary line in the tab it belongs to. On a phone the
  name, the drive chip and the three tabs are centred, the tabs share the row in
  three equal columns (the gaps either side of the middle one are identical), and
  the chip has a real gap above the tabs instead of perching on them.
- **The Download bar's two most-used controls are symbols.** Start/pause is one
  button carrying ▶ and ❙❙, and the folder is an icon hard against the right edge;
  the two travel together, held clear of the session and counter chips, and neither
  changes width — the words used to move the whole row every time the queue's state
  changed. A resume waiting out an Instagram limit keeps its warning as an amber
  ring, since there is no text left to say it.
- **"Clear finished" is gone from the bar.** A finished profile is still the handle
  you grab more from, so a button that empties the list sat among the queue's
  controls inviting the one irreversible click in the row. The Finished section
  keeps its own Clear, where the list it clears is on screen.
- **The gallery sort is two buttons, not one cycling four ways.** `A→Z` / `Z→A`
  and `count ↓` / `count ↑` each name their own axis, the one in use is outlined,
  and pressing the axis already in use reverses it. Getting from "most" to "Z→A"
  used to take three taps, and the single label could only describe half of what it
  sorted by. The unused axis keeps its own direction, and both survive a reload;
  the old one-key value is read once so the grid does not jump on the first load
  after this change.
- **Adding photos defaults to the profile you are in.** The upload box's picker
  started on "(new name…)" no matter where you were, so putting a photo into the
  profile you had open meant finding that name again in a list of hundreds.
  Opening someone's gallery now points the picker at them, and it follows you
  between profiles; a name typed by hand still takes priority. Leaving the
  profile keeps the last default rather than resetting, since the person you were
  just looking at is the likeliest destination. Opening a profile from a link —
  which never renders the list — adds that one name to the picker instead of
  leaving it empty.
- **The heart reacts, and the view you were in survives a reload.** A heart
  springs once when it changes, so the tap registers before the server answers,
  and each post takes one favourite move at a time — a second tap mid-flight used
  to start a competing move over the same files. The current view is written into
  the URL (`?tab=gallery&profile=…&platform=…`, plus `plat=tiktok` for the queue),
  so reloading keeps the tab, the site and the open profile instead of dropping
  you back on Download. Pasting that link elsewhere opens the same page.
- **TikTok's logins live in one place: Sessions.** The Download → TikTok tab
  carried its own session card (save from browser, forget) on top of the list the
  Sessions panel already keeps — two copies of the same control, and only the
  panel could show more than one saved login, rename one or say which was in use.
  The card is gone; the tab keeps a line pointing at Sessions, which lists every
  saved login, marks the one downloads use, and renames or deletes each.

### Fixed
- **The gallery head no longer wraps into thin columns on a phone.** The title, the
  profile count and the two sort buttons shared one line, so the title broke over
  three lines, the count split its number away from its word, and the count button
  stacked its arrow under its label. On a phone the title and the count take a line
  each, and the two buttons share a row of their own, right-aligned, one line each.
- **The installed app no longer opens under the phone's status bar.** The page asks
  for a translucent status bar, so on a phone home-screen install the web view
  draws full-screen and the heading, subtitle and tabs sat behind the clock and
  the notch. It now asks for `viewport-fit=cover` and pads the top (and the sides
  and bottom) by the device's safe-area insets, so the layout starts below the
  status bar and the home indicator; on a desktop browser the insets are zero and
  nothing moves. Anything scrolled to — the Download settings
  section, the Excluded list — also keeps the inset clear instead of landing
  its own heading under the clock.
- **The heart agrees with itself in All Favourites and All Trash.** Un-favouriting
  from the enlarged view in an archive-wide list flipped the heart in the viewer
  but left the tile behind it showing ♥ — and the count line still claimed the old
  total — until a reload. The tile under the viewer is only refreshed for an open
  profile, and an archive list has none, so the refresh returned immediately. The
  refresh now knows which of those views can gain or lose a post, and redraws that
  view when one moves: the tile goes, the count follows, and the viewer keeps
  playing through the redraw because it lives outside the grid.
- **The profile chip in Shorts takes a tap.** The bottom bar is
  `pointer-events:none` so its gradient never eats a swipe, and the chip inside
  it never re-enabled pointer events — it painted perfectly and every tap went to
  the video underneath, so the picture and handle did nothing. The chip now takes
  pointers with a slightly larger touch target (padding, cancelled by an equal
  negative margin, so nothing moves), and a real click at its centre opens
  `?tab=gallery&profile=…&platform=…` with the right tab already chosen. Worth
  saying plainly: this was tested before with `element.click()` in a script,
  which dispatches straight to the node and never consults hit-testing, so it
  passed while a finger could not reach it.
- **The installed app has a real icon.** Both manifests declared a single
  `data:` URL SVG at 192px, `index.html` declared no icon at all and
  `/favicon.ico` was a 404 — so Android's install UI and splash screen had
  nothing at 512, iOS fell back to a screenshot of the page, and the browser tab
  showed a blank. `make_icons.py` now draws the same mark into a 192, a 512, a
  maskable 512 (background to the edges, artwork inside the safe zone) and an
  opaque 180 for the home screen, plus a proper multi-size `.ico`; the server
  serves all of them and both manifests and both pages point at the files.
  Chrome reports the manifest and the install criteria as error-free with the
  new set. There is still no service worker, so nothing is cached and the
  installed copy needs the server — see the README for why that is deliberate.
- **A throttled session is no longer reported as a crash.** When instaloader
  cannot verify the saved session it falls back to asking for a password; in the
  queue there is no terminal to ask, so `getpass` died with `EOFError` and the
  job was filed as "instaloader crashed", losing an attempt, when the truth was
  Instagram answering the session check with `401 … Please wait a few minutes`.
  The password prompt is now recognised before the traceback is, and the run
  backs off like any other rate limit with a message that says so. A genuine
  Python crash still fails the job, and a profile that really is gone still
  goes fatal.
- **A favourite that had left a placeholder behind could not be undone.** A post
  moved to `favorites/` leaves a 0-byte placeholder in `main/` — that is what
  stops instaloader fetching a deleted post again — and the move code took that
  placeholder to be the file itself. Un-favouriting then answered "already
  there": true of the placeholder, untrue of the post, so nothing moved at all,
  the heart flipped anyway, and the next load put it back. Moves now work from
  the copy that has real bytes, choose the copy in the folder it is moving *out*
  of, and clear a placeholder that is in the way.
- **The deletion detector treated a favourite as a deletion.** It compares
  `main/` against the folder's remembered index, and a file that had just been
  favourited looked exactly like one deleted outside the app: it wrote a
  placeholder and a tombstone for a post you had merely moved. Anything now
  sitting in `favorites/` or `trash/` is exempt, which also closes the race that
  produced the placeholders in the first place. Existing placeholders need no
  cleaning up — un-favouriting goes straight through them now.
- **Two real copies of one file are no longer a coin toss.** If the destination
  already holds a real copy, the other one is retired to the desktop trash — but
  only when the bytes match. Two different files sharing a name are refused with
  a message rather than overwritten.
- **The enlarged view claimed success before the server had answered.** It fired
  the moves and toasted "Favourited" regardless, never noticed a refusal, and
  left the grid behind it on the old state — which is what made a like look
  undone until a reload. It now waits, reverts the heart and says *Could not
  update favourites* when a move fails, and updates the tile it was opened from
  in place, so the grid neither flashes nor loses its scroll.
- **A tile and the enlarged view could disagree about where a post was.** An
  in-place favourite updated the tile but the viewer still read the section from
  the payload the grid was built with, so a post you had just favourited opened
  with an empty heart, and tapping it moved the files the wrong way. The viewer
  now takes its section from the tile it was opened from.
- **A photo you added by hand shows the day it was taken, not the day it was
  copied here.** With no sidecar and no instaloader stamp, the gallery fell back
  to the file's own timestamp, so a batch of camera JPEGs all claimed the day
  they were dropped into the folder. The EXIF capture time is now read first
  (`DateTimeOriginal`, then `DateTimeDigitized`, then `DateTime`), which is the
  only place a hand-added photo records when it was taken. EXIF carries no
  timezone and is read as UTC, and a date before 2000 or after tomorrow is
  rejected as a wrong camera clock rather than believed. A photo with no EXIF
  still falls back to the file timestamp and is still marked with `~`.
- **TikTok dates were the date you downloaded a video, not the date it was
  posted.** TikTok files are named by video id and their yt-dlp metadata lives in
  `<id>.info.json`, which the gallery never looked at — it looked for `<id>.json`,
  found nothing, and fell back to the file's modification time. So every video
  carried the day it was saved to disk. The date now comes from the video's own
  metadata (`timestamp`, else `upload_date`), falls back to the id itself — TikTok
  ids are snowflake ids, so the creation time is recoverable from the number —
  and only then to the file's mtime, labelled approximate as before. Cached post
  lists carry a recipe version, so this kind of parser fix drops them instead of
  leaving the old answers in service.
- **A saved profile picture is no longer fetched again.** instaloader asks for
  it on every run — `--fast-update` does not cover pictures — and the
  high-quality copy costs its own API call. When Instagram answers that call
  with a `"fail"` payload, instaloader exits non-zero for "Errors or warnings
  occurred" and the profile is retried, repeatedly, over a picture it already
  has on disk. `--no-profile-pic` is now passed once a picture is saved in the
  profile's folder, and that warning on its own can never set a run's error,
  its retry, or a fatal verdict. A profile with no picture yet still asks for
  it, so the gallery keeps getting one.
- **Selection mode in the gallery.** *☑ Select* (or holding a tile) turns tapping
  into picking: choose as many tiles as you like and then Star, Like, Favourite,
  Trash them — or Restore from the trash view, or Un-favourite from the favourites
  view — in one go. Done, Escape, or a tap outside leaves it. Trashing from the
  full-screen viewer no longer opens a hidden dialog: the confirmation was at
  z-index 60, below the viewer at 70, so the viewer was all you could see.
- **"All" can mean a slice at a time.** A TikTok profile set to *all* can now
  fetch 25 videos a turn and then go to the back of the queue, so the next profile
  gets a turn and this one comes round again instead of holding the queue until it
  is finished. Settings → TikTok (yt-dlp) → *"All" a slice at a time*, with the
  turn size beside it. Off by default, and it only changes what *all* means: a
  profile given an explicit number still fetches that many and finishes. Each turn
  picks up where the last one stopped rather than re-reading the same videos.
- **Pause works between profiles now.** The gap between profiles was the one
  moment the button could not stop anything. Two copies of the run button's logic
  had drifted apart: the main poll asked whether `st.current` was set, and that
  is an object of platform -> username, so it is truthy even when nothing is
  running -- the button read "Pause". The click handler looked at the platform's
  own entry instead, found nothing running, and fell through to **Start**, which
  skips the gap and begins the next download. Pressing Pause during pacing made
  it download sooner. Both now share one painter, and a pacing gap counts as
  working, so the button offers Pause and actually pauses.
- **TikTok has a session of its own.** Instagram always kept session files; the
  TikTok side only ever held a pointer at a browser profile, re-read on every
  run. That is not a session the app has -- it lasts exactly as long as that
  browser stays signed in and its profile directory stays put, and nothing could
  report whether it was there at all. **Save session from browser** copies the
  login into `tiktok-cookies.txt` under the config directory (mode 0600, it is a
  credential) and downloads use that jar; the browser becomes the fallback for
  browsers whose cookies cannot be copied. The card shows what is held -- how many
  login cookies out of how many, and when it was saved -- so "is there a session"
  is answerable, and **Forget session** drops it.
- **Sessions can be locked.** A flagged account could be picked up again with one
  click, and a service restart put it straight back to work. Every session now has
  a **Lock** button: the lock is saved in `state.json`, a locked session is never
  auto-chosen and never used for a download, and picking it is refused until you
  press **Unlock**. Locking the session in use drops it so the engine falls back to
  another saved one instead of stalling. If every session is locked, the Instagram
  worker holds and the queue says so rather than quietly downloading anonymously.
- **Opening a profile's gallery from its queue entry.** The picture on a card is
  clickable and opens that profile's gallery directly, and every card has a
  **Gallery** button beside Open — which matters for TikTok entries, since they
  have no picture to click. Neither route goes via the gallery's profile list, so
  it is one click from the queue to the files.
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
- **The Sessions panel follows the platform tab, and TikTok can hold more than one
  login.** TikTok had a single session slot, so saving a second account silently
  destroyed the first, and the panel opened the Instagram account list whichever tab
  you were on. TikTok now keeps each captured login as its own session with
  **Use** / **✎** / **✕** like the Instagram side: swap between accounts, name them,
  and delete one without disturbing the session in use.
- **The search box sits above the queue and finished lists.** It was at the bottom
  of the pager, so a long queue had to be scrolled past before you could type in
  it. The paging controls stay at the bottom; a search with no matches keeps its
  box on screen, because hiding the box would strand the filter with no way to
  clear it.
- **The button labels are gone** from the Shorts rail. Each action is the icon
  alone, with the words kept as tooltips and labels for screen readers, so
  hovering or long-pressing still says what it does. The topbar filter reads as a
  star rather than the word "Favourites". The skip buttons keep their number --
  that is the step you set by tapping three times, not a name for the button.
- **Shorts for one person.** Opening the Shorts tab while a profile is open in
  the Gallery shows only that person's clips. The filter travels in the player's
  URL, which the app sets when the tab opens and reloads when the open profile
  changes, so what is on screen and what is filtered cannot disagree; the count
  line reads "1 / 8 · only @someone" so a filtered feed is never mistaken for a
  small archive. Returning to the profile list goes back to the whole feed.
  Reaching it: open someone in the Gallery, then tap Shorts.
- **The Shorts feed no longer belongs to whoever has the most videos.** A
  profile's share of the feed was proportional to its clip count, which is not a
  judgement about the person, just about how many files they happen to have: of
  484 profiles, the top ten took 63% of the feed and the 378 with five clips or
  fewer took under 6% between them. Each clip is now scaled by
  `1 / count ** 0.5` (`ALGO_CLIP_BALANCE`), so a profile's share follows the
  square root of its clip count. Measured on the real archive: the small profiles
  go from 5.9% of the feed to 38.3%, the top ten from 63.5% to 24.5%, and the
  profiles seen in a long run from 340 to 483 of 484. The constant is one number:
  0 restores the old behaviour, 1 gives every profile a flat equal share.
- **Pause stops the profile downloading now, not after it.** Pressing Pause kills
  the running `instaloader`/`yt-dlp` process for that platform and puts the
  interrupted profile back at the **front** of its queue, where it restarts on
  resume. Previously a long run had to finish first, so sitting out a block could
  take another twenty minutes. Nothing is half-written — instaloader writes media
  to a `.temp` file and renames it into place only when complete, and yt-dlp keeps
  `.part` files — so the next run re-fetches cleanly. A pause you press is now
  persisted too: a service restart, deploy or reboot comes back paused instead of
  silently resuming, and only **Resume** starts it again. A rate-limit pause is
  tracked separately and still clears itself when its cooldown expires.
- **One paste box for both platforms.** The Instagram and TikTok tabs each had
  their own box, so a mixed list meant two pastes on two tabs. There is now a
  single box at the top of the tab, with the Instagram/TikTok buttons sitting
  under it, and a mixed paste of Instagram and TikTok links goes in at once, one
  per line.
- **A TikTok profile opens straight away.** TikTok offers no profile embed, so
  the in-app panel could only say "there is nothing to show here" and ask for a
  second click on the link inside it — a click and a scroll to reach a page whose
  address we already had. Clicking a TikTok profile (its name, or the TikTok
  button in the menu) now opens it on tiktok.com immediately. Instagram keeps the
  panel, because its embed does show something.
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
- **The Shorts controls can be hidden with a mouse too.** A swipe has no desktop
  equivalent, so there is a small 👁 in the topbar and an invisible strip along the
  right edge that reveals the controls while the cursor is in it and puts them back
  afterwards. The strip is gated on the pointer that arrives, not on a guess about
  the device, and is removed from the layout entirely where nothing can hover — a
  phone keeps the swipe and never gets a dead band down its edge.
- **TikTok profile pictures now reach the queue cards.** They were being withheld
  by a rule written when TikTok had no pictures at all, so the cache, the endpoint
  and the gallery could all have them while the queue showed none. The one-time
  probe that decides whether a profile has a picture is now keyed by platform as
  well as name: one handle can be two accounts on two sites, and a verdict borrowed
  from the other site's lookup is simply wrong.
- **A Starred view in the Gallery.** A star is a bookmark — the higher-level
  favourite — and it lives on the clip rather than in a folder, so there was no
  way to see them together. The *★ Starred* chip beside All Favourites lists every
  starred clip across the archive, newest star first. `starred_at` is recorded when
  you star something, because `first_seen` is a sync stamp that every row in a pass
  shares and ordering a bookmarks view by it means nothing.
- **Swipe left or right in Shorts to show or hide the controls.** They start
  visible and nothing hides them on their own: no timer, and a refill does not pop
  them back after you have swiped them away. Either direction toggles — the rail is
  on the right edge where a thumb already is, and one specific direction on a
  control that does not slide would be a rule to remember rather than a gesture.
  The tap that ends a swipe is swallowed, so it does not also pause the clip.
- **The queue list was rebuilt twice on every poll, forever.** `paintPagedQueue`
  drew the rows once without their queue positions and then immediately drew them
  again with them — two different strings, so the "nothing changed, leave the DOM
  alone" guard never caught it and every card was thrown away and recreated every
  1.2s, even with nothing running. Measured: 2 mutations per poll, ~13 over eight
  idle seconds; now 0.
- **A change to one card no longer rebuilds the whole list.** Replacing the
  container's innerHTML costs the open log panels and the menus, and on a touch
  screen it costs the scroll: replacing the element a gesture started on cancels
  that scroll. Only the rows whose text actually moved are replaced now — measured
  by moving one job's counters and repainting: 1 row replaced, 9 kept their DOM
  nodes.
- **A repaint no longer lands mid-scroll.** Held off for a moment while the page
  has been scrolled or touched, and retried on the next poll.
- **TikTok profile pictures.** A TikTok archive held none at all — yt-dlp is never
  asked for one, so all 402 profiles had nothing but videos. The account's own
  page carries the picture in its rehydration blob and one plain GET returns it,
  so `tools/tiktok_avatars.py` fetches them, paced, and each profile's download
  now fetches its own. They are cached rather than written into the profile
  folder: the gallery scans every picture in a folder as media, so a file put
  there appears as an extra post, and an avatar is derived from a URL rather than
  archive content.
- **Accounts that are the same person show as one card.** An identical handle on
  both sites is folded into a single entry automatically, and any two accounts can
  be merged by hand for burners and finstas. Gallery view only: the folders, the
  jobs and the downloads are untouched. Opening a merged card shows the posts from
  every account in it, each still knowing which profile and platform it came from,
  so favouriting, moving or trashing acts on the folder the file is really in.
- **Splitting, which stays split.** A merged card offers a way to say "these are
  different people". That drops the merge and remembers the handle, so the
  automatic same-name rule leaves it alone from then on — an automatic rule that
  silently undoes a decision is worse than having no rule.
- **A profile picture in the viewer.** Tapping it opens that person's gallery, and
  going back to Shorts returns to the same clip and, from then on, shows only that
  person. The viewer is an iframe, so the tap is sent to the app as a message
  rather than a navigation, which is what keeps the sitting intact.
- **`PRIMARY` is a reserved word in SQLite**, so the column named that made every
  request fail with a syntax error while the service still reported itself as
  running. The column is `parent` now. Tables are also created before columns are
  migrated, since adding a column to a table that does not exist yet fails on a
  fresh install.
- **Shorts has stars, separate from likes.** A star is a bookmark -- the thing you
  want to find again -- and a like is a reaction, so they are different columns
  and neither implies the other: a clip can be liked, starred, both or neither.
  The star sits in the rail beside the heart (gold, against the heart's pink, so
  they do not read as one control in two states) and the topbar filter is now the
  star: filled while you are looking at starred clips only.
  `starred` is a new column; existing likes are untouched and stay likes.
- **The Shorts feed stops getting slower the longer you scroll.** Every refill
  emptied the feed and recreated every clip element, so everything already loaded
  was thrown away and had to be fetched and decoded again — including the clip in
  view — and since the list only grows, each refill cost more than the last. Only
  the unseen tail is replaced now, so buffered clips survive a refill.
- **Clips are fetched before you reach them.** Every clip was created with
  `preload="none"` and only the one in view was told to load, so each swipe was a
  cold fetch and decode. The next two are now given their source while the
  current one plays, and the two behind are kept ready for a scroll back. Outside
  three clips either way the source is released, which keeps a long sitting from
  holding a whole session of decoded video.
- **A clip scrolled past no longer becomes unplayable.** The source was removed
  once a clip was more than three screens away and nothing ever put it back, so
  scrolling up to something you had passed left a dead player. The source is
  restored when the clip is in range again.
- **The app now opens on the tab it was asked for.** The `?tab=` handler was
  nested inside `showPlatform()` by mistake, which broke it twice: it never ran at
  load, so an installed phone app (whose start URL is `/?tab=shorts`) opened on
  Download instead of Shorts; and clicking the Instagram/TikTok sub-tab ran it
  late, throwing you onto Shorts mid-use. It is now a single top-level call at the
  end of the script. Shorts lands with the whole app around it — the tabs and the
  menu are there, so leaving it is a tap rather than a back button.
- **Clearing a tombstone now sticks.** It removed the placeholder but left the
  name in the folder's `.insta-index.json`, so the next gallery load compared the
  index against the folder, saw a file it remembered but could not find, and
  concluded it had been deleted outside the app — writing the placeholder
  straight back and re-recording the tombstone. "Post will re-download" was
  untrue in exactly the case the feature exists for. The index entry is dropped
  too now, which is what makes the clear hold.
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
