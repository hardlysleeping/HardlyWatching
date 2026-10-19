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
- **Recoverable removals.** Removing a profile never loses it — restore it from
  the Removed list.
- **Reels as a separate pass.** Optionally queue reels as their own follow-up
  job instead of mixing them into the main download.
- **Browser session import.** Pick up the Instagram login already in Firefox or
  Zen instead of typing your password.
- **Autostart.** Optional systemd user service so it's always running.

## Requirements

- **Linux** with **Python 3.10+** (the app itself is standard-library only — no
  `pip install` needed).
- **[instaloader](https://instaloader.github.io/)** — the actual downloader.
  ```
  pipx install instaloader     # recommended
  # or: pip install --user instaloader
  ```
- An **Instagram account** you can log into. Most profile content requires a
  session; anonymous requests get blocked almost immediately.
- Optional: Firefox, Zen or LibreWolf if you want the browser-cookie import.

## Quick start

```bash
git clone https://github.com/YOURNAME/insta-desktop.git
cd insta-desktop
python3 server.py
```

It opens `http://127.0.0.1:8765` in your browser.

Then:

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
| `~/instagram/<username>/` | Downloaded media, in instaloader's layout |
| `~/.config/insta-desktop/state.json` | Queue, order, settings, cooldowns |
| `~/.config/instaloader/session-<user>` | Instagram session (created by instaloader) |
| `~/.config/instaloader/latest-stamps.ini` | "What have I already archived" record |

Override the first two with `INSTA_OUTPUT` and `INSTA_STATE`. Also honours
`INSTA_PORT`.

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
