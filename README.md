# Instagram Downloader

A small local web UI for bulk-archiving Instagram profiles with
[instaloader](https://instaloader.github.io/). Paste a list of profiles, press
start, and watch progress in the browser. One profile downloads at a time, with
pacing and automatic back-off so you don't get rate-limited.

Everything runs on your own machine. Nothing is sent anywhere except to
Instagram, by instaloader, using your own logged-in session.

## What it does

- **Queue-based.** Paste many profiles; they download one at a time.
- **Survives restarts.** The queue, its order, and any cooldown are saved to
  disk. Reboot and it picks up where it left off.
- **Rate-limit aware.** Any HTTP 429 stops the whole queue and waits, because
  retrying harder is what gets you blocked. Instagram's own "retry in N seconds"
  is respected when it tells us.
- **Skips what you already have.** Re-running a profile only fetches new posts.
- **Remembers independently of the files.** Move or delete an archive folder and
  re-runs still know what was already archived (`--latest-stamps`).
- **Deletions stick.** Trash a post, then delete it for good — it goes to your
  desktop trash bin, and a hidden marker stops instaloader fetching it again.
  Delete something straight from your file manager and the app notices on the
  next look and marks it too. Un-delete any of them from the UI to get it back.
- **Favourites and trash across every profile.** Star a post to keep it handy;
  trash one to set it aside. Both have their own view over the whole archive,
  not just one profile.
- **Recoverable removals.** Removing a profile never loses it — restore it from
  the Removed list.
- **Reels as a separate pass.** Optionally queue reels as their own follow-up
  job instead of mixing them into the main download.
- **Browser session import.** Pick up the Instagram login already in Firefox or
  Zen instead of typing your password.
- **Autostart.** Optional systemd user service so it's always running.

> **Platform:** the core runs anywhere Python 3.10+ does — **Linux, macOS,
> Windows** — with `python3 server.py` (or `run.py`). The extra conveniences
> are Linux-only for now: the launcher icon, the autostart service, and the
> GTK control-panel window. On macOS/Windows, "send to trash" recovery still
> works (Finder Trash / Recycle Bin) and folder-opening uses the native file
> manager.

## Running it on macOS or Windows

The web UI and downloader are plain Python — they run on any OS. You just
don't get the Linux desktop extras.

```bash
# any OS, from the cloned folder
python3 run.py            # makes its own venv, installs instaloader, starts
```

Then open the address it prints in your browser. No launcher icon, no
autostart service, no control-panel window — those are Linux-only. Everything
in the web UI (queue, gallery, favourites, trash, folder choice) works the
same.

To reach it from another device, set `INSTA_HOST=0.0.0.0` (on Windows,
`set INSTA_HOST=0.0.0.0` before the command).

## Requirements

- **Python 3.10+** (the app itself is standard-library only — no
  `pip install` needed).
- **[instaloader](https://instaloader.github.io/)** — the actual downloader.
  The launcher offers to install it for you; or do it yourself:
  ```
  pipx install instaloader     # recommended
  # or: pip install --user instaloader
  ```
- An **Instagram account** you can log into. Most profile content requires a
  session; anonymous requests get blocked almost immediately.
- Optional: Firefox, Zen or LibreWolf if you want the browser-cookie import.

## Quick start

The easiest way — one command, no setup:

```bash
git clone https://github.com/hardlysleeping/insta-desktop.git
cd insta-desktop
./start.sh
```

That checks everything, starts the app, and opens it in your browser. If the
default port (8765) is taken it picks a free one automatically and tells you
which. If instaloader is missing it says so plainly and offers to install it
for you.

Prefer something you can click? Add a launcher icon:

```bash
./install-desktop-icon.sh
```

Then start it any time from your applications menu or desktop.

Want a small window that shows whether it's running and lets you start, stop,
restart, copy or open the link?

```bash
python3 control-panel.py
```

Closing that window does **not** stop the app — it keeps running in the
background. Only **Stop** (or Ctrl-C) stops it.

Other ways to start it:

```bash
python3 server.py         # plain, if you already have instaloader
python3 run.py            # fully self-contained: makes its own venv + installs
                          # instaloader into it, nothing else needed
python3 run.py --service  # set it up to start automatically (see below)
```

`run.py` is the most foolproof: it sets up its own private environment next to
itself (`.venv/`), installs instaloader into it if needed, then starts the app.
Nothing is installed system-wide.

Then, in the browser:

1. **Log in.** Click **Sessions** → either
   - **Auto-grab best session** (uses the Instagram login already in your
     browser — no password needed), or
   - type your username and click **Log in**, which opens a terminal to run
     instaloader's login (so your password is never handled by this app).
2. **Paste profiles** — one per line. Usernames, `@handles` and full
   `instagram.com` URLs all work.
3. **Add to queue**, then **Start**.
4. Watch the queue. Downloading goes to `~/instagram/<username>/` in
   instaloader's normal layout.

## Install as a service (autostart)

```bash
./install-service.sh          # install + start, enabled at login
./install-service.sh remove   # stop + uninstall
```

Manage it with:

```bash
systemctl --user status insta-downloader
journalctl --user -u insta-downloader -f
```

## The gallery

Every finished profile gets a browsable grid, plus two archive-wide views:

- **Profiles** — pick a profile, see its posts as tiles. Videos start muted;
  unmute from the player controls.
- **♥ All Favourites** — every post you starred, across every profile, in one
  grid, each tile labelled with whose post it is.
- **🗑 All Trash** — everything you set aside, with a button to delete it all.

On each tile: **♥** favourites a post, **🗑** sends it to trash. The trash
button sits in the bottom-right corner on its own, away from the favourite
button, so it is hard to hit by accident on a phone.

### Deleting for good

"Delete all trash" does two things, both deliberately recoverable:

1. It moves the files to your **desktop trash bin** (`~/.local/share/Trash`),
   so you can still restore them from your file manager or empty the bin
   yourself when you're sure.
2. It leaves a **0-byte placeholder** with the same filename in the profile
   folder. instaloader decides a post is already downloaded purely by whether
   that file exists, so the placeholder stops it fetching the post again.

Delete a file straight from your file manager instead, and the app notices the
next time it lists that profile: it leaves the same kind of placeholder, so the
post still won't come back. Any post you marked this way can be un-marked from
the UI, which removes the placeholder so it downloads again on the next run.

## Settings

All of instaloader's useful options are exposed in the UI, grouped, with a
filter box. Every field's tooltip shows the underlying instaloader flag.

The ones worth knowing:

| Setting | Default | Why |
| --- | --- | --- |
| Seconds between profiles | 240 | Spacing between full-profile runs. Longer is safer. |
| Only new posts | on | Re-runs skip what's already archived. |
| Remember what's archived | on | Keeps tracking even if you move the folder to another drive. |
| Stories / Highlights | on | Extra requests per profile; the most common source of errors. |
| Reels | off | Queues a separate reels-only job after the main download. |

Rate-limit behaviour is deliberately conservative: a 429 pauses the whole queue
for 15 minutes (or longer if Instagram says so) rather than retrying
immediately. This is the single most important thing for keeping an account
unblocked.

## Where things live

| Path | What |
| --- | --- |
| `~/instagram/<username>/` | Downloaded media (default — changeable, see below) |
| `~/instagram/<username>/.insta-index.json` | Hidden record of what was there, used to spot file-manager deletions |
| `~/.config/insta-desktop/state.json` | Queue, order, settings, cooldowns, deleted-post markers |
| `~/.config/instaloader/session-<user>` | Instagram session (created by instaloader) |
| `~/.config/instaloader/latest-stamps.ini` | "What have I already archived" record |

Override the first two with `INSTA_OUTPUT` and `INSTA_STATE`. Also honours
`INSTA_PORT` and `INSTA_HOST`.

### Choosing the downloads folder

In the app, click **Change folder…** (shown under "Nothing queued yet", or any
time from the empty state). Give a full path, e.g. `/mnt/bigdrive/instagram`.

- Leave **Move my existing archives across too** unticked to just send *new*
  downloads to the new location. Your old files stay where they are.
- Tick it to **move every existing profile folder** into the new location in one
  go, so everything lives together. It refuses if a folder with the same name
  already exists there, and refuses while a download is running.

Your choice is saved and survives restarts. And because instaloader's
`--latest-stamps` record lives outside the media folder, moving or even
deleting the whole tree never causes re-downloads — it still knows what you
already have.

## Reaching it from your phone or another device

By default the server binds `127.0.0.1`, which means only this machine can
reach it. Set `INSTA_HOST=0.0.0.0` to listen on every interface:

```bash
INSTA_HOST=0.0.0.0 python3 server.py
```

Then open `http://<this-machine's-ip>:8765` — e.g. `http://100.x.y.z:8765` over
Tailscale, or `http://192.168.1.50:8765` on the same Wi-Fi. Local
`http://127.0.0.1:8765` keeps working.

For the systemd user service, add a drop-in:

```bash
systemctl --user edit insta-downloader
```

```ini
[Service]
Environment=INSTA_HOST=0.0.0.0
```

then `systemctl --user restart insta-downloader` and open the port in your
firewall.

**There is no authentication of any kind.** The UI can add, remove and requeue
jobs and start Instagram logins. Bind it to `0.0.0.0` only on a network you
trust (Tailscale, your own LAN), and don't port-forward it to the internet.

Note the browser-login endpoint opens a terminal on the *server* machine, so
session login only makes sense from the machine the service runs on.

## Honest limitations

- **Instagram fights this.** Even logged in, large profiles can hit rate limits
  partway through. The queue resumes automatically, but "everything in one
  sitting" isn't guaranteed — spread big jobs over days.
- **Don't scrape with an account you care about.** IP-level rate limits are
  temporary, but patterns that look automated can lead to an account
  checkpoint. A throwaway account for bulk work is wise.
- **Sessions expire eventually.** When that happens every job fails until you
  log in again. The UI shows the problem rather than silently burning requests.
- **Stories and highlights are the flakiest part.** Instagram sometimes returns
  malformed responses for them, which crashes instaloader; the job is then
  marked done with a note rather than re-downloading everything.
- **Private profiles** only work if your account follows them.
- **No proxy support.** Proxying through cloud/VPS IPs tends to make rate
  limiting *worse* for anonymous access, per instaloader's own docs.

## Legal

Use this for content you have the right to download. Respect Instagram's Terms
of Service and the rights of the people whose content you are archiving.

## Licence

MIT — see [LICENSE](LICENSE).
