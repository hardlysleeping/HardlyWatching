"""What makes the gallery caches give up their answer.

Every request now reuses three things it used to rebuild: one folder's listing
plus its invalidation stamp (``_folder_sig``), the profile list's disk half
(``gallery_profiles``), and the whole timeline (``Engine.timeline``). A cache
that keeps serving a trashed post is worse than a slow one, so each of them is
tested the same way: warm it, change the archive the way the app does, and
assert the next call already shows the change.

The rules being pinned, in one place:

  * a file added, removed, renamed or emptied changes its directory, and the
    directory's (mtime, size, inode) is checked on every call -- so trash,
    favourites, uploads and deletes are visible immediately;
  * a write that leaves no trace in that metadata (a file emptied in place)
    goes through ``_media_cache_forget``, which is what the built answers are
    told about;
  * a file rewritten IN PLACE with the same name and size -- an edited sidecar
    -- is only re-read after ``SIG_TTL``, which is the one lag the design
    accepts. The test pins that by setting the TTL to 0 rather than sleeping.

In-process against a throwaway archive, with the cache database repointed at
the sandbox: a test must never write a row about a temp folder into the real
one, and must never read the live state.
"""

from __future__ import annotations

import json
import unittest

try:  # imported as `tests.test_gallery_cache` (discover from the repo root)
    from . import sandbox
except ImportError:  # imported as a top-level module (discover -s tests)
    import sandbox


class CacheCase(unittest.TestCase):
    """Sandbox, temp archive, and an Engine pointed entirely at both."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sb = sandbox.Sandbox(seed_profile=None)
        cls.sb.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sb.stop()

    def engine(self, root):
        """An Engine over `root`, with state and every database in the sandbox."""
        import engine as E
        import profile_db as pdb

        old_state, old_shared, old_cache = E.STATE_FILE, E._shared_db, E._cache
        old_pdb = pdb._shared_db
        self.addCleanup(setattr, E, "STATE_FILE", old_state)
        self.addCleanup(setattr, E, "_shared_db", old_shared)
        self.addCleanup(setattr, E, "_cache", old_cache)
        self.addCleanup(setattr, pdb, "_shared_db", old_pdb)
        # The memory caches are module-level, so a previous test's folders would
        # otherwise be visible here.
        E._SIG_CACHE.clear()
        E._PEOPLE_KNOWN.clear()
        self.addCleanup(E._SIG_CACHE.clear)
        self.addCleanup(E._PEOPLE_KNOWN.clear)

        db = pdb.ProfileDB(self.sb.root / ("cache-test-%s.db"
                                          % self.id().rsplit(".", 1)[-1]))
        self.addCleanup(db.close)
        E.STATE_FILE = self.sb.root / "cache-test-state.json"
        E._shared_db = lambda: db
        pdb._shared_db = lambda: db
        E._cache = E._Cache(self.sb.root / "cache-test.db")

        eng = E.Engine(output_root=root)
        eng.roots = [root]
        self.addCleanup(eng.save_state)
        return eng

    @staticmethod
    def post(folder, stamp, caption="", parts=1):
        """One post on disk: `parts` media files and the sidecar beside them."""
        for n in range(1, parts + 1):
            name = stamp if parts == 1 else "%s_%d" % (stamp, n)
            (folder / (name + ".jpg")).write_bytes(b"x" * 120)
        side = {"node": {"date": 1700000000, "caption": caption,
                         "owner": {"username": folder.name}}}
        (folder / (stamp + ".json")).write_text(json.dumps(side))


class FolderSignatureTests(CacheCase):
    """One folder, one scan -- and what makes it scan again."""

    def setUp(self):
        # A folder per test: the signature cache deliberately does NOT notice a
        # file rewritten in place until SIG_TTL has passed, so tests that share
        # one directory would read each other's edits.
        self.root = self.sb.root / ("sig-archive-" + self.id().rsplit(".", 1)[-1])
        self.folder = self.root / "someone"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.post(self.folder, "2024-01-01_12-00-00_UTC", caption="first")
        self.post(self.folder, "2024-01-02_12-00-00_UTC", caption="second")
        self.eng = self.engine(self.root)

    def names(self, section="main"):
        return sorted(p["post"] for p in self.eng.gallery_posts("someone", section))

    def test_a_trashed_post_leaves_the_grid_at_once(self):
        self.assertEqual(len(self.names()), 2)
        self.assertEqual(len(self.names()), 2)          # second call: all cached
        ok, _ = self.eng.move_media("someone", "2024-01-02_12-00-00_UTC.jpg", "trash")
        self.assertTrue(ok)
        self.assertEqual(self.names(), ["2024-01-01_12-00-00_UTC"],
                         "a trashed post was still in the grid")
        self.assertEqual(self.names("trash"), ["2024-01-02_12-00-00_UTC"])

    def test_writes_inside_a_subdirectory_do_not_throw_the_posts_away(self):
        """A folder inside a profile is not a change to any post in it.

        Download staging directories churn constantly while a job runs; if their
        contents counted, a profile's cached posts would be rebuilt over and
        over for files that cannot alter a single caption.
        """
        import engine as E

        staging = self.folder / "downloads-in-progress"
        staging.mkdir()
        self.names()
        sig = E._folder_sig(self.folder, deep=False)
        self.assertIsNotNone(sig)
        before = sig[1]
        for n in range(3):
            (staging / ("part%d.jpg" % n)).write_bytes(b"x" * 200)
        self.names()
        sig = E._folder_sig(self.folder, deep=False)
        self.assertIsNotNone(sig)
        self.assertEqual(before, sig[1],
                         "a write inside a subdirectory invalidated the posts")

    def test_a_file_emptied_in_place_is_dropped_by_the_write_hook(self):
        """The trash flow empties a file: no directory metadata changes."""
        import engine as E

        before = self.names()
        self.assertEqual(len(before), 2)
        target = self.folder / "2024-01-02_12-00-00_UTC.jpg"
        target.write_bytes(b"")            # emptied, not removed: size 0
        # A cached signature cannot see this, which is exactly why the write
        # paths call this hook.
        E._media_cache_forget(self.folder)
        self.assertEqual(self.names(), ["2024-01-01_12-00-00_UTC"],
                         "an emptied file was still listed after the write hook")

    def test_a_new_file_appears_without_any_hook(self):
        self.assertEqual(len(self.names()), 2)
        self.post(self.folder, "2024-01-03_12-00-00_UTC", caption="third")
        self.assertEqual(len(self.names()), 3,
                         "a new download did not show up on the next request")

    def test_an_edited_sidecar_is_reread_after_the_ttl(self):
        """The one accepted lag: an in-place rewrite, with the same name/size."""
        import engine as E

        old_ttl = E.SIG_TTL
        self.addCleanup(setattr, E, "SIG_TTL", old_ttl)
        E.SIG_TTL = 0.0                    # stand in for "the TTL has elapsed"

        captions = {p["post"]: p["caption"] for p in self.eng.gallery_posts("someone")}
        self.assertEqual(captions["2024-01-01_12-00-00_UTC"], "first")

        side = self.folder / "2024-01-01_12-00-00_UTC.json"
        doc = json.loads(side.read_text())
        doc["node"]["caption"] = "edited"
        side.write_text(json.dumps(doc))   # same name, same length? irrelevant: mtime
        captions = {p["post"]: p["caption"] for p in self.eng.gallery_posts("someone")}
        self.assertEqual(captions["2024-01-01_12-00-00_UTC"], "edited",
                         "an edited sidecar kept serving the old caption")

    def test_the_sqlite_mirror_answers_a_cold_start(self):
        """A new process must not rescan: the row carries names AND the stamp."""
        import engine as E

        names, stamp = E._folder_sig(self.folder, deep=False)
        self.assertEqual(len(names), 2)
        E._SIG_CACHE.clear()               # as if this were a fresh process
        disk = E._cache.get_sig(str(self.folder), E._dir_stamp(self.folder))
        self.assertIsNotNone(disk, "the mirror refused a row it had just written")
        self.assertEqual(sorted(disk[0]), sorted(names))
        self.assertEqual(disk[1], stamp)
        # A directory that has changed must not be answered from the row.
        self.post(self.folder, "2024-01-04_12-00-00_UTC")
        self.assertIsNone(
            E._cache.get_sig(str(self.folder), (0, 0, 0)),
            "a row was accepted for a directory stamp that does not match")


class BuiltAnswerTests(CacheCase):
    """The profile list and the timeline: reused until the archive moves."""

    def setUp(self):
        self.root = self.sb.root / ("built-archive-" + self.id().rsplit(".", 1)[-1])
        self.folder = self.root / "somebody"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.post(self.folder, "2024-02-01_12-00-00_UTC")
        self.post(self.folder, "2024-02-02_12-00-00_UTC")
        self.eng = self.engine(self.root)

    def profile_count(self):
        got = [p for p in self.eng.gallery_profiles()
               if p["username"] == "somebody"]
        self.assertEqual(len(got), 1, "the seeded profile is not listed")
        return got[0]["count"]

    def test_the_timeline_is_reused_and_rebuilt_when_a_file_moves(self):
        first = self.eng.timeline()
        self.assertEqual(first["total"], 2)
        built = self.eng._timeline_all
        again = self.eng.timeline()
        self.assertEqual(again["total"], 2)
        self.assertIs(self.eng._timeline_all, built,
                      "the timeline was rebuilt with nothing changed on disk")

        ok, _ = self.eng.move_media("somebody", "2024-02-02_12-00-00_UTC.jpg", "trash")
        self.assertTrue(ok)
        self.assertEqual(self.eng.timeline()["total"], 1,
                         "a trashed post stayed in the cached timeline")
        self.assertIsNot(self.eng._timeline_all, built,
                         "the timeline was not rebuilt after a file moved")

    def test_the_profile_list_count_follows_the_folder(self):
        self.assertEqual(self.profile_count(), 2)
        self.assertEqual(self.profile_count(), 2)      # cached disk half
        self.post(self.folder, "2024-02-03_12-00-00_UTC")
        self.assertEqual(self.profile_count(), 3,
                         "a new file did not reach the profile list")
        ok, _ = self.eng.move_media("somebody", "2024-02-01_12-00-00_UTC.jpg", "trash")
        self.assertTrue(ok)
        self.assertEqual(self.profile_count(), 2,
                         "a trashed file was still counted in the profile list")

    def test_a_new_profile_appears_in_the_list(self):
        self.assertEqual(self.profile_count(), 2)
        other = self.root / "another"
        other.mkdir(parents=True, exist_ok=True)
        self.post(other, "2024-03-01_12-00-00_UTC")
        names = {p["username"] for p in self.eng.gallery_profiles()}
        self.assertIn("another", names,
                      "a profile folder added while running was not listed")

    def test_gallery_all_shows_a_favourite_at_once(self):
        posts = self.eng.gallery_all("favorites")
        self.assertEqual(posts, [])
        ok, _ = self.eng.move_media("somebody", "2024-02-01_12-00-00_UTC.jpg",
                                    "favorites")
        self.assertTrue(ok)
        posts = self.eng.gallery_all("favorites")
        self.assertEqual(len(posts), 1,
                         "a favourited post did not appear in the all-profiles view")
        self.assertEqual(posts[0]["username"], "somebody")
        self.assertEqual(posts[0]["section"], "favorites")


if __name__ == "__main__":
    unittest.main(verbosity=2)
