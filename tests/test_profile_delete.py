"""One action that deletes a profile: files, queue rows, and a lasting exclusion.

The user asked for "an easier faster way to delete profile and/or all their
media and not reload it accidentally, needs to leave queue and finished". Three
things have to happen together, and each one has a failure mode worth pinning:

  * the media folder goes to the SYSTEM TRASH (recoverable), never unlinked and
    never ``shutil.rmtree`` -- and if the trash refuses it, nothing else happens
    either, so a half-deleted profile cannot be quietly rebuilt by the next run;
  * every job for that name and platform is dropped whatever its status, so the
    person leaves Waiting and Finished together;
  * an exclusion is recorded, so ``add()``, an extra pass, a TikTok paste and the
    empty-queue autocheck all refuse it with a message that says how to undo it.

Everything here runs against a throwaway archive, a throwaway ``state.json`` and
a throwaway ``profiles.db`` (the sandbox's own HOME, so even the Trash bin is
inside the temp tree). The live archive is never a test fixture: a delete bug is
not recoverable.
"""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path

try:  # imported as `tests.test_profile_delete` (discover from the repo root)
    from . import sandbox
except ImportError:  # imported as a top-level module (discover -s tests)
    import sandbox


class DeleteCase(unittest.TestCase):
    """A sandbox, a temp archive, and an Engine pointed entirely at both."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sb = sandbox.Sandbox(seed_profile=None)
        cls.sb.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sb.stop()

    def setUp(self) -> None:
        import engine as E
        import profile_db as pdb

        tag = self.id().rsplit(".", 1)[-1]
        self.root = self.sb.root / ("delete-archive-" + tag)
        self.root.mkdir(parents=True, exist_ok=True)

        old_state, old_shared, old_cache = E.STATE_FILE, E._shared_db, E._cache
        old_pdb = pdb._shared_db
        self.addCleanup(setattr, E, "STATE_FILE", old_state)
        self.addCleanup(setattr, E, "_shared_db", old_shared)
        self.addCleanup(setattr, E, "_cache", old_cache)
        self.addCleanup(setattr, pdb, "_shared_db", old_pdb)
        E._SIG_CACHE.clear()
        self.addCleanup(E._SIG_CACHE.clear)

        self.db = pdb.ProfileDB(self.sb.root / ("delete-%s.db" % tag))
        self.addCleanup(self.db.close)
        E.STATE_FILE = self.sb.root / ("delete-%s-state.json" % tag)
        E._shared_db = lambda: self.db
        pdb._shared_db = lambda: self.db
        E._cache = E._Cache(self.sb.root / ("delete-%s-cache.db" % tag))

        self.eng = E.Engine(output_root=self.root)
        self.eng.roots = [self.root]
        # add() kicks a worker, and a worker in THIS process would run the real
        # downloader against the network. Nothing here needs it to run.
        self.eng._kick = lambda: None
        self.addCleanup(self.eng.save_state)

    # ------------------------------------------------------------------ helpers

    def job(self, username: str, status: str, kind: str = "main",
            platform: str = "instagram"):
        import engine as E

        j = E.Job(username=username, kind=kind, platform=platform)
        j.status = status
        self.eng.jobs[j.id] = j
        if status in ("queued", "retrying"):
            self.eng.pending.append(j.id)
        return j

    def profile_folder(self, name: str, files: int = 3) -> Path:
        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        for n in range(files):
            (folder / ("2024-01-0%d_12-00-00_UTC.jpg" % (n + 1))).write_bytes(b"x" * 64)
        (folder / (name + "_profile_pic.jpg")).write_bytes(b"x" * 64)
        return folder

    def trash_files(self) -> Path:
        """Where the sandbox's Trash bin lives (HOME is inside the temp tree)."""
        home = Path(self.sb.home)
        return home / ".local" / "share" / "Trash" / "files"


class RefusalTests(DeleteCase):
    """Nothing is touched while that profile is downloading."""

    def test_refuses_while_that_profile_is_running(self):
        running = self.job("Busy", "running")
        folder = self.profile_folder("Busy")

        res = self.eng.delete_profile("busy", "instagram")

        self.assertFalse(res["ok"])
        self.assertIn("downloading", res["error"])
        self.assertIn(running.id, self.eng.jobs, "a running job was dropped")
        self.assertTrue(folder.is_dir(), "media moved while the job was running")
        self.assertNotIn("busy", self.eng._excluded_set("instagram"),
                         "a refused delete still recorded an exclusion")

    def test_another_profile_running_does_not_block_it(self):
        self.job("someoneelse", "running")
        self.job("target", "done")
        res = self.eng.delete_profile("target", "instagram", media=False)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["jobs_removed"], 1)


class QueueRowTests(DeleteCase):
    """The person leaves Waiting and Finished together."""

    def test_a_done_job_and_a_queued_job_both_disappear(self):
        done = self.job("Gone", "done")
        queued = self.job("gone", "queued", kind="tagged")
        other = self.job("other", "queued")

        res = self.eng.delete_profile("@gone", "instagram", media=False)

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["jobs_removed"], 2)
        self.assertNotIn(done.id, self.eng.jobs)
        self.assertNotIn(queued.id, self.eng.jobs)
        self.assertEqual(self.eng.pending, [other.id],
                         "another profile's place in the queue was disturbed")
        self.assertIn(other.id, self.eng.jobs)

    def test_every_status_goes_not_just_waiting(self):
        for i, status in enumerate(("queued", "done", "failed", "cancelled")):
            self.job("many", status, kind=("main" if i == 0 else "pass%d" % i))
        res = self.eng.delete_profile("many", "instagram", media=False)
        self.assertEqual(res["jobs_removed"], 4)
        self.assertFalse([j for j in self.eng.jobs.values() if j.username == "many"])
        self.assertEqual(self.eng.pending, [])

    def test_an_archived_removed_entry_is_dropped_too(self):
        """"Remove from list" archives a finished job; it must not resurface."""
        self.eng.removed = [{"id": "abc", "username": "Gone", "platform": "instagram"},
                            {"id": "def", "username": "keepme", "platform": "instagram"}]
        res = self.eng.delete_profile("gone", "instagram", media=False)
        self.assertEqual(res["archived_removed"], 1)
        self.assertEqual([r["username"] for r in self.eng.removed], ["keepme"])

    def test_the_other_platforms_job_is_untouched(self):
        ig = self.job("same", "done", platform="instagram")
        tt = self.job("same", "done", platform="tiktok")
        res = self.eng.delete_profile("same", "instagram", media=False)
        self.assertEqual(res["jobs_removed"], 1)
        self.assertIn(tt.id, self.eng.jobs, "deleting the Instagram one took TikTok")
        self.assertNotIn(ig.id, self.eng.jobs)


class MediaTests(DeleteCase):
    """The folder is MOVED to the system Trash -- and is recoverable there."""

    def setUp(self) -> None:
        super().setUp()
        # _to_desktop_trash shells out to gio/trash-put, which resolve the Trash
        # bin from HOME/XDG_DATA_HOME. Point both inside the sandbox for the
        # duration of the test so the bin this asserts on is the temp one.
        for key, value in (("HOME", str(self.sb.home)),
                           ("XDG_DATA_HOME", str(Path(self.sb.home) / ".local" / "share"))):
            old = os.environ.get(key)
            os.environ[key] = value
            self.addCleanup(self._restore_env, key, old)

    @staticmethod
    def _restore_env(key: str, old):
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old

    def test_media_moves_to_the_trash_and_the_folder_is_gone(self):
        if not any(Path(p).exists() for p in ("/usr/bin/gio", "/usr/bin/trash-put",
                                              "/home/linuxbrew/.linuxbrew/bin/trash-put")):
            self.skipTest("no trash tool on this machine")
        folder = self.profile_folder("trashme", files=3)
        expected = sum(1 for _ in folder.rglob("*") if _.is_file())
        self.job("trashme", "done")

        res = self.eng.delete_profile("trashme", "instagram")

        self.assertTrue(res["ok"], res)
        self.assertTrue(res["media_moved"])
        self.assertEqual(res["files"], expected, "the count shown in the dialog is wrong")
        self.assertGreater(res["bytes"], 0)
        self.assertFalse(folder.exists(), "the profile folder is still on disk")

        # Recoverable, not shredded: the bytes are in the Trash bin, under the
        # folder's own name. This is the assertion the feature exists for.
        binned = self.trash_files() / "trashme"
        self.assertTrue(binned.is_dir(),
                        "nothing named trashme under %s" % self.trash_files())
        recovered = [p for p in binned.rglob("*") if p.is_file()]
        self.assertEqual(len(recovered), expected)
        self.assertEqual(sum(p.stat().st_size for p in recovered),
                         res["bytes"], "the trashed copy is not the original bytes")

    def test_the_dialog_numbers_are_the_delete_numbers(self):
        """What the confirmation promises must be what the result reports."""
        self.profile_folder("countme", files=4)
        totals = self.eng.profile_totals("countme", "instagram")
        self.assertTrue(totals["has_folder"])
        res = self.eng.delete_profile("countme", "instagram")
        self.assertEqual(res["files"], totals["files"],
                         "the dialog promised a different file count")
        self.assertEqual(res["bytes"], totals["bytes"])

    def test_no_media_leaves_the_folder_alone(self):
        folder = self.profile_folder("keepme", files=2)
        self.job("keepme", "done")
        res = self.eng.delete_profile("keepme", "instagram", media=False)
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["media_moved"])
        self.assertTrue(folder.is_dir(), "media=False still moved the files")
        self.assertEqual(len(list(folder.iterdir())), 3)

    def test_a_deleted_profile_leaves_the_gallery_at_once(self):
        self.profile_folder("vanish", files=2)
        self.job("vanish", "done")
        self.assertIn("vanish", [p["username"] for p in self.eng.gallery_profiles()])
        self.eng.delete_profile("vanish", "instagram")
        self.assertNotIn("vanish", [p["username"] for p in self.eng.gallery_profiles()],
                         "the gallery kept listing a deleted profile")


class ExclusionTests(DeleteCase):
    """An exclusion is a memory, and it is what stops the accidental re-download."""

    def test_add_refuses_an_excluded_name(self):
        self.eng.delete_profile("neveragain", "instagram", media=False)
        added, rejected = self.eng.add(["neveragain"])
        self.assertEqual(added, [])
        self.assertEqual(len(rejected), 1)
        self.assertIn("deleted on purpose", rejected[0])
        self.assertIn("Excluded", rejected[0], "the refusal does not say how to undo it")

    def test_the_refusal_folds_case_and_a_leading_at(self):
        self.eng.delete_profile("neveragain", "instagram", media=False)
        added, rejected = self.eng.add(["@NeverAgain"])
        self.assertEqual(added, [])
        self.assertIn("deleted on purpose", rejected[0])

    def test_autocheck_skips_an_excluded_name(self):
        # The autocheck re-queues every ARCHIVED profile, so there has to be a
        # folder on disk for it to consider -- otherwise it proves nothing.
        self.profile_folder("neveragain", files=2)
        self.eng.delete_profile("neveragain", "instagram", media=False)
        self.assertTrue(self.profile_folder("neveragain", files=2).is_dir())

        res = self.eng.autocheck()

        self.assertEqual(res["queued"], 0, "autocheck queued a deleted profile")
        self.assertFalse([j for j in self.eng.jobs.values()
                          if j.username.lower() == "neveragain"])

    def test_an_extra_pass_is_refused_too(self):
        src = self.job("neveragain", "done")
        self.eng.delete_profile("neveragain", "instagram", media=False)
        # delete_profile removed the job, so re-add it to prove the guard itself
        # refuses rather than the "not in the list" check.
        self.eng.jobs[src.id] = src
        res = self.eng.add_extra_pass(src.id, "reels")
        self.assertFalse(res["ok"])
        self.assertIn("deleted on purpose", res["error"])

    def test_undo_makes_it_addable_again(self):
        self.eng.delete_profile("neveragain", "instagram", media=False)
        self.assertIn("neveragain", self.eng._excluded_set("instagram"))

        # What POST /api/exclusions/remove calls.
        self.assertTrue(self.db.restore("instagram", "neveragain"))

        self.assertNotIn("neveragain", self.eng._excluded_set("instagram"))
        added, rejected = self.eng.add(["neveragain"])
        self.assertEqual(rejected, [])
        self.assertEqual([j.username for j in added], ["neveragain"])

    def test_the_exclusion_is_platform_scoped(self):
        self.eng.delete_profile("same", "instagram", media=False)
        added, rejected = self.eng.add(["same"])
        self.assertEqual(added, [])
        self.assertTrue(rejected)
        tt_added, tt_skipped = self.eng.add_tiktok(["same"], count=3)
        self.assertEqual([j.username for j in tt_added], ["same"],
                         "an Instagram exclusion blocked the TikTok account")
        self.assertEqual(tt_skipped, [])

    def test_exclude_false_deletes_without_the_memory(self):
        self.job("temporary", "done")
        res = self.eng.delete_profile("temporary", "instagram",
                                      media=False, exclude=False)
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["excluded"])
        self.assertNotIn("temporary", self.eng._excluded_set("instagram"))
        added, _rejected = self.eng.add(["temporary"])
        self.assertEqual([j.username for j in added], ["temporary"])


class HttpDeleteTests(unittest.TestCase):
    """The three routes, through a real server, on a throwaway archive.

    This is the half an in-process test cannot prove: that the buttons have a
    URL to call, that the body it sends is the body the engine expects, and that
    the reply carries the numbers the toast reports.

    The queue is written into the sandbox's own ``state.json`` before the server
    starts, with a done job AND a queued one for the same person plus an
    unrelated queued job. It has to be written rather than downloaded: the
    sandbox has no instaloader session, so the Instagram worker refuses to start
    any job at all -- and a test that waited for a download would only prove
    that.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.sb = sandbox.Sandbox(seed_profile="deleteviahttp", seed_count=3)
        cls.sb.state_file.write_text(json.dumps({
            "settings": {},
            "output_root": str(cls.sb.output),
            "roots": [str(cls.sb.output)],
            "jobs": [
                {"id": "jobdone00001", "username": "deleteviahttp",
                 "status": "done", "kind": "main", "platform": "instagram",
                 "posts": 3},
                {"id": "jobqueued002", "username": "deleteviahttp",
                 "status": "queued", "kind": "tagged", "platform": "instagram"},
                {"id": "jobother0003", "username": "otherprofile",
                 "status": "queued", "kind": "main", "platform": "instagram"},
            ],
            "order": ["jobqueued002", "jobother0003"],
        }))
        cls.sb.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sb.stop()

    def test_delete_then_undo(self):
        folder = self.sb.output / "deleteviahttp"
        self.assertTrue(folder.is_dir())
        self.assertIn("deleteviahttp",
                      [p["username"] for p in self.sb.get("/api/gallery").json()
                       .get("profiles", [])])

        res = self.sb.post("/api/profile/delete",
                           {"profile": "deleteviahttp", "platform": "instagram",
                            "media": True, "exclude": True}).json()

        self.assertTrue(res["ok"], res)
        self.assertTrue(res["media_moved"])
        self.assertGreaterEqual(res["files"], 4)
        self.assertEqual(res["jobs_removed"], 2,
                         "the done job and the queued one must both go")
        self.assertFalse(folder.exists(), "the folder survived the delete")
        binned = Path(self.sb.home) / ".local" / "share" / "Trash" / "files" / "deleteviahttp"
        self.assertTrue(binned.is_dir(), "the files are not in the Trash bin")
        self.assertTrue([p for p in binned.rglob("*") if p.is_file()])

        # Gone from Waiting and Finished, and the unrelated job is untouched.
        st = self.sb.get("/api/state").json()
        names = [j["username"] for j in st["jobs"]]
        self.assertNotIn("deleteviahttp", names, "the job is still in the queue")
        self.assertIn("otherprofile", names)
        self.assertNotIn("deleteviahttp",
                         [p["username"] for p in self.sb.get("/api/gallery").json()
                          .get("profiles", [])])

        # Listed as excluded, and refused by a paste.
        ex = self.sb.get("/api/exclusions").json()
        self.assertIn("deleteviahttp", [p["profile"] for p in ex["profiles"]])
        self.assertEqual(ex["profiles"][0]["platform"], "instagram")
        again = self.sb.post("/api/add", {"profiles": ["deleteviahttp"]}).json()
        self.assertEqual(again["added"], [])
        self.assertIn("deleted on purpose", again["rejected"][0])

        # Undo: the name is addable again.
        undo = self.sb.post("/api/exclusions/remove",
                            {"profile": "deleteviahttp",
                             "platform": "instagram"}).json()
        self.assertTrue(undo["ok"], undo)
        ex = self.sb.get("/api/exclusions").json()
        self.assertNotIn("deleteviahttp", [p["profile"] for p in ex["profiles"]])
        back = self.sb.post("/api/add", {"profiles": ["deleteviahttp"]}).json()
        self.assertEqual(back["added"], ["deleteviahttp"])

    def test_a_missing_profile_is_a_clean_refusal(self):
        res = self.sb.post("/api/profile/delete", {"media": True}).json()
        self.assertFalse(res["ok"])
        self.assertIn("no profile", res["error"])

    def test_an_unknown_name_does_not_error(self):
        res = self.sb.post("/api/profile/delete",
                           {"profile": "nobodyhere", "platform": "instagram"}).json()
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["media_moved"])
        self.assertEqual(res["jobs_removed"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
