"""Tests for dupes.py -- the byte-identical finder.

The scan is the dangerous half of the feature (it walks someone's archive), so
these pin the rules that make it safe to run: it is read-only, it never compares
across platforms, it ignores tombstones and derived files, and it needs the same
size *and* the same checksum before it calls two files identical.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dupes


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class DupesScanTest(unittest.TestCase):
    def test_identical_files_in_two_profiles_are_one_group(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            blob = os.urandom(4096)
            a = _write(root / "alice" / "2024-01-01_01.jpg", blob)
            b = _write(root / "bob" / "2024-01-01_01.jpg", blob)
            res = dupes.scan([root])
            self.assertEqual(len(res["groups"]), 1)
            g = res["groups"][0]
            self.assertEqual(len(g["files"]), 2)
            self.assertEqual({f["profile"] for f in g["files"]}, {"alice", "bob"})
            self.assertEqual(g["wasted"], 4096)   # one of the two is recoverable
            self.assertEqual(res["wasted"], 4096)
            self.assertFalse(res["stopped"])
            # The scan is read-only: both files are still there.
            self.assertTrue(a.exists())
            self.assertTrue(b.exists())

    def test_same_size_different_bytes_is_not_a_duplicate(self):
        # The size bucket is a filter, not the answer. Two 4096-byte files that
        # differ anywhere must not be reported -- this is the case that would
        # make automatic deletion catastrophic.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root / "alice" / "a.jpg", b"A" * 4096)
            _write(root / "bob" / "b.jpg", b"B" * 4096)
            res = dupes.scan([root])
        self.assertEqual(res["groups"], [])
        self.assertEqual(res["hashed"], 2)   # both compared, just not equal

    def test_platforms_are_never_merged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            blob = os.urandom(2048)
            _write(root / "alice" / "pic.jpg", blob)
            _write(root / "tiktok" / "alice" / "pic.jpg", blob)
            res = dupes.scan([root])
        self.assertEqual(res["groups"], [])

    def test_platform_filter_keeps_only_that_platform(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            blob = os.urandom(2048)
            _write(root / "alice" / "pic.jpg", blob)
            _write(root / "bob" / "pic.jpg", blob)
            _write(root / "tiktok" / "alice" / "clip.mp4", blob)
            res = dupes.scan([root], platform="instagram")
        self.assertEqual(len(res["groups"]), 1)
        self.assertTrue(all(f["platform"] == "instagram"
                            for f in res["groups"][0]["files"]))

    def test_tombstones_and_small_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 0-byte placeholders: what a deleted post leaves behind, and
            # identical by definition. Reporting them would offer to "tidy"
            # every deletion ever made.
            _write(root / "alice" / "deleted_01.jpg", b"")
            _write(root / "bob" / "deleted_01.jpg", b"")
            # Below the size floor: a real duplicate, but not worth reading.
            _write(root / "alice" / "tiny.jpg", b"x" * 10)
            _write(root / "bob" / "tiny.jpg", b"x" * 10)
            res = dupes.scan([root])
        self.assertEqual(res["groups"], [])
        self.assertEqual(res["scanned"], 0)

    def test_sidecars_and_derived_dirs_are_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            blob = os.urandom(4096)
            _write(root / "alice" / "post.jpg", blob)
            _write(root / "bob" / "post.jpg", blob)
            # Same bytes, but not posts: a json sidecar, a cache dir, a dotdir,
            # a backup copy.
            _write(root / "alice" / "post.jpg.json", blob)
            _write(root / ".cache" / "x.jpg", blob)
            _write(root / "alice" / "thumbnails" / "post.jpg", blob)
            _write(root / "backups" / "old.jpg", blob)
            res = dupes.scan([root])
        self.assertEqual(len(res["groups"]), 1)
        names = {f["path"] for f in res["groups"][0]["files"]}
        self.assertEqual(len(names), 2)
        self.assertTrue(all("thumbnail" not in p and ".cache" not in p
                            and "backups" not in p for p in names))

    def test_sections_come_from_the_folder_layout(self):
        # move_media writes `<profile>/trash/<file>`, so a copy that has been
        # set aside is *inside* the profile folder, not in a top-level trash/.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            blob = os.urandom(4096)
            _write(root / "alice" / "post.jpg", blob)
            _write(root / "bob" / "trash" / "post.jpg", blob)
            res = dupes.scan([root])
            self.assertEqual(len(res["groups"]), 1)
            sections = {f["section"] for f in res["groups"][0]["files"]}
            self.assertEqual(sections, {"main", "trash"})
            # Only the main copy is recoverable space: the trashed one is
            # already set aside, so it must not be counted twice.
            self.assertEqual(res["groups"][0]["wasted"], 0)
            # Oldest first, so the UI can treat the first entry as "keep".
            files = res["groups"][0]["files"]
            self.assertEqual([f["mtime"] for f in files],
                             sorted(f["mtime"] for f in files))

    def test_three_copies_count_wasted_bytes_once_per_extra(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            blob = os.urandom(2048)   # above dupes.MIN_SIZE
            for who in ("alice", "bob", "carol"):
                _write(root / who / "post.jpg", blob)
            res = dupes.scan([root])
        self.assertEqual(len(res["groups"]), 1)
        self.assertEqual(res["groups"][0]["wasted"], 4096)
        self.assertEqual(res["group_count"], 1)

    def test_stop_returns_a_partial_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for i in range(6):
                blob = os.urandom(2048)
                _write(root / "alice" / f"p{i}.jpg", blob)
                _write(root / "bob" / f"p{i}.jpg", blob)
            calls = {"n": 0}

            def should_stop():
                calls["n"] += 1
                return calls["n"] > 2

            res = dupes.scan([root], should_stop=should_stop)
        self.assertTrue(res["stopped"])
        # Whatever was found before stopping is still a well-formed report.
        self.assertIsInstance(res["groups"], list)

    def test_top_level_section_folders_are_still_understood(self):
        # Older archives have `trash/<profile>/<file>` at the root; those files
        # must not be reported as being in the main folder, or the panel would
        # offer to move them again.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            blob = os.urandom(4096)
            _write(root / "alice" / "post.jpg", blob)
            _write(root / "trash" / "alice" / "post.jpg", blob)
            res = dupes.scan([root])
        self.assertEqual(len(res["groups"]), 1)
        self.assertEqual({f["section"] for f in res["groups"][0]["files"]},
                         {"main", "trash"})

    def test_unreadable_root_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            res = dupes.scan([Path(td) / "does-not-exist"])
        self.assertEqual(res["groups"], [])
        self.assertEqual(res["scanned"], 0)

    def test_scanner_runs_in_a_thread_and_reports_progress(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            blob = os.urandom(4096)
            _write(root / "alice" / "post.jpg", blob)
            _write(root / "bob" / "post.jpg", blob)
            sc = dupes.Scanner()
            self.assertTrue(sc.start([root]))
            sc._thread.join(20)
            snap = sc.snapshot()
            self.assertFalse(snap["running"])
            self.assertIsNone(snap["error"])
            self.assertEqual(snap["report"]["group_count"], 1)
            # Starting again after a finished scan is allowed; while one runs it
            # is refused rather than queued, because nobody waits on the second.
            self.assertTrue(sc.start([root]))
            self.assertFalse(sc.start([root]))
            sc.stop()
            sc._thread.join(20)


if __name__ == "__main__":
    unittest.main()
