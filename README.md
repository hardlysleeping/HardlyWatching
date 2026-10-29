# HardlyWatching

### A self-hosted viewer and download queue for Instagram and TikTok

Paste the profiles you follow, let them download over days, and browse what you
have collected in one place — grid, timeline, and a full-screen short-video feed
for your phone. Both platforms share one queue and one gallery.

## ➜ Install the Flatpak

**[Click here to install it](https://hardlysleeping.github.io/HardlyWatching/hardlywatching.flatpakref)** —
your software installer opens with the signed repository already added, and
`flatpak update` keeps it current. From a terminal, the same thing:

```bash
flatpak install https://hardlysleeping.github.io/HardlyWatching/hardlywatching.flatpakref
```

It brings its own Python, ffmpeg, instaloader and yt-dlp, so there is nothing else
to install. No Flatpak on your machine? There is an
[**AppImage** and a **DEB**](https://github.com/hardlysleeping/HardlyWatching/releases)
on the releases page, and from source it is `python3 run.py`. Each one's files,
port and dependencies: [Install a release build](#install-a-release-build).

> **⚠️ Use at your own risk.** Downloading at scale can get your IP blocked,
> rate-limited, or your account banned. Read [Use at your own risk](#use-at-your-own-risk)
> before you install anything — it is short, and it is the part that can cost you an
> account.

> ### It is a web UI for **[instaloader](https://instaloader.github.io/)** and **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — it has no downloader of its own.
>
> <img src="docs/instaloader-logo.png" width="64" height="64" alt="instaloader logo" align="left" hspace="12" vspace="4">
>
> Every file on disk is written by one of two separate, mature command-line
> tools, and this repository runs them:
>
> | what you download | the tool that does it | project |
> |---|---|---|
> | Instagram profiles | **instaloader** | [docs](https://instaloader.github.io/) · [PyPI](https://pypi.org/project/instaloader/) · [source](https://github.com/instaloader/instaloader) |
> | TikTok profiles | **yt-dlp** (with ffmpeg) | [docs](https://github.com/yt-dlp/yt-dlp#readme) · [PyPI](https://pypi.org/project/yt-dlp/) · [source](https://github.com/yt-dlp/yt-dlp) |
>
> This project adds a browser interface on top: a persistent queue, pacing, and a
> gallery. It reimplements neither downloader and never talks to Instagram or
> TikTok itself — it builds their command lines and runs them.
>
> **Want a downloader? Install [instaloader](https://instaloader.github.io/) or
> [yt-dlp](https://github.com/yt-dlp/yt-dlp).** If you have them and only want them,
> you don't need this project at all.
<br clear="left">

**What this adds:** paste 200 profiles once and let them drain over days, survive
reboots, and browse what you hoarded.

---

## Use at your own risk

**This is the section to read.** Downloading at scale can get your **IP address
blocked, rate-limited, or your account banned** — by Instagram, by TikTok, or by
your network provider. That is true of instaloader and yt-dlp used directly too;
this project does not make it safer, it just makes it easier to run a lot of it
unattended.

The pacing exists to make you *less likely* to be noticed, not to guarantee you
won't be. It is a speed bump, not a shield. Things that make a block or ban more
likely:

- Adding hundreds of profiles and leaving it running for days
- Shortening the waits between profiles in Download settings
- Logging in with a password instead of using a saved session
- Running it from a shared or datacentre IP, or a VPN others also use
- Downloading the same account repeatedly after it has started refusing you

A block usually looks like empty downloads, `feedback_required`, HTTP 429, or
login failures. If that starts happening, stop for a day rather than retrying —
retrying is what turns a temporary throttle into a longer one. **Your account is
the thing at risk here, and it is not recoverable by editing a setting.** Use a
throwaway account for bulk archiving.

**No warranty.** This software is provided as-is. If your account is limited or
banned, if a download fails, or if a file is lost, that is on you, not on this
project. Nothing here is designed to evade detection.

This is a local tool with **no authentication** — anyone who can reach the port
can start logins and delete files. Do not expose it to the internet.

---

## Screenshots

**Queue** — paste profiles, add them, start. Instagram and TikTok download in
parallel, one profile each, with the waiting and finished lists side by side. The
two lists line up on the same row, and the occasional panels live behind
**Options…** rather than stacked under the paste box.

![The queue screen: profiles waiting and finished, with the page and search controls](docs/queue.png)

**TikTok** — the same screen for TikTok, with per-profile controls for how many
videos to grab. Start with five, raise it later; only the missing ones download.

![The TikTok queue: profiles with grab-newest count controls](docs/tiktok.png)

**Downloading** — one profile runs at a time, pinned in a **Downloading now** row
above both lists while the rest of the queue waits.

![A profile downloading with the rest of the queue waiting](docs/downloading.png)

**Gallery** — every profile you have archived, plus archive-wide views and a box
to add your own photos or folders.

![The gallery: profile cards, with Liked, Bookmarks, All Trash and Timeline](docs/gallery.png)

**One profile** — its posts as a grid, filterable by media type and sorted however
you like. The top says whose profile you are in and which site it came from.

![A single profile's post grid with media filters and sort options](docs/gallery-profiles.png)

**Timeline** — every post from every profile in one chronological stream, grouped
by month, with a slider between months and a filter for what you liked. Opened
from inside a profile it stays on that person.

![The timeline: year and month selectors, a month slider, and a row of post tiles](docs/timeline.png)

**Enlarged view** — click any post. Arrows (or the arrow keys) step through posts;
like, bookmark, download a video, or trash it. The wheel resizes the picture
(40–400%), double-click resets it.

![A post enlarged over the grid, with previous and next arrows and action buttons](docs/viewer.png)

**Bookmarks** — everything you saved, newest first, with category chips and the
controls to add, rename and delete categories.

![The bookmarks view: saved posts with category chips and category actions](docs/bookmarks.png)

**Shorts** — a full-screen, phone-sized feed of your reels and TikTok videos.

![The shorts feed on a phone-sized screen](docs/shorts.png)

**Sessions** — every saved login for the platform you are on, with the one in use
marked, plus grabbing a login out of your browser.

![The sessions panel: saved sessions with Use buttons, one marked in use](docs/sessions-panel.png)

**Options** — download settings, backup and restore, phone access and the
excluded list were four collapsed panels between the paste box and the queue.
They are one button and a tab strip now, so the queue is what the page shows.

![The Options dialog on its Download settings tab](docs/options.png)

**Backup & restore** — export everything the app *remembers* into one zip, and
restore it on any copy of the app, here or on another machine. The archive
folders are not in the zip; the gallery and shorts already rebuild from them.

![Options, the Backup & restore tab, with saved backups and an export button](docs/options-backup.png)

**On a phone** — the same app at phone width. The phone card in Options has the
switch, the address to type and a copy button.

![The queue on a 390px-wide screen](docs/queue-phone.png)

![Options, phone access, with the switch on and the address to type](docs/options-phone.png)

---

## Install and run

You need **Python 3.10+** and an **Instagram account**. You do **not** need to
install instaloader yourself — `run.py` does it for you.

```bash
git clone https://github.com/hardlysleeping/hardlywatching.git
cd hardlywatching
python3 run.py
```

That is the whole install. `run.py` creates a private environment in `.venv/`
next to itself, installs instaloader into it, starts the app and opens your
browser. Nothing touches the rest of your system and nothing needs `sudo`. First
run takes ~20 seconds, mostly pip; after that it starts instantly.

Then:

1. **Log in** — click **Sessions** and grab the login already in your browser
   (see [Accounts and sessions](#accounts-and-sessions)).
2. **Paste profiles** — one per line. `username`, `@username` and full
   `instagram.com` URLs all work.
3. **Add to queue**, press **Start**, and leave it running.

Downloads land in `~/instagram/<username>/` in instaloader's normal layout, so
instaloader itself can still read the folder.

**Starting it twice does not give you two copies.** The first instance writes a
lock beside its state file, and a second start finds it, prints
`HardlyWatching is already running at <url>` and exits 0 with nothing started.
Two servers over one state file meant two workers pulling the same queue and two
windows writing the same archive; the lock is there so that cannot happen, and so
the second start tells you where the app really is instead of leaving you to guess
a port. A lock left behind by a killed process does not block the next start — the
recorded process is checked for being alive and answering `/api/health`, and a
stale lock is taken over. A copy with its own state file (`INSTA_STATE`) or its
own `INSTA_LOCK` is a separate instance, and takes the next free port.

To ask a running instance what it is, `GET /api/version` answers with the app
version, its process id, the port it holds, when it started and the API number:

```json
{"version": "0.2.0", "pid": 124968, "port": 8899, "started": "2026-10-23T16:09:57-07:00", "api": 1}
```

### Install a release build

Every [release](https://github.com/hardlysleeping/HardlyWatching/releases) has
Linux x86_64 binaries, built from the tag. They are the same app as
`python3 run.py` — one web UI, one queue, one engine — just packaged.

| | how | where its files go |
|---|---|---|
| **Flatpak** | [Install](https://hardlysleeping.github.io/HardlyWatching/hardlywatching.flatpakref) — opens your software installer, or `flatpak install https://hardlysleeping.github.io/HardlyWatching/hardlywatching.flatpakref` | `~/.var/app/io.github.hardlysleeping.HardlyWatching`; port 8777 |
| **AppImage** | `chmod +x HardlyWatching_*_amd64.AppImage` and run it | same as `run.py` |
| **DEB** | `sudo apt install ./HardlyWatching_*_amd64.deb`, then launch from the menu | same as `run.py` |

The flatpak carries its own Python, ffmpeg, instaloader and yt-dlp, keeps its own
queue and cache, and listens on **8777** so it cannot collide with an install
already running on 8765. It needs the `org.gnome.Platform//49` runtime from
Flathub, which the installer fetches; updates arrive with `flatpak update`. The
AppImage and DEB use your system's WebKitGTK, GTK3 and ffmpeg, so they want a
glibc 2.38+ distribution (Fedora 39+, Ubuntu 24.04+).

A release build keeps its state where that build expects it. Nothing follows the
source tree around: to move a queue from one copy to another, export from
**Options… → Backup & restore** and restore the zip on the other side.

### Other ways to start it

```bash
python3 run.py --service     # also set it up to start at login (Linux)
python3 run.py --no-install  # don't install instaloader, just run
python3 run.py --port 9000   # different port
./start.sh                   # if you already have instaloader on PATH
python3 server.py            # plain, standard library only
python3 server.py --attach      # print the URL of the instance already running
python3 server.py --no-browser  # start without opening a tab
python3 server.py --force       # start anyway, beside the instance holding the lock
```

Those three are `server.py` flags.

`--attach` starts nothing: it prints the running instance's address and exits 0,
or prints `HardlyWatching is not running.` and exits 1, which makes it the quick
answer to "is it up, and where" for a script as much as for you. `--no-browser`
serves as usual and leaves the browser alone — the banner says where to go rather
than claiming a tab opened. `--force` starts a second copy on purpose: it comes up
beside the first, takes the next free port, and leaves the lock with the instance
that already had it, so `--attach` still names that one; it says which instance it
is running beside.

**Platforms:** the app is standard-library Python and runs on Linux, macOS and
Windows. Linux gets the extras (autostart service, desktop icon, control panel).
On macOS/Windows everything in the web UI works the same, and delete-for-good
still goes to Trash / Recycle Bin.

Optional Linux extras:

```bash
./install-desktop-icon.sh                                       # menu icon -> control panel
./control-panel.sh                                              # small window: start / stop / restart / copy link
```

The control panel uses whichever of PySide6 or PyGObject is installed. Without
either you get a plain terminal prompt with the same commands (`start`, `stop`,
`restart`, `status`, `open`, `quit`). Closing the panel does **not** stop the app.

### Reach it from your phone

**Options… → Open on your phone** is the whole switch. Turn it on and the card
shows the address to type into the phone — the machine's LAN address, and the
Tailscale one if you have it — plus a copy button. Open that address on the phone
and the browser's *Add to Home Screen* puts a shortcut there; `/shorts` on its
own is the phone-shaped feed.

The switch is a gate, not a password. While it is on, anything that can reach the
port can use the whole app: the queue, download settings, backup export, file
deletion. Turn it off when you are not using it, and do not put the port on the
internet. The socket is open on all interfaces so the address in the card works,
but requests from other machines are refused with a short explanation unless the
switch is on.

Off, requests from other machines are refused. The server binds every interface
either way, because a loopback-only socket cannot answer the phone at all; the
switch is what decides. If you would rather not have the socket open, or you are
starting it from a script, the same thing is an environment variable:

```bash
INSTA_HOST=127.0.0.1 python3 server.py   # loopback only: the phone can never reach it
INSTA_HOST=0.0.0.0   python3 server.py   # all interfaces, still gated by the switch
```

The browser-login button opens a terminal on the *server* machine, so logging in
only makes sense on the machine it runs on — from the phone, the app is a viewer
and a queue.

### Autostart (Linux)

```bash
./install-service.sh          # install + start, enabled at login
./install-service.sh remove   # stop + uninstall
systemctl --user status hardlywatching
journalctl --user -u hardlywatching -f
```

**Start on login** can also be toggled from the app, under *Download settings →
App*. It sets the same systemd unit, so there is only ever one answer.

**A service start has no browser to open.** Under systemd, or in any script that
starts it for you, there is nobody to look at a tab, so pass `--no-browser`: the
app serves and prints its address and never calls the browser. To make the
installed service start that way, add the flag to the unit's `ExecStart` line:

```ini
ExecStart=/path/to/.venv/bin/python3 /path/to/server.py --no-browser
```

Nothing else about the start changes, and the address it prints is in
`journalctl --user -u hardlywatching`. Because the unit holds the state file,
starting the app by hand while the service is up just says where the service
already is and stops, so the same queue cannot end up with two owners.

---

## How it works

Two pieces:

- **instaloader / yt-dlp** make every request and write the files.
- **This app** is a queue and a viewer. It decides which profile runs next, runs
  the downloader for it, reads its output to show progress, and serves the
  gallery from the files on disk.

A job is one profile. **Instagram and TikTok each have their own worker**, so one
download per platform runs at a time and they run *alongside* each other — a
TikTok video starts in seconds even when the Instagram queue is hundreds of
profiles long, and an Instagram rate-limit pause does not stop TikTok.

State lives in `~/.config/hardlywatching/state.json`, so closing the browser,
restarting the app or rebooting all keep the queue, its order and any cooldown.
Only a profile that was mid-download restarts from the beginning.

**Pause** stops the profile downloading now instead of waiting for it to finish.
The interrupted profile goes back to the **front** of its queue and restarts when
you resume. Nothing is half-written: instaloader writes to a `.temp` file and
renames it into place, and yt-dlp keeps `.part` files. A pause **is** saved —
restarting or rebooting comes back paused, so nothing resumes behind your back. (A
pause caused by a rate limit is separate: it clears itself when its cooldown
expires, which a pause you pressed never does.)

Re-running a profile does not re-download it. instaloader's `--fast-update` stops
at the first file it already has, and `--latest-stamps` records what has been
archived **outside** the media folder — so moving the archive to another drive
never causes re-downloads.

---

## Why the limits are there

This is the part people skip and then get blocked.

**Instagram rate-limits by IP.** Push too fast and you get HTTP 429, or their
anti-bot check (`feedback_required`), which in instaloader's output often appears
as a misleading `Profile <name> does not exist`. The account is fine; you are
being throttled. Retrying harder is what turns a temporary throttle into a real
block.

Three layers protect you:

1. **instaloader's own limiter.** It counts its requests and waits before
   exceeding roughly 275 GraphQL requests per 10 minutes, plus per-type windows.
   This app does not disable it.
2. **A gap between profiles** — 120 seconds by default. instaloader has no
   inter-profile delay of its own, and this app restarts it per profile, which
   resets that internal accounting. The gap stops a burst of logins back to back.
   Raising it is safer; lowering it to 0 will get you throttled.
3. **A whole-queue stop on 429.** If Instagram says stop, the entire queue waits
   — 15 minutes, or whatever retry it asks for — instead of hammering. This
   single behaviour matters more than any other for keeping an account usable.

The practical consequence: a large archive takes days, not hours. That is the
trade for not being blocked. If you need it faster, archive fewer profiles per
day rather than turning the delays off.

---

## What you can do

- **Paste 200 profiles once and forget it.** They drain over days, and reboots or
  crashes don't lose the queue.
- **Know what happened.** Every job shows its status, how much it got, and the raw
  downloader output when something failed.
- **Skip the re-downloads.** Re-run anything; only new posts come down.
- **Browse the whole archive.** A grid per profile, plus ♥ Liked, 🔖 Bookmarks and
  🗑 All Trash across every profile, and a merged timeline grouped by month.
- **Works on a phone.** Swipe through the clips you liked full screen, tap to
  enlarge; on desktop the arrow keys walk post to post.
- **Multiple drives.** Keep old archives on one disk and send new downloads to
  another. Unplug a drive and its profiles drop out of the gallery until it is
  back; nothing is moved or copied without asking.
- **Your own photos too.** Any folder in an archive location appears as a profile,
  so non-Instagram pictures and phone uploads browse in the same grid.
- **Delete safely.** Trashing moves files to your desktop Trash and leaves a
  marker so the downloader doesn't fetch them again.
- **Recoverable removals.** Removing a profile never loses it — restore it from
  the Removed list.
- **Reels as a separate pass.** Optionally queue reels as their own follow-up job.

### The gallery

- **Profiles** — pick a person, see their posts as tiles. Videos play on hover.
- **♥ Liked** — everything you liked, across every profile, with a full-screen
  swipe view: the **favourites folder** (a like on a photo moves the file there)
  *plus* every clip you liked from the viewer or in Shorts.
- **🔖 Bookmarks** — everything you saved, newest first, with categories.
- **🗑 All Trash** — everything you set aside, with delete-for-good.
- **🕒 Timeline** — every post in one chronological stream, grouped by month, with
  a liked-only filter. Opened from a profile it covers that person alone.

**Order.** The profile list sorts by **name**, **posts** or **size on disk** (the
walk is cached for six hours and warmed at startup, so the numbers are there
before you ask). Inside a profile the posts sort by **date** or by the size of
their files, and the size badges only appear while that order is on. Every card
says `count · size` either way.

**Tagged.** A profile can be narrowed to the posts somebody else made — the ones
an `--tagged` pass dropped into the folder. Nothing on a tile used to say who
made a post; now each tagged tile carries the account that did, and the **🏷
Tagged** chip is always there, greyed out when the folder holds none.

**Going back.** Opening a profile from Bookmarks, Liked, All Trash, Timeline or
the profile list puts a floating **← where you were** button on the screen, and
it returns to that view at the scroll position you left it at. Pressing a view
chip instead starts a fresh journey, so the button never offers a stale one.

**No people.** A local detector (YOLOv8n as ONNX, 12.8 MB, CPU only) marks each
photo as having somebody in it or not, one profile at a time, saving every answer
as it goes — stopping or restarting loses nothing and the next run continues.
The profile grid then has a **👤 No people** chip. A picture that has not been
looked at, or that sits in a post with a video part, is left out of that filter
rather than counted as empty. It is off until the model is in place:
`pip install onnxruntime` and drop `yolov8n.onnx` in `~/.ai-tools/models/` (or
point `AI_PEOPLE_MODEL` at it). Without it the button is disabled and says so.

On a tile: **♡/♥** like, **🔖** bookmark, **🏷** file that bookmark, **⤢** open
bigger, **🗑** trash. In the enlarged view: **♡/♥**, **🔖**, **🏷**, **⤓**
download (videos), **🗑**, and **‹ ›** to step through posts. On desktop the arrow
keys do the same, `f` likes, `t` trashes, `d` downloads, `Esc` closes, the wheel
resizes the picture and double-click resets it. On a phone, swipe left/right to
move and swipe down to close.

**Picking several at once.** **☑ Select** — or holding a tile on a phone — turns
tapping into picking: tap as many as you like, then **♥ Like**, **♥ File as
liked**, **🔖 Save** or **🗑 Trash** the lot. Each view offers the way back
(**↩ Restore**, **↩ Unlike**, **↩ Unsave**), so a batch is undone the way it was
done. **Done**, **Esc** or a tap outside leaves the mode.

### Bookmarks and categories

A bookmark is the second, higher intention: the heart says *I like this*, the
bookmark says *find this again*. **🔖** saves a post from a profile grid, from the
enlarged view, or a clip in Shorts (`b`), and saving one never likes it and never
moves a file.

Gallery → **🔖 Bookmarks** shows everything you saved across profiles, newest
first, with chips for **All**, **Uncategorised** and every category you have made.
**＋ Category** adds one, **✎ Rename** renames the category being shown, and
**🗑 Delete category** deletes the category and not its contents — every bookmark
filed under it goes back to **Uncategorised**.

**🔖 on a post that is already saved opens its categories** instead of unsaving
it. They are checkboxes with a ✓ on each one it is in, and a post can be in as
many as you like — it then shows under each category as well as under **All**.
Tapping a ticked category takes it out of that one; taking it out of the last one
unsaves it, and a separate **Remove bookmark** row does that in one go. A single
tap cannot lose a bookmark any more.

Bookmarks live in the same SQLite database as everything else, so the saved state
travels with the post: one made in Shorts shows filled in its profile's grid and
in the Timeline. A bookmark whose file has gone is forgotten rather than left as
a tile that cannot open, while an unplugged drive only hides it until the drive
is back.

### Pictures, stills and deletions

- Profile pictures are saved by instaloader as part of the profile. TikTok has no
  such file, so those are fetched from the account's page and cached in
  `~/.cache/hardlywatching/avatars` — never in the profile folder, because
  anything there is scanned as gallery media.
- A profile that has not been downloaded yet has nothing to show and stays blank.
  That is the normal state for a fresh queue, not an error.
- **HEIC** photos from an iPhone are converted to JPEG for tiles and the enlarged
  view, and downloaded as the untouched original. Needs the optional
  `pillow-heif` package, which `run.py` installs.
- **Delete for good** moves the files to your desktop trash bin *and* leaves a
  0-byte placeholder in the profile folder, because instaloader treats "the file
  exists" as "already downloaded". Delete a file from your file manager instead
  and the app leaves the same placeholder on the next listing; any post marked
  this way can be un-marked in the UI so it downloads again.
- **Still videos:** many stories are seconds of one unchanging image stored as
  video. Gallery → **🖼 Still videos** scans in the background and can write a
  JPEG beside each clip (`<date>_still.jpg`) — it never changes or deletes a
  video. From the command line: `python3 tools/static_videos.py --stories-only`
  (add `--convert` to write the stills).

### Posts that mix photos and videos

Instagram carousels often mix the two, and instaloader numbers every part in one
sequence whatever its type. Those parts belong to **one** post and are shown as
one tile with a `⧉ n` badge. Files with no instaloader stamp are only grouped when
they really are parts: the unnumbered file must exist and the numbered ones must
run 2, 3, 4 with nothing missing.

---

## Shorts

**Shorts** is a tab in the header — a phone-shaped, full-screen feed of downloaded
videos. Swipe up and down or use the arrow keys. It is deliberately random with
repetition, so the same clip can come back; **more** and **less** change how often
a *profile* reappears rather than hiding anything.

- Each clip has **mute**, **like**, **bookmark**, **file**, **download**, **trash**,
  **more** and **less** — icons only, with the words as tooltips. A swipe left or
  right (or the **👁** in the topbar, or a hidden strip down the right edge on a
  desktop) shows or hides them.
- Clips start **muted**, because browsers block autoplay with sound. Turn sound on
  with the speaker button (or **m**) and the choice holds for the sitting.
- On desktop there is a **seek bar**, and **← / →** jump five seconds. Skip steps
  come in **5, 10 or 15 seconds**, starting at 5: tap either skip button three
  times to cycle them. On a phone, double-tap the left or right half of a clip to
  jump; a single tap plays and pauses.
- The topbar's **♥ Liked only** switch shows just the clips you liked. A like is a
  reaction, a bookmark is a save — they are independent.
- Opening someone in the Gallery and then tapping Shorts gives you that person
  alone; go back to the profile list and it is everyone again.

**It shows reels, not every video.** Every TikTok clip belongs in the feed, since
short-form is all TikTok is. For Instagram it shows reels only: which posts are
reels comes from `is_reel_media` in instaloader's own `.json` record for each post,
which works on an archive you already have. A video *you uploaded* has no such
record, so its shape is measured with `ffprobe` — taller than wide joins the feed —
and the verdict is cached next to the file.

An iOS limitation, told plainly: a clip that scrolls into view will not play sound
unless you have tapped that page, so if one refuses to start with sound on it falls
back to muted and the speaker button flips to say so.

`/shorts` also works on its own, which is what the installable phone app uses. Add
it to your home screen and it runs as an app launching at `/?tab=shorts`; there is
no service worker, so the installed copy still needs the server running.

---

## TikTok

TikTok profiles download through **yt-dlp** rather than instaloader. Same queue,
same one-at-a-time worker, same gallery.

Pick TikTok on the Download tab and paste any of:

- the whole **Following accounts** page (`Ctrl+A`, `Ctrl+C`) — the usernames are
  found for you. Open it with **view all** next to Following accounts and scroll
  that list to the bottom until it stops loading before you copy; only the rows on
  screen exist in the page, and the app tells you if it noticed a mismatch
- plain handles, one per line
- `@handles` and profile URLs

**How many videos to grab is adjustable per profile, at any time.** Each row has
its own `−5 −15 [n] +5 +15 +25 all` control, counting back from the newest video.
The `n` is shown against the profile's own total as `5 / 120`. **Raising it only
fetches what is new** — each profile keeps a `.yt-dlp-archive.txt`, so going from
5 to 50 fetches the 45 missing ones. A finished profile can be raised too: it
returns to the queue and picks up where it left off.

There is no Instagram login involved: yt-dlp reads a public profile itself, which
is also why a **private** TikTok profile cannot be downloaded at all. Those show
in **red** next to the queue count rather than failing quietly.

The two platforms keep **separate folders** and never merge on disk; the gallery
groups them by label, so either side can be deleted or re-fetched without touching
the other.

**Excluded.** Remove a profile from the queue — or delete it after downloading —
and it is remembered: pasting your following list again will not add it back. It
appears under **Excluded** with the reason, and **Restore** puts it back. Nothing
is deleted by excluding; it is a memory, not a blacklist.

**Saved videos.** The **Saved videos** card on the TikTok tab downloads every
video saved on your own account. TikTok does not let a downloader list that
collection — the call behind the Favorites tab only answers a signed request from
TikTok's own page, and every unsigned or re-signed attempt is refused — so the
list is read from the export TikTok makes for you instead (**Settings → Account →
Download your data**, JSON). **Find my export** looks in Downloads, Documents and
Desktop; **Check** counts the saved videos and says how many are new; **Add to
queue** puts them in as one job called **Saved videos**.

Each video is written into **its creator's own folder** (`tiktok/<handle>/`),
created if that profile is not archived yet, so it shows up in the gallery as
theirs — not as one anonymous pile. Two things stop a second export from
re-downloading the lot: the app keeps a **ledger of every saved video it has
taken** (`saved_videos` in `profiles.db`, which survives the file being moved or
renamed) and it also skips anything already on disk. Everything it takes is filed
under the bookmark category **TikTok Favorites**, so the whole collection is
browsable in one place without disturbing bookmarks you made yourself.

By default it also **adds each creator to the download queue** (newest 5 videos
each) — a saved clip belongs to somebody's profile, and one video in an otherwise
empty folder is not an archive. Handles already queued or excluded are skipped.
The switch for that sits on the Saved videos card, so it can be turned off
without turning off the saved pass.

**yt-dlp options** are in Settings, mapped to real flags: video quality rung,
container, format conversion, metadata/description/thumbnail files, embedded
metadata and cover art, request spacing, retries, rate limit, date window, cookies
from a browser, and proxy. Anything not listed there is not configurable. TikTok
only serves a few vertical renditions and yt-dlp reports height as the **long**
side, so 1080p means 1080x1920 — hence the familiar rungs of 540, 720 or 1080.

---

## Duplicate files

An Instagram joint post is served from every collaborator's profile, so the same
carousel lands in two folders. Nothing else in the app notices — the gallery is
per profile by design — so the copies sit there and the archive is quietly
larger than it needs to be.

**Options → Duplicates → Scan for duplicates** compares files by content: size
first, then a SHA-256 of every file whose size occurs more than once, which on a
large archive is a small fraction of them. The comparison is *within one
platform*, because the same picture on Instagram and TikTok is not a duplicate,
and it skips 0-byte placeholders (what a deleted post leaves behind), sidecars,
thumbnail caches and backups.

Each group lists its copies oldest first with the oldest marked **keep**. Every
other copy is pre-selected; untick anything you would rather keep, then press
**Move selected to Trash**. That moves files into the profile's own Trash
section — the same place the gallery's trash button puts them — so nothing is
deleted from disk and the gallery's Restore undoes it. Copies already in
Favourites or Trash are listed but never offered, so tidying cannot undo a
deliberate decision.

## Backup and restore

The archive is just folders: point any copy of the app at them and the gallery and
shorts rebuild from the files. What does *not* come back that way is everything the
app remembers **about** them — the queue and its order, download settings, hidden
and excluded profiles, delete tombstones, bookmarks and their categories, likes,
shorts history. That is what a backup is for.

**Options… → Backup & restore:**

- **Back up now** writes one into the app's own backups folder. It also does this
  itself once a day; the newest seven are kept.
- **Export a copy…** downloads the same thing as a zip: `state.json`,
  `profiles.db`, and a manifest. Optionally the instaloader session too, so the
  other machine does not have to spend a rate-limited login.
- **Restore from backup…** takes a zip back in, here or on another copy of the
  app (web, AppImage, DEB, flatpak — any of them).

Restoring replaces the queue, settings and people list, **not** the archive
folders: no media is moved, downloaded or deleted by a restore. The current state
is snapshotted first, so it can be undone, a mismatched archive path is rewritten
to the folder you are actually using, and the restored queue comes back paused —
nothing starts downloading until you press Start.

`gallery-cache.db` is deliberately not in the zip. It is a folder-listing cache
keyed by mtime and inode, it rebuilds itself, and copying it would only carry
stale rows.

## Settings worth knowing

All of instaloader's useful options are exposed in the UI, grouped, with a filter
box, and every tooltip shows the underlying flag. The section lives on the
**Download** tab as one collapsed line — **Download settings** — and opens in
place. Rows belonging to the *other* platform are hidden by default;
**Show all platforms** reveals them dimmed and still editable.

| Setting | Default | Why |
| --- | --- | --- |
| Seconds between profiles | 120 | Keeps a burst of logins from looking automated |
| Only new posts | on | Re-runs skip what you already have |
| Remember what's archived | on | Works even if you move the folder |
| Stories / Highlights | on | Extra requests; the most common source of errors |
| Reels | off | Adds a separate reels-only pass per profile |

---

## Accounts and sessions

Instaloader talks to Instagram through a saved login ("session"). This app manages
those for you, so you never paste a password into it. Click **Sessions** to open
the panel; it shows the platform you are on, each saved login with the account
name where there is one, whether it still works, and when it was saved.

**Grabbing a login from your browser** — the easiest way, and the one to try
first. Log in to Instagram in **Firefox, Zen or LibreWolf** as usual, then click
**Sessions** → **Auto-grab best session**. It reads the login cookie your browser
already has. If you have several, it picks the one that looks best and lists the
rest.

**Logging in with a password instead** — type your username in **Log in to
Instagram** and press **Log in**. This opens instaloader's own login in a terminal
window; your password goes to instaloader, never to this app, and 2FA codes are
entered there too.

**Using more than one account.** Everything you save stays listed:

- **Use** — makes that session the one downloads run as; anything already queued
  continues with the new account from the next profile.
- **Lock** — stops that account being used, and it stays locked across restarts.
  Locking the account in use hands downloads to another saved session. Use this
  rather than just switching away if Instagram has flagged an account for bot
  activity — it is the difference between "not right now" and "not until I say so".
- **✕** — deletes a saved session. **Refresh** — re-checks each one, so an expired
  session shows as incomplete rather than failing a download later.

**TikTok sessions** are the same panel plus **✎** to rename. TikTok's cookies
carry no user name, so a new one is named after the browser it came from
(something like "Zen (Default)"). **Save a new session** copies a browser's TikTok
login and keeps each capture as its own session instead of overwriting the last.

**Moving a session to another machine:** copy
`~/.config/instaloader/session-<username>` into the same place there. Treat it
like a password — anyone with the file can post as that account.

**Sessions expire eventually.** When one does, every job fails until you log in
again; the panel shows it as incomplete instead of silently burning requests.

---

## Where things live

| Path | What |
| --- | --- |
| `~/instagram/<username>/` | Downloaded Instagram media (changeable, see below) |
| `~/tiktok/<handle>/` | Downloaded TikTok videos, in a folder **beside** the Instagram one |
| `~/tiktok/<handle>/.yt-dlp-archive.txt` | Which TikTok videos are already downloaded |
| `~/instagram/<username>/.insta-index.json` | Hidden record of what was there, used to spot file-manager deletions |
| `~/.config/hardlywatching/state.json` | Queue, order, settings, cooldowns, deleted-post markers |
| `~/.config/hardlywatching/run/hardlywatching.lock` | The instance owning that state file: its pid, port, version and start time. A second start reads it instead of starting |
| `~/.config/instaloader/session-<user>` | Instagram session, created by instaloader |
| `~/.config/instaloader/latest-stamps.ini` | What has already been archived |
| `.venv/` (next to `run.py`) | The private environment `run.py` creates |

Override paths with `INSTA_OUTPUT` and `INSTA_STATE`; also honours `INSTA_PORT`
and `INSTA_HOST`.
`INSTA_LOCK` sets where the lock file itself lives.

**Choosing the downloads folder.** Click **Change…** on the Downloads folder card
in Settings and give a full path, e.g. `/mnt/bigdrive/instagram`. Leave *Also move
my existing archives into the new folder* unticked to send only *new* downloads to
the new location — old files stay where they are and still show in the gallery,
because the old folder stays in the archive list. Tick it to move every profile
folder across in one go; it refuses if a folder with the same name exists there,
or while a download is running. Because `--latest-stamps` lives outside the media
folder, moving or deleting the whole tree never causes re-downloads.

**Telling the two apart.** Instagram sits directly in the downloads folder
(`<downloads>/<username>/`), TikTok in a folder *beside* it
(`<downloads>/../tiktok/<handle>/`), so a handle on both platforms gets two folders
rather than one shared pile. If the downloads folder *is* a drive's mount point,
the sibling would land on another disk, so TikTok goes inside the downloads folder
instead. That sibling is only used when it looks like a TikTok archive (a
`.hardlywatching-tiktok` marker, or a child folder holding a video or a
`.yt-dlp-archive.txt`), so an unrelated directory called `tiktok` is never adopted.

**Pacing.** Each platform waits its own gap between profiles — Instagram's is the
long one, TikTok's is short because yt-dlp only reads a public page. The gap shows
as a live countdown, and **pressing Start cuts it short** for that platform.

**Disk space.** Free space for every archive folder is shown in the header, and
the app watches it so downloads cannot fill a drive by surprise:

| Used | What happens |
| --- | --- |
| under 90% | nothing, no notice |
| 90% | the chip turns amber and a banner appears. Downloads continue |
| 95% | downloads stop. Nothing new starts, and a run that crosses the limit is halted mid-way |

Stopping is deliberately *not* a failure: a profile that could not start goes back
to the front of its queue, is **not** marked failed, and starts by itself once
space is freed. A run halted part-way keeps the videos it already finished. The
rule is enforced as a reserve checked *before* a job starts — at least 5% of the
volume (or 2 GB, whichever is larger) free — and each archive folder is judged
against its own drive.

---

## Honest limitations

- **Big archives are slow by design.** Days, not hours — the rate-limit trade
  above, not a bug.
- **Instagram fights this.** Even logged in, large profiles can hit limits partway
  through. The queue pauses and resumes; "everything in one sitting" is not
  guaranteed.
- **Sessions expire eventually.** Then every job fails until you log in again.
- **Stories and highlights are the flakiest part.** Instagram sometimes returns
  malformed data there and crashes instaloader; the job is marked done with a note
  instead of re-downloading everything.
- **Private profiles** only work if your account follows them. A profile that is
  private and you *don't* follow cannot be downloaded by anyone, by any method —
  the content is not visible to your account, so there is nothing to fetch. One
  that flips from visible to private after a block or unfollow shows up as a
  profile that used to download and now does not; that is not a bug, and re-adding
  it will not help.
- **No proxy for Instagram.** Per instaloader's own docs, cloud and VPN IPs tend to
  get *stricter* limits, not looser. TikTok does have an optional proxy setting.
- **No authentication in the UI.** Treat it as a local tool.
- **The GTK control panel** needs PySide6 or PyGObject; without either you get a
  terminal prompt offering the same functions.

---

## Legal

Use this for content you have the right to download. Respect Instagram's Terms of
Service and the rights of the people whose content you are archiving.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for what changed between versions.

## Licence

HardlyWatching is free software: you can redistribute it and/or modify it under
the terms of the **GNU General Public License, version 3 or later**.

It is distributed in the hope that it will be useful, but **without any
warranty**; without even the implied warranty of merchantability or fitness for
a particular purpose. See [LICENSE](LICENSE) for the full text.

```
Copyright (C) 2026 hardlysleeping
```

Full licence: <https://www.gnu.org/licenses/gpl-3.0.html>

This project bundles no instaloader or yt-dlp code: both are installed
separately and keep their own licences (instaloader is MIT; check yt-dlp's
licence with its own distribution). GPL-3.0 applies to this project's own
code, and it is not imported from either tool.

## Support

[![Donate with PayPal](https://www.paypalobjects.com/en_US/i/btn/btn_donateCC_LG.gif)](https://www.paypal.com/donate/?hosted_button_id=RNT5W4XNC64EY)
