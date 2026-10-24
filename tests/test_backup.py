"""Backup and restore: the two files that are not the archive.

What these tests are actually about: an archive can move between machines by
being copied, because the gallery and shorts feed rebuild themselves from the
folders. What cannot rebuild itself is the app's *memory* -- the queue, the
settings, hidden items, bookmarks, likes, the imported profile list. So the zip
has to carry those two files, and the restore has to put them where the app on
*this* machine expects them while pointing them at *this* machine's archive.

Two servers are involved on purpose: the backup is taken from one sandbox and
restored into another whose archive folder has a different path, which is the
"same folders, new computer, different mount point" case.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path

try:  # imported as `tests.test_backup`
    from . import sandbox
except ImportError:  # imported as a top-level module
    import sandbox

REPO = Path(__file__).resolve().parents[1]


class DataDirTest(unittest.TestCase):
    """INSTA_DATA puts every remembered path in one folder.

    Checked in a subprocess because engine.py resolves these at import time --
    which is the point: an env var set before the app starts is all it takes,
    and re-importing in this process would lie about that.
    """

    PROBE = (
        "import json, engine;"
        "print(json.dumps({"
        "'state': str(engine.STATE_FILE),"
        "'profiles': str(engine.profiles_path()),"
        "'cache': str(engine.CACHE_DB),"
        "'thumbs': str(engine.THUMB_CACHE),"
        "'avatars': str(engine.AVATAR_CACHE),"
        "'session': str(engine.instaloader_config_dir()),"
        "'output': str(engine.OUTPUT_ROOT)}))"
    )

    def _probe(self, **env_extra) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("INSTA_")}
        env.update(env_extra)
        out = subprocess.run([sys.executable, "-c", self.PROBE], cwd=str(REPO),
                             env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_one_folder_when_insta_data_is_set(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            got = self._probe(INSTA_DATA=td)
        self.assertEqual(got["state"], str(Path(td) / "state.json"))
        self.assertEqual(got["profiles"], str(Path(td) / "profiles.db"))
        self.assertEqual(got["cache"], str(Path(td) / "gallery-cache.db"))
        self.assertEqual(got["thumbs"], str(Path(td) / "thumbs"))
        self.assertEqual(got["avatars"], str(Path(td) / "avatars"))
        # The session has to travel too: otherwise a new machine has to log in
        # again, and Instagram rate-limits logins.
        self.assertEqual(got["session"], str(Path(td) / "instaloader"))

    def test_platform_defaults_when_insta_data_is_unset(self):
        got = self._probe(HOME="/tmp/hw-datadir-test", XDG_CONFIG_HOME="/tmp/hw-datadir-test/cfg",
                          XDG_CACHE_HOME="/tmp/hw-datadir-test/cache")
        self.assertEqual(got["state"], "/tmp/hw-datadir-test/cfg/hardlywatching/state.json")
        self.assertEqual(got["profiles"], "/tmp/hw-datadir-test/cfg/hardlywatching/profiles.db")
        self.assertEqual(got["thumbs"], "/tmp/hw-datadir-test/cache/hardlywatching/thumbs")
        self.assertTrue(got["session"].endswith("/instaloader"))

    def test_specific_overrides_still_win(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            got = self._probe(INSTA_DATA=td, INSTA_STATE="/tmp/explicit-state.json")
        self.assertEqual(got["state"], "/tmp/explicit-state.json")
        self.assertEqual(got["profiles"], str(Path(td) / "profiles.db"))


class BackupRoundTripTest(unittest.TestCase):
    """Export from one sandbox, restore into another with a different root."""

    @classmethod
    def setUpClass(cls):
        cls.src = sandbox.Sandbox(seed_profile="backup_src", seed_count=3).start()
        profile = cls.src.seed_profile("backup_src", 2)
        files = sorted(p.name for p in profile.iterdir())
        # Something that is only in the database, never on disk: a bookmark.
        cls.bookmarked = files[0] if files else "2024-01-01_00-00-00_UTC.jpg"
        res = cls.src.post("/api/bookmark", {"profile": "backup_src",
                                            "file": cls.bookmarked, "on": True})
        assert res.status == 200, res.body[:400]
        cls.zip_bytes = b""

    @classmethod
    def tearDownClass(cls):
        cls.src.stop()

    def test_1_export_is_a_portable_zip(self):
        res = self.src.get("/api/backup/export")
        self.assertEqual(res.status, 200, res.body[:400])
        self.assertEqual(res.content_type, "application/zip")
        raw = res.body
        self.assertGreater(len(raw), 1000)
        BackupRoundTripTest.zip_bytes = raw
        with zipfile.ZipFile(__import__("io").BytesIO(raw)) as zf:
            names = set(zf.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("state.json", names)
            self.assertIn("profiles.db", names)
            manifest = json.loads(zf.read("manifest.json"))
            self.assertEqual(manifest["app"], "hardlywatching")
            self.assertEqual(manifest["format"], 1)
            self.assertEqual(manifest["counts"]["bookmarks"], 1)
            self.assertEqual(manifest["output_root"], str(self.src.output))
            # The listing cache has no business in a backup.
            self.assertNotIn("gallery-cache.db", names)

    def test_2_export_can_include_the_session(self):
        res = self.src.get("/api/backup/export?session=1")
        self.assertEqual(res.status, 200)
        with zipfile.ZipFile(__import__("io").BytesIO(res.body)) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        self.assertTrue(manifest["session"])

    def test_3_info_reports_this_machines_paths(self):
        res = self.src.get("/api/backup/info")
        self.assertEqual(res.status, 200, res.body[:400])
        info = res.json()
        self.assertEqual(info["paths"]["state"], str(self.src.state_file))
        self.assertEqual(info["paths"]["profiles"],
                         str(self.src.home / ".config" / "hardlywatching" / "profiles.db"))
        self.assertFalse(info["portable"])          # INSTA_DATA is unset in the sandbox
        self.assertGreater(info["sizes"]["state"], 0)

    def test_4_restore_into_a_different_archive_path(self):
        self.assertTrue(BackupRoundTripTest.zip_bytes, "test_1 must run first")
        # Same folder names in the archive, different absolute path: this is
        # the "same drive, new mount point, new machine" case.
        dst = sandbox.Sandbox(seed_profile="backup_src", seed_count=3).start()
        try:
            self.assertNotEqual(dst.output, self.src.output)
            # The destination archive has the same folder, so the bookmark's
            # file exists there -- give it the same name the backup recorded.
            dst_profile = dst.seed_profile("backup_src", 1)
            (dst_profile / self.bookmarked).write_bytes(b"\xff\xd8\xff\xd9")
            res = dst.post_raw("/api/backup/import?root=%s" % dst.output,
                               BackupRoundTripTest.zip_bytes)
            self.assertEqual(res.status, 200, res.body[:600])
            summary = res.json()
            self.assertTrue(summary["ok"])
            self.assertTrue(summary["restored"]["state"])
            self.assertTrue(summary["restored"]["profiles"])
            self.assertTrue(summary["root_changed"])
            self.assertEqual(summary["archive_root"], str(dst.output))
            # Before this, the destination had no backups at all; the restore
            # keeps a copy of what it replaced.
            self.assertTrue(summary["paths"]["backups"])

            # The bookmark survived, and it is the same post.
            marks = dst.get("/api/bookmarks")
            self.assertEqual(marks.status, 200, marks.body[:400])
            files = [item["file"]
                     for post in marks.json().get("posts") or []
                     if post.get("bookmarked")
                     for item in post.get("items") or []]
            self.assertIn(self.bookmarked, files)

            # The state on disk points at *this* machine's archive, and the old
            # root is not left behind as a ghost folder.
            state = json.loads(dst.state_file.read_text())
            self.assertEqual(state["output_root"], str(dst.output))
            self.assertEqual(state["roots"], [str(dst.output)])
            # A restored queue comes back paused: the people are back, but
            # nothing starts fetching until somebody says so.
            self.assertEqual(state["user_paused"], {"instagram": True, "tiktok": True})
            # ... and the running server agrees, without a restart.
            live = dst.get("/api/state").json()
            self.assertEqual(live["output_root"], str(dst.output))
        finally:
            dst.stop()

    def test_5_a_broken_zip_is_refused_and_nothing_changes(self):
        dst = sandbox.Sandbox(seed_profile="untouched", seed_count=1).start()
        try:
            res = dst.post_raw("/api/backup/import", b"this is not a zip")
            self.assertEqual(res.status, 400)
            self.assertIn("error", res.json())
            # The gallery is still what it was before the attempt.
            gallery = dst.get("/api/gallery").json().get("profiles") or []
            self.assertIn("untouched", [g.get("username") for g in gallery])
        finally:
            dst.stop()

    def test_6_a_zip_without_a_manifest_is_refused(self):
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("state.json", "{}")
        dst = sandbox.Sandbox(seed_profile="untouched2", seed_count=1).start()
        try:
            res = dst.post_raw("/api/backup/import", buf.getvalue())
            self.assertEqual(res.status, 400)
            self.assertIn("manifest", res.json()["error"].lower())
        finally:
            dst.stop()


class AutoBackupTest(unittest.TestCase):
    """A dated zip appears next to the data, and old ones are pruned."""

    def test_auto_backup_writes_a_file_and_prunes(self):
        sb = sandbox.Sandbox(seed_profile="auto_src", seed_count=2).start()
        try:
            # The server may already have written today's automatic backup on
            # startup, so count relative to what is there now.
            n0 = len(sb.get("/api/backup/info").json()["backups"])
            first = sb.post("/api/backup/auto", {"keep": 7})
            self.assertEqual(first.status, 200, first.body[:400])
            body = first.json()
            self.assertTrue(body["ok"])
            # >= because the startup backup runs in a background thread and may
            # land during this call; what must hold is that this one exists, by
            # its own name, and that nothing was overwritten.
            self.assertGreaterEqual(len(body["backups"]), n0 + 1)
            self.assertIn(body["backup"]["name"],
                          [b["name"] for b in body["backups"]])
            path = Path(body["backup"]["path"])
            self.assertTrue(path.is_file())
            self.assertTrue(path.name.startswith("hw-backup-"))
            with zipfile.ZipFile(path) as zf:
                self.assertIn("profiles.db", zf.namelist())
            # A second one, then pruning to one.
            self.assertEqual(sb.post("/api/backup/auto", {"keep": 1}).status, 200)
            listed = sb.get("/api/backup/info").json()["backups"]
            self.assertEqual(len(listed), 1)
            # And the listed backup downloads by name.
            got = sb.get("/api/backup/download?name=%s" % listed[0]["name"])
            self.assertEqual(got.status, 200)
            self.assertEqual(len(got.body), listed[0]["bytes"])
        finally:
            sb.stop()


if __name__ == "__main__":
    unittest.main()
