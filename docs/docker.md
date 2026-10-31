# HardlyWatching in Docker

The container runs the same server the desktop builds bundle: a web UI on
`:8765` plus the queue behind it. There is no desktop window — you use it from
your browser, from any machine that can reach the port.

```bash
docker run -d --name hardlywatching \
  -p 127.0.0.1:8765:8765 \
  -v "$PWD/data:/data:z" \
  -v "$PWD/archive:/archive:z" \
  --restart unless-stopped \
  ghcr.io/hardlysleeping/hardlywatching:latest
```

Open <http://127.0.0.1:8765>. The first screen is the queue; **Gallery** is the
archive you have collected.

## The two volumes

| path | what lives there |
|---|---|
| `/data` | `state.json` (the queue), `profiles.db`, `gallery-cache.db`, thumbnails, avatars, the instaloader session, `latest-stamps.ini` |
| `/archive` | the media itself — one folder per profile, with `favorites/` and `trash/` inside each |

Nothing else persists. `INSTA_DATA=/data` and `INSTA_OUTPUT=/archive` are baked
into the image, which is why the whole app moves with those two folders: copy
them to another machine, point a new container at them, and the queue, the
sessions and the gallery are all there.

**`:z` is not optional on SELinux hosts** (Fedora, Bazzite, RHEL, Silverblue).
Without it the container runs as UID 1000, the mount looks correctly owned, and
every write still fails with `Permission denied` — the gallery comes back empty
and `sqlite3.OperationalError: unable to open database file` is in the log. The
label makes the directory usable from inside the container.

## Ports and reachability

The image sets `INSTA_HOST=0.0.0.0` (it has to, to be reachable through the
published port) and `INSTA_LAN=1`, because in a container there is no loopback
client — every request arrives from the docker bridge, which the app treats as
remote and refuses unless "reachable" is on.

Publish the port to `127.0.0.1` unless you have put something in front of it.
**The app has no authentication:** anyone who can reach the port can start
Instagram logins and delete files.

```bash
# from your phone on the same network, with a reverse proxy and a password:
-p 0.0.0.0:8765:8765
```

## Permissions

The image runs as UID/GID 1000 (`hw`). On a host where you are UID 1000 — the
first user on most desktops — bind mounts work as-is. If you are not, the
archive gets written as the wrong user, so pass your own:

```bash
docker run --user "$(id -u):$(id -g)" ...
```

The media folders you already own keep their owner either way, because the app
only ever writes inside `/archive` and `/data`.

## docker compose

```yaml
services:
  hardlywatching:
    image: ghcr.io/hardlysleeping/hardlywatching:latest
    ports:
      - "127.0.0.1:8765:8765"
    volumes:
      - ./data:/data:z
      - ./archive:/archive:z
    environment:
      - TZ=America/Los_Angeles
    restart: unless-stopped
```

A copy of this is in the repository root as `docker-compose.yml`:

```bash
docker compose up -d
docker compose logs -f      # watch a download
```

## What is in the image

- `python:3.12-slim`, the app copied in at `/app`, run as `python3 server.py`
- `ffmpeg` from Debian (thumbnails, TikTok and reel downloads, hover previews)
- `instaloader` and `yt-dlp` from PyPI — the two tools that actually download
- `pillow` + `pillow-heif` (HEIC thumbnails), `numpy` + `onnxruntime` (the
  "who is in this picture" pass)

About 1.1 GB on disk (measured: 1,131,732,934 bytes). The optional extras are
the features, not decoration:
without `pillow-heif` HEIC thumbnails are skipped, and without `onnxruntime`
the people filter reports itself off.

## Updating

```bash
docker pull ghcr.io/hardlysleeping/hardlywatching:latest
docker rm -f hardlywatching && docker run ...   # or: docker compose up -d
```

The archive and the queue are in the volumes, so replacing the container loses
nothing. Pinning a version instead of `:latest` is the safer habit — the tags
match the releases, e.g. `:0.5.1`.

## Limits worth knowing

- **No desktop shell.** The window, the tray icon and the desktop-entry
  integration are the Flatpak/DEB/AppImage builds, not this.
- **No login for Instagram in a browser.** `instaloader` needs a session; the
  app imports one from a cookies file or your browser's profile, both of which
  are paths on the *container's* filesystem. Mount the file and point Settings
  at it, or import the session on a desktop install and copy `data/instaloader/`
  into the container's `/data`.
- **The port is the whole security model.** There is no user account, no
  password and no per-request authentication.
