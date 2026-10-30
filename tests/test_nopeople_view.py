"""The archive-wide "No people" view, which has its own rules.

A post is people-free only when EVERY part of it was judged and none of them
had a person -- the same predicate `_attach_people` applies per folder. Grouping
only the clean rows was the obvious version and it was wrong: a ten-frame
carousel with two people in it came out as "clean" with those two frames
silently dropped.

These tests exist because that view shipped with 191 green tests that all
covered the per-profile path it deliberately bypasses.
"""
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.sandbox import Sandbox  # noqa: E402


class PeopleFreeViewTests(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox(seed_profile=None)
        self.sb.start()
        self.addCleanup(self.sb.stop)
        import engine as E
        import profile_db as pdb
        self.E, self.pdb = E, pdb
        old_state, old_shared, old_pdb = E.STATE_FILE, E._shared_db, pdb._shared_db
        self.addCleanup(setattr, E, "STATE_FILE", old_state)
        self.addCleanup(setattr, E, "_shared_db", old_shared)
        self.addCleanup(setattr, pdb, "_shared_db", old_pdb)
        self.db = pdb.ProfileDB(self.sb.root / "nobody.db")
        self.addCleanup(self.db.close)
        E.STATE_FILE = self.sb.root / "nobody-state.json"
        E._shared_db = lambda: self.db
        pdb._shared_db = lambda: self.db
        self.root = self.sb.root / "archive"
        self.folder = self.root / "someone"
        self.folder.mkdir(parents=True)
        self.eng = E.Engine(output_root=self.root)
        self.eng.roots = [self.root]
        self.addCleanup(self.eng.save_state)

    # -- helpers ---------------------------------------------------------
    def media(self, *names, size=200):
        for n in names:
            (self.folder / n).write_bytes(b"x" * size)

    def judge(self, *pairs):
        """pairs of (filename, verdict): 1 person, 0 nobody, -1 unreadable."""
        rows = [(str(self.folder / n), (self.folder / n).stat().st_size, v, 0.5)
                for n, v in pairs]
        self.db.people_set_many(rows)

    def posts(self):
        return {p["post"]: p for p in self.eng.people_free_posts()}

    # -- the predicate ---------------------------------------------------
    def test_a_clean_post_is_listed(self):
        self.media("2024-05-01_10-00-00_UTC.jpg")
        self.judge(("2024-05-01_10-00-00_UTC.jpg", 0))
        self.assertIn("2024-05-01_10-00-00_UTC", self.posts())

    def test_a_carousel_with_one_person_is_not_listed(self):
        # The bug that shipped: only the clean rows were grouped, so this post
        # came out as people-free with the frame that has somebody in it gone.
        self.media("2024-05-02_10-00-00_UTC_1.jpg",
                   "2024-05-02_10-00-00_UTC_2.jpg",
                   "2024-05-02_10-00-00_UTC_3.jpg")
        self.judge(("2024-05-02_10-00-00_UTC_1.jpg", 0),
                   ("2024-05-02_10-00-00_UTC_2.jpg", 1),
                   ("2024-05-02_10-00-00_UTC_3.jpg", 0))
        self.assertNotIn("2024-05-02_10-00-00_UTC", self.posts(),
                         "a post with a person in one frame was listed as clean")

    def test_a_half_judged_carousel_is_not_listed(self):
        self.media("2024-05-03_10-00-00_UTC_1.jpg", "2024-05-03_10-00-00_UTC_2.jpg")
        self.judge(("2024-05-03_10-00-00_UTC_1.jpg", 0))
        self.assertNotIn("2024-05-03_10-00-00_UTC", self.posts(),
                         "an unjudged frame was treated as clean")

    def test_an_unreadable_file_is_not_clean(self):
        self.media("2024-05-04_10-00-00_UTC.mp4")
        self.judge(("2024-05-04_10-00-00_UTC.mp4", -1))
        self.assertNotIn("2024-05-04_10-00-00_UTC", self.posts())

    def test_a_fully_clean_carousel_is_listed_in_numeric_order(self):
        names = [f"2024-05-05_10-00-00_UTC_{n}.jpg" for n in (1, 3, 10)]
        self.media(*names)
        self.judge(*[(n, 0) for n in names])
        got = self.posts()["2024-05-05_10-00-00_UTC"]
        parts = [self.E.POST_RE.match(i["file"]).group("part") for i in got["items"]]
        self.assertEqual(parts, ["1", "3", "10"],
                         "_10 must not sort between _1 and _3")

    # -- the files behind it ---------------------------------------------
    def test_a_post_whose_file_vanished_is_not_listed(self):
        self.media("2024-05-06_10-00-00_UTC.jpg")
        self.judge(("2024-05-06_10-00-00_UTC.jpg", 0))
        (self.folder / "2024-05-06_10-00-00_UTC.jpg").unlink()
        self.assertNotIn("2024-05-06_10-00-00_UTC", self.posts())

    def test_a_zero_byte_placeholder_is_not_listed(self):
        (self.folder / "2024-05-07_10-00-00_UTC.jpg").write_bytes(b"")
        self.db.people_set_many(
            [(str(self.folder / "2024-05-07_10-00-00_UTC.jpg"), 0, 0, 0.1)])
        self.assertNotIn("2024-05-07_10-00-00_UTC", self.posts())

    def test_moving_a_post_to_trash_drops_it(self):
        self.media("2024-05-08_10-00-00_UTC.jpg")
        self.judge(("2024-05-08_10-00-00_UTC.jpg", 0))
        self.assertIn("2024-05-08_10-00-00_UTC", self.posts())
        ok, msg = self.eng.move_media("someone", "2024-05-08_10-00-00_UTC.jpg", "trash")
        self.assertTrue(ok, msg)
        self.assertNotIn("2024-05-08_10-00-00_UTC", self.posts(),
                         "a trashed post stayed in the people-free list")

    # -- ordering --------------------------------------------------------
    def test_a_tiktok_id_gives_a_real_timestamp(self):
        # 7494727892723633430 >> 32 is the second it was posted.
        got = self.E._post_ts("7494727892723633430")
        self.assertGreater(got, 1_700_000_000)
        self.assertEqual(self.E._post_ts("2024-05-09_10-00-00_UTC"), 1715248800)

    def test_the_page_reports_a_total_and_slices(self):
        for n in range(5):
            name = f"2024-06-0{n + 1}_10-00-00_UTC.jpg"
            self.media(name)
            self.judge((name, 0))
        page = self.eng.people_free_page(2, 0)
        self.assertEqual(page["total"], 5)
        self.assertEqual(len(page["posts"]), 2)
        self.assertTrue(page["has_more"])
        tail = self.eng.people_free_page(2, 4)
        self.assertEqual(len(tail["posts"]), 1)
        self.assertFalse(tail["has_more"])


if __name__ == "__main__":
    unittest.main()
