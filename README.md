# Instagram Downloader

> ### This is a web UI for [instaloader](https://instaloader.github.io/) — it has no downloader of its own.
>
> <img src="docs/instaloader-logo.png" width="64" height="64" alt="instaloader logo" align="left" hspace="12" vspace="4">
>
> Every request to Instagram, and every file on disk, is made and written by
> **[instaloader](https://github.com/instaloader/instaloader)** —
> a separate, mature command-line tool
> ([docs](https://instaloader.github.io/) · [PyPI](https://pypi.org/project/instaloader/) · [source](https://github.com/instaloader/instaloader)).
> This repository adds a browser interface on top: a persistent queue, pacing, and
> a gallery. It reimplements none of it and never talks to Instagram itself — it
> builds instaloader's command line and runs it.
>
> **Want to install a downloader? Install instaloader.** If you have it and only
> want it, you don't need this project at all.
<br clear="left">

**What this adds:** paste 200 profiles once and let them drain over days, survive
reboots, and browse what you collected.

---

## Screenshots

**Queue** — paste profiles, add them, start. They download one at a time, with the
waiting list and finished list side by side. Both page and search, so a queue of
hundreds is still easy to navigate.

![The queue screen: eight profiles waiting, one finished, with the page and search controls](docs/queue.png)

**Gallery** — every profile you have archived, marked while it is downloading, plus
archive-wide views and a box to add your own photos or folders.

![The gallery: profile cards each marked downloading, with All Favourites, All Trash and Timeline](docs/gallery.png)

**One profile** — its posts as a grid, filterable by media type and sorted however
you like.

![A single profile's post grid with media filters and sort options](docs/gallery-profiles.png)

**Timeline** — every post from every profile in one chronological stream, grouped
by month. The slider jumps between months; the two selectors pick a year and month
directly. Filter to just favourites if you want.

![The timeline: year and month selectors, a month slider, and a row of post tiles](docs/timeline.png)

**Enlarged view** — click any post. Step through posts with the arrows (or the
arrow keys), favourite, download a video, or trash it.

![A post enlarged over the grid, with previous and next arrows and action buttons](docs/viewer.png)

**Sessions** — every saved Instagram login in one list, with the one in use marked.
Switch accounts with one click, or grab a login out of your browser so you never
type a password here.

![The sessions panel: three saved sessions with Use buttons, one marked in use, plus grab-from-browser and log-in options](docs/sessions-panel.png)

---

## Install and run

You need **Python 3.10+** and an **Instagram account**. You do **not** need to
install instaloader yourself — `run.py` does it for you (see below).

```bash
git clone https://github.com/hardlysleeping/insta-desktop.git
cd insta-desktop
python3 run.py
```

That is the whole install. `run.py` creates a private Python environment in
`.venv/` next to itself, installs instaloader into it, starts the app and opens
your browser. Nothing touches the rest of your system and nothing needs `sudo`.
If instaloader is already installed it skips straight to starting.

First run takes ~20 seconds, mostly pip. After that it starts instantly.

Then:

1. **Log in** — click **Sessions**. See [Accounts and sessions](#accounts-and-sessions)
   below; the short version is one click to grab the login already in your browser.
2. **Paste profiles** — one per line. `username`, `@username` and full
   `instagram.com` URLs all work.
3. **Add to queue**, press **Start**, and leave it running.

Downloads land in `~/instagram/<username>/` in instaloader's normal layout, so
instaloader itself can still read the folder.

### Other ways to start it

```bash
cd insta-desktop             # enter folder location
python3 run.py --service     # also set it up to start at login (Linux)
python3 run.py --no-install  # don't install instaloader, just run
python3 run.py --port 9000   # different port
./start.sh                   # if you already have instaloader on PATH
python3 server.py            # plain, standard library only
```

**Platforms:** the app is standard-library Python and runs on Linux, macOS and
Windows. Linux gets the extras (autostart service, desktop icon, GTK control
panel). On macOS/Windows everything in the web UI works the same, and
delete-for-good still goes to Trash / Recycle Bin.

Optional Linux extras:

```bash
./install-desktop-icon.sh     # launcher icon in your applications menu
python3 control-panel.py      # small window: start / stop / restart / copy link
```

The control panel needs PyGObject. Without it you get a plain terminal prompt
with the same commands (`start`, `stop`, `restart`, `status`, `open`, `quit`).
Closing the panel does **not** stop the app.

### Reach it from your phone

It listens on `127.0.0.1` by default. To use it from another device on your own
network or over Tailscale:

```bash
INSTA_HOST=0.0.0.0 python3 run.py
```

Then open `http://<machine-ip>:8765` — e.g. `http://100.x.y.z:8765` over
Tailscale, or `http://192.168.1.50:8765` on the same wifi. On Windows, `set
INSTA_HOST=0.0.0.0` first. For the systemd service, `systemctl --user edit
insta-downloader` and add `Environment=INSTA_HOST=0.0.0.0` under `[Service]`.

**There is no authentication** — the UI can start logins and delete files. Only
do this on a network you trust, never port-forwarded to the internet. The
browser-login button opens a terminal on the *server* machine, so login only
makes sense from the machine it runs on.

### Autostart (Linux)

```bash
./install-service.sh          # install + start, enabled at login
./install-service.sh remove   # stop + uninstall
systemctl --user status insta-downloader
journalctl --user -u insta-downloader -f
```

---

## How it works

Two pieces:

- **instaloader** makes every request to Instagram and writes the files. It is
  the only thing that talks to Instagram.
- **This app** is a queue and a viewer. It decides which profile runs next, runs
  instaloader for it, reads instaloader's output to show progress, and serves the
  gallery from the files on disk.

A job is one profile. Jobs run **one at a time**. State lives in
`~/.config/insta-desktop/state.json`, so closing the browser, restarting the app
or rebooting all keep the queue, its order and any cooldown. Only a profile that
was mid-download restarts from the beginning.

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

It also helps to not use an account you care about. A throwaway account for bulk
archiving is wise.

---

## What it is actually good for

- **Paste 200 profiles once and forget it.** They drain over days, and reboots or
  crashes don't lose the queue.
- **Know what happened.** Every job shows its status, how much it got, and the
  raw instaloader output when something failed.
- **Skip the re-downloads.** Re-run anything; only new posts come down.
- **Browse the whole archive.** A grid per profile, plus All Favourites and All
  Trash across every profile, and a merged timeline of every post you have,
  grouped by month with a slider.
- **Works on a phone.** Swipe through favourites full screen, tap to enlarge; on
  desktop the arrow keys walk post to post.
- **Multiple drives.** Keep old archives on one disk and send new downloads to
  another. Unplug a drive and its profiles drop out of the gallery until it is
  back; nothing is moved or copied without asking.
- **Your own photos too.** Any folder in an archive location appears as a
  profile, so non-Instagram pictures and phone uploads browse in the same grid.
- **Delete safely.** Trashing moves files to your desktop Trash and leaves a
  marker so instaloader doesn't fetch them again. Everything is recoverable.
- **Recoverable removals.** Removing a profile never loses it — restore it from
  the Removed list.
- **Reels as a separate pass.** Optionally queue reels as their own follow-up job
  instead of mixing them into the main download.

---

## The gallery

- **Profiles** — pick a person, see their posts as tiles. Videos play on hover.
- **♥ All Favourites** — everything you starred, across every profile, with a
  full-screen swipe view.
- **🗑 All Trash** — everything you set aside, with delete-for-good.
- **🕒 Timeline** — every post from every profile in one chronological stream,
  grouped by month, with a favourites-only filter.

On a tile: **♥** favourite, **⤢** open bigger, **🗑** trash. In the enlarged view:
**♡/♥**, **⤓** download (videos), **🗑**, and **‹ ›** to step through posts. On
desktop the arrow keys do the same, `f` favourites, `t` trashes, `d` downloads,
`Esc` closes. On a phone, swipe left/right to move and swipe down to close.

### Deleting for good

"Delete all trash" does two things, both deliberately recoverable:

1. It moves the files to your **desktop trash bin** (`~/.local/share/Trash`), so
   you can restore them from your file manager or empty the bin when you're sure.
2. It leaves a **0-byte placeholder** with the same filename in the profile
   folder. instaloader decides a post is already downloaded purely by whether
   that file exists, so the placeholder stops it fetching the post again.

Delete a file straight from your file manager instead, and the app notices the
next time it lists that profile and leaves the same kind of placeholder. Any post
marked this way can be un-marked in the UI, which removes the placeholder so it
downloads again on the next run.

---

## Settings worth knowing

All of instaloader's useful options are exposed in the UI, grouped, with a filter
box. Every field's tooltip shows the underlying instaloader flag.

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
those for you, so you never paste a password into it.

Click **Sessions** to open the panel. Every saved login is listed with the account
name, whether it still works, and when it was saved. The one currently in use is
marked **in use**.

### Grabbing a login from your browser

The easiest way, and the one to try first:

1. Log in to Instagram **in Firefox, Zen or LibreWolf** as usual.
2. Click **Sessions** → **Auto-grab best session**.

It reads the login cookie your browser already has and saves it for instaloader.
No password, no terminal, nothing typed into this app. If you have several, it
picks the one that looks best and lists the rest so you can grab those too.

### Logging in with a password instead

If you don't keep an Instagram login in a browser, type your username in the
**Log in to Instagram** box and press **Log in**. This opens instaloader's own
login in a terminal window — your password goes to instaloader, never to this app.
Two-factor codes are entered there too.

### Using more than one account

Everything you save stays listed, so you can keep several accounts on hand and
switch whenever you like:

- **Use** — makes that session the one downloads run as. Anything already queued
  continues with the new account from the next profile.
- **✕** — deletes a saved session. Nothing else is touched.
- **Refresh** — re-checks each session, so one that expired shows as incomplete
  rather than failing a download later.

Useful when one account gets rate-limited: switch to another and keep going, while
the first one recovers.

### Moving a session to another machine

A session is just a file in instaloader's config folder:

```
~/.config/instaloader/session-<username>
```

Copy that file into the same place on another computer and it appears in the panel
as a saved session, ready to use. Treat it like a password — anyone with the file
can post as that account.

**Sessions expire eventually.** When one does, every job fails until you log in
again. The panel shows it as incomplete instead of silently burning requests.

## Where things live

| Path | What |
| --- | --- |
| `~/instagram/<username>/` | Downloaded media (changeable, see below) |
| `~/instagram/<username>/.insta-index.json` | Hidden record of what was there, used to spot file-manager deletions |
| `~/.config/insta-desktop/state.json` | Queue, order, settings, cooldowns, deleted-post markers |
| `~/.config/instaloader/session-<user>` | Instagram session, created by instaloader |
| `~/.config/instaloader/latest-stamps.ini` | What has already been archived |
| `.venv/` (next to `run.py`) | The private environment `run.py` creates |

Override paths with `INSTA_OUTPUT` and `INSTA_STATE`; also honours `INSTA_PORT`
and `INSTA_HOST`.

### Choosing the downloads folder

In the app, click **Change…** on the Downloads folder card in Settings. Give a
full path, e.g. `/mnt/bigdrive/instagram`.

- Leave **Also move my existing archives into the new folder** unticked to send
  only *new* downloads to the new location. Your old files stay where they are
  and still show in the gallery, because the old folder stays in the archive
  list.
- Tick it to move every existing profile folder across in one go. It refuses if a
  folder with the same name already exists there, or while a download is running.

Because instaloader's `--latest-stamps` record lives outside the media folder,
moving or even deleting the whole tree never causes re-downloads — it still knows
what you already have.

---

## Honest limitations

- **Big archives are slow by design.** Days, not hours — the rate-limit trade
  above, not a bug.
- **Instagram fights this.** Even logged in, large profiles can hit limits
  partway through. The queue pauses and resumes; "everything in one sitting" is
  not guaranteed.
- **Sessions expire eventually.** Then every job fails until you log in again.
  The UI reports it rather than silently burning requests.
- **Stories and highlights are the flakiest part.** Instagram sometimes returns
  malformed data there and crashes instaloader; the job is marked done with a
  note instead of re-downloading everything.
- **Private profiles** only work if your account follows them.
- **No proxy support.** Per instaloader's own docs, cloud and VPN IPs tend to get
  *stricter* limits, not looser.
- **No authentication in the UI.** Treat it as a local tool.
- **The GTK control panel** needs PyGObject; without it you get a terminal prompt
  offering the same functions.

---

## Legal

Use this for content you have the right to download. Respect Instagram's Terms of
Service and the rights of the people whose content you are archiving.

## Licence

MIT — see [LICENSE](LICENSE). This project bundles no instaloader code;
instaloader is installed separately under its own licence (GPL-3.0).
