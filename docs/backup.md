# Backup and restore

The archive folders are the files. This is the *other* half: the queue, the
download settings, the hidden and excluded lists, bookmarks, likes, and the list
of people you imported — including people who were queued but never downloaded,
which no amount of rescanning can bring back.

Settings → **Backup & restore** does it, or the same four routes from anything
that speaks HTTP.

## What is in the zip

| Member            | What it is                                              |
| ----------------- | ------------------------------------------------------- |
| `manifest.json`   | format version, app version, date, counts, archive root |
| `state.json`      | queue, settings, roots, tombstones, pause/cooldown      |
| `profiles.db`     | people, bookmarks, categories, likes, shorts history    |
| `instaloader/…`   | optional, only when "include Instagram login" is on      |

Left out on purpose: `gallery-cache.db`, thumbnails and avatars. They are keyed
by file mtime/size/inode, so they are worthless on another machine and rebuild
themselves on first scan. Not included either: any of your media. The archive
is not copied into a backup.

`profiles.db` is copied through SQLite's own backup API, not by reading the file
— a live `-wal` copy can be missing the last transactions.

## Exporting

Settings → Backup & restore → **Back up now** writes one dated zip in
`<app data>/backups/` (`hw-backup-YYYYmmdd-HHMMSS.zip`, 7 kept).
**Export a copy…** downloads the same thing with a
`hardlywatching-backup-YYYY-MM-DD.zip` name, for a USB stick or another machine.
The login is included only when the checkbox is on; it is a credential, so the
default in `Export` is your choice and the daily automatic backup takes it so
that a new machine does not have to log in again (Instagram rate-limits logins).

One automatic backup is written per day on startup, in the background; nothing
waits for it. It is skipped when the last one is younger than 24 hours.

## Restoring

**Restore from backup…** takes a zip, validates the manifest, replaces
`state.json` / `profiles.db` in *this* machine's app data, and restarts the
engine in-process — no restart of the app, no restart of the download workers.

* The archive root recorded in the backup is kept when that folder still exists;
  otherwise the zip's root is remapped to the folder this app already uses, and
  the response says `root_changed: true`.
* The state that is about to be replaced is exported to
  `backups/hw-prerestore-<stamp>.zip` first. A wrong zip is undone by restoring
  that one.
* A restore is **refused while anything is queued or running** (HTTP 409): the
  workers write `state.json` as they go, so the swap would be undone or the
  queue corrupted. Clear the queue first.
* After a restore, downloads do not start by themselves: the jobs land paused
  in the queue.

## Endpoints

| Route                        | Purpose                                        |
| ---------------------------- | ---------------------------------------------- |
| `GET  /api/backup/info`      | paths, sizes, portable mode, backup list       |
| `GET  /api/backup/export`    | download a fresh zip (`?session=1` adds login) |
| `GET  /api/backup/download`  | download one saved backup (`?name=…`)          |
| `POST /api/backup/import`    | raw zip body; `?root=…` remaps the archive     |
| `POST /api/backup/auto`      | write one now (`{"keep": 7, "session": true}`) |

There is no authentication on the API: these routes are exactly as exposed as
the rest of the app, which must stay on loopback or the tailnet. Do not publish
it. `import` writes app state, so it is the one route worth guarding first when
remote access arrives.

## One folder for everything (optional)

By default the files live in the platform convention
(`~/.config/hardlywatching/` on Linux, `%APPDATA%` on Windows,
`~/Library/Application Support` on macOS). Setting `INSTA_DATA=/some/folder`
before launch puts the state, the database, the caches, the thumbnails, the
avatars and the Instaloader session under that one folder instead:

```
$INSTA_DATA/state.json
$INSTA_DATA/profiles.db
$INSTA_DATA/gallery-cache.db
$INSTA_DATA/thumbs/
$INSTA_DATA/avatars/
$INSTA_DATA/instaloader/
$INSTA_DATA/backups/
```

Unset, nothing changes. Explicit per-file variables (`INSTA_STATE`,
`INSTA_PROFILES_DB`, `INSTA_CACHE_DB`, `INSTA_THUMBS`, `INSTA_AVATARS`) still win
over the folder, so an existing customised setup keeps working.

`INSTA_DATA` is the app's memory, not the media. Files still go to the downloads
folder and are read from the archive folders, which can be anywhere.
