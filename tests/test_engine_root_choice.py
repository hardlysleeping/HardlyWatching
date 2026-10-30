"""Which drive a new download is written to, and what stops one profile splitting.

The downloads folder used to be the only place a job could write, so a full
drive stopped the queue: every job waited for room on a disk nobody was going
to free. The archive folders were already readable, so the fix is on the write
side -- pick a root with room, pin it on the job, and write there.

The rules this file pins down:

  * the downloads folder is used while it has room, so the ordinary case is
    unchanged;
  * when it is at its stop limit, a job is pinned to another configured root
    that is THERE and has room;
  * an unplugged root is absent, not empty -- never chosen;
  * one profile never splits across two drives: an archive already on a root
    keeps writing there, and the block is reported the way it always was;
  * a follow-up pass (tagged, reels) inherits the root its parent was pinned to;
  * the choice survives a restart, because it lives on the job in state.json.

Fake drives, real code paths. "Full" is stubbed at ``shutil.disk_usage`` -- the
same seam the disk-guard tests use -- so nothing here has to fill a real disk,
and the live archive, state.json and profiles.db are never touched: the sandbox
gives a throwaway HOME and every path is repointed at it before an Engine is
built.
"""

from __future__ import annotations

import collections
import unittest
from pathlib import Path

try:  # imported as `tests.test_engine_root_choice` (discover from the repo root)
    from . import sandbox
except ImportError:  # imported as a top-level module (discover -s tests)
    import sandbox

GB = 2 ** 30
Usage = collections.namedtuple("usage", "total used free")

# One fake volume, 100 GB, described only by how full it is.
VOLUME_GB = 100


def fake_usage(levels: dict[str, float]):
    """A stand-in for shutil.disk_usage() that reports per-root fullness.

    `levels` maps a root path to the percentage used. A path under a root gets
    that root's volume; anything else gets a roomy default. Real drives are
    never consulted, so a test cannot be influenced by how full this machine
    happens to be.
    """
    def usage(path) -> Usage:
        p = str(path)
        best = None
        for root, pct in levels.items():
            if p == root or p.startswith(root.rstrip("/") + "/"):
                if best is None or len(root) > len(best):
                    best = root
        pct = levels[best] if best is not None else 40.0
        total = VOLUME_GB * GB
        used = int(total * pct / 100.0)
        return Usage(total, used, total - used)
    return usage


class RootChoiceCase(unittest.TestCase):
    """Three fake drives, a throwaway state file, and an Engine over them."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sb = sandbox.Sandbox(seed_profile=None)
        cls.sb.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sb.stop()

    def setUp(self) -> None:
        # The app's own paths are repointed BEFORE the Engine is built, so this
        # test can never read or write the real state.json / profiles.db.
        import engine as E
        import profile_db as pdb

        tag = self.id().rsplit(".", 1)[-1]
        self.E = E
        self.base = self.sb.root / ("roots-" + tag)
        self.base.mkdir(parents=True, exist_ok=True)

        old_state, old_shared, old_cache = E.STATE_FILE, E._shared_db, E._cache
        old_pdb = pdb._shared_db
        self.addCleanup(setattr, E, "STATE_FILE", old_state)
        self.addCleanup(setattr, E, "_shared_db", old_shared)
        self.addCleanup(setattr, E, "_cache", old_cache)
        self.addCleanup(setattr, pdb, "_shared_db", old_pdb)
        E._SIG_CACHE.clear()
        self.addCleanup(E._SIG_CACHE.clear)

        self.db = pdb.ProfileDB(self.base / "profiles.db")
        self.addCleanup(self.db.close)
        self.state_file = self.base / "state.json"
        E.STATE_FILE = self.state_file
        E._shared_db = lambda: self.db
        pdb._shared_db = lambda: self.db
        E._cache = E._Cache(self.base / "cache.db")

        # The stop limit is module state shared by every writer.
        E.apply_disk_limits(stop_pct=95, off=False)
        self.addCleanup(E.apply_disk_limits, 95, False)

        self.a = self.drive("driveA")
        self.b = self.drive("driveB")
        self.c = self.drive("driveC")
        self.levels: dict[str, float] = {}
        self.patch_disk()

        self.eng = E.Engine(output_root=self.a)
        self.eng.roots = [self.a, self.b]
        # add() kicks a worker; a worker in THIS process would run the real
        # downloader. Nothing here needs one to run.
        self.eng._kick = lambda: None
        self.addCleanup(self.eng.save_state)

    # ------------------------------------------------------------------ helpers

    def drive(self, name: str) -> Path:
        """A root shaped like this app's own: `<drive>/instagram`."""
        root = self.base / name / "instagram"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def patch_disk(self) -> None:
        """Point the disk guard at `self.levels` for the rest of the test."""
        old = self.E.shutil.disk_usage
        self.addCleanup(setattr, self.E.shutil, "disk_usage", old)
        self.E.shutil.disk_usage = fake_usage(self.levels)

    def fullness(self, **by_drive) -> None:
        """Set how full each fake drive is, by name: fullness(driveA=96).

        Mutated in place on purpose: the stubbed disk_usage holds this very
        dict, so rebinding it would leave the stub reading the old numbers.
        """
        drives = {"driveA": self.a, "driveB": self.b, "driveC": self.c}
        self.levels.clear()
        self.levels.update({str(drives[k]): v for k, v in by_drive.items()})

    def added(self, name: str = "someone"):
        """Queue one Instagram profile and return its job."""
        jobs, _rejected = self.eng.add([name])
        self.assertTrue(jobs, "add() queued nothing")
        return jobs[0]

    def archived(self, root: Path, name: str) -> Path:
        """A profile folder with one file in it, as a finished run leaves it."""
        folder = root / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "2024-01-01_12-00-00_UTC.jpg").write_bytes(b"x" * 32)
        return folder


class ChoosesWhereToWrite(RootChoiceCase):
    """pick_root: the downloads folder while it has room, then the others."""

    def test_downloads_folder_is_used_while_it_has_room(self):
        self.fullness(driveA=40, driveB=40)

        job = self.added("alpha")

        # Not pinned: an unpinned job follows the downloads folder, which is
        # exactly what every job did before overflow existed.
        self.assertEqual(job.root, "")
        self.assertEqual(self.eng.job_root(job), self.a)
        self.assertEqual(self.eng.pick_root(), self.a)

    def test_full_downloads_folder_pins_the_next_root_with_room(self):
        self.fullness(driveA=96, driveB=40)

        job = self.added("beta")

        self.assertEqual(job.root, str(self.b))
        self.assertEqual(self.eng.job_root(job), self.b)
        self.assertIn(str(self.b), job.message, "the card should say where it goes")
        # The folder is created by the writer, not by choosing -- picking must
        # not leave half-made trees behind when the job is later removed.
        self.assertFalse((self.b / "beta").exists())

    def test_every_root_full_queues_the_job_and_reports_the_block(self):
        self.fullness(driveA=96, driveB=97)

        job = self.added("gamma")

        self.assertEqual(job.root, "")
        self.assertIn(job.id, self.eng.pending, "the job was not left queued")
        self.assertEqual(job.status, "queued")
        # The message the app already shows for a full drive, unchanged.
        block = self.eng.disk_blocks(job.root)
        self.assertIn("at the 95% limit", block)
        self.assertIn("% used", block)

    def test_an_archived_profile_keeps_its_own_drive(self):
        self.archived(self.a, "delta")
        self.fullness(driveA=40, driveB=40)

        job = self.added("delta")

        # Its files are on A: writing to B would split one profile in two.
        self.assertEqual(job.root, str(self.a))
        self.assertEqual(self.eng.job_root(job), self.a)

    def test_an_archived_profile_leaves_a_full_drive_for_one_with_room(self):
        # The one exception, and it is deliberate: a profile on a drive at its
        # stop limit continues on a drive that has room instead of waiting for
        # space that is not coming. What is already archived stays on A.
        self.archived(self.a, "delta")
        self.fullness(driveA=96, driveB=40)

        job = self.added("delta")

        self.assertEqual(job.root, str(self.b))
        self.assertIn(str(self.b), job.message)
        self.assertTrue((self.a / "delta").is_dir(), "nothing may be moved off A")

    def test_an_unplugged_root_is_never_chosen(self):
        gone = self.base / "unplugged" / "instagram"
        self.eng.roots = [self.a, gone, self.b]
        self.fullness(driveA=96, driveB=40)
        self.levels[str(gone)] = 10.0      # emptiest drive in the world...

        # ...but it is not there, so it is absent, not empty.
        self.assertEqual(self.eng.pick_root(), self.b)
        self.assertEqual(self.added("epsilon").root, str(self.b))

        # And with the only other root full too, nothing is picked rather than
        # a missing folder being handed a download.
        self.fullness(driveA=96, driveB=97)
        self.levels[str(gone)] = 10.0
        self.assertIsNone(self.eng.pick_root())
        self.assertEqual(self.added("zeta").root, "")

    def test_a_follow_up_pass_inherits_the_parent_root(self):
        self.fullness(driveA=96, driveB=40)
        main = self.added("eta")

        res = self.eng.add_extra_pass(main.id, "tagged")

        self.assertTrue(res["ok"])
        child = next(j for j in self.eng.jobs.values() if j.kind == "tagged")
        self.assertEqual(child.root, main.root)
        self.assertEqual(child.root, str(self.b))

    def test_a_tiktok_job_is_pinned_the_same_way(self):
        self.fullness(driveA=96, driveB=40)

        jobs, _skipped = self.eng.add_tiktok(["ttuser"], count=5)

        self.assertTrue(jobs)
        self.assertEqual(jobs[0].root, str(self.b))
        # Its folder lives beside the Instagram one and can be created there.
        ready, why = self.eng.root_ready(self.b, "tiktok")
        self.assertTrue(ready, why)

    def test_choosing_never_changes_the_configuration(self):
        self.fullness(driveA=96, driveB=40)
        roots_before = list(self.eng.roots)
        output_before = self.eng.output_root

        for _ in range(3):
            self.eng.pick_root()
        self.added("theta")

        self.assertEqual(self.eng.roots, roots_before)
        self.assertEqual(self.eng.output_root, output_before)

    def test_the_same_folder_spelled_two_ways_is_one_root(self):
        # A symlinked spelling of drive B, which is how /home and /var/home
        # relate: the same place under two names.
        link = self.base / "driveB-link"
        try:
            link.symlink_to(self.b, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks not available here")
        self.archived(self.b, "iota")
        self.eng.roots = [self.a, link]
        self.fullness(driveA=96, driveB=40)

        job = self.added("iota")

        # Found through the link, and it is the same place as drive B.
        self.assertTrue(self.E._same(Path(job.root), self.b))
        self.assertEqual(self.eng.job_root(job).resolve(), self.b.resolve())

    def test_the_pinned_root_survives_a_restart(self):
        self.fullness(driveA=96, driveB=40)
        job = self.added("kappa")
        self.assertEqual(job.root, str(self.b))
        self.eng.save_state()

        reloaded = self.E.Engine(output_root=self.a)
        back = reloaded.jobs.get(job.id)
        if back is None:
            self.fail("the job was not restored from state.json")

        self.assertEqual(back.root, str(self.b))

    def test_roots_status_says_who_takes_the_overflow(self):
        self.fullness(driveA=96, driveB=40)

        status = self.eng.roots_status()

        self.assertFalse(status[0]["overflow"], "a full downloads root claims room")
        self.assertIn("limit", status[0]["overflow_reason"])
        self.assertTrue(status[1]["overflow"])
        self.assertEqual(status[1]["overflow_reason"], "")

    def test_a_missing_root_is_reported_as_absent_not_full(self):
        gone = self.base / "gone" / "instagram"

        ready, why = self.eng.root_ready(gone)

        self.assertFalse(ready)
        self.assertEqual(why, "not mounted")


class MovesAJobThatCannotRun(RootChoiceCase):
    """_reroot: the pinned drive changed its mind between queueing and running."""

    def pinned_job(self, name: str = "lambda"):
        """A job pinned to drive B, as if B had room when it was queued."""
        self.fullness(driveA=96, driveB=40)
        job = self.added(name)
        self.assertEqual(job.root, str(self.b))
        return job

    def test_a_full_pinned_drive_hands_the_job_to_one_with_room(self):
        job = self.pinned_job()
        self.eng.roots = [self.a, self.b, self.c]
        self.fullness(driveA=96, driveB=96, driveC=40)

        note = self.eng._reroot(job)

        self.assertTrue(note, "the switch was not reported")
        self.assertEqual(job.root, str(self.c))
        self.assertEqual(job.message, note)
        self.assertIn(str(self.c), note)
        self.assertIn("switched to", note)
        self.assertTrue(any("writing to" in line for line in job.tail),
                        "the switch should be in the job's log too")

    def test_a_profile_already_on_the_pinned_drive_stays_while_it_has_room(self):
        job = self.pinned_job("mu")
        self.archived(self.b, "mu")
        self.eng.roots = [self.a, self.b, self.c]
        self.fullness(driveA=40, driveB=40, driveC=40)

        note = self.eng._reroot(job)

        self.assertEqual(note, "")
        self.assertEqual(job.root, str(self.b), "one profile split across drives")

    def test_a_profile_already_on_a_full_pinned_drive_continues_elsewhere(self):
        # Deliberate, and the reason a half-finished profile is not stuck behind
        # a drive it has outgrown: what is on B stays on B, the rest goes to C.
        job = self.pinned_job("mu")
        self.archived(self.b, "mu")
        self.eng.roots = [self.a, self.b, self.c]
        self.fullness(driveA=96, driveB=96, driveC=40)

        note = self.eng._reroot(job)

        self.assertTrue(note)
        self.assertEqual(job.root, str(self.c))
        self.assertTrue((self.b / "mu").is_dir(), "nothing may be moved off B")

    def test_a_job_that_never_ran_leaves_a_drive_that_is_gone(self):
        job = self.pinned_job("nu")
        gone = self.b
        job.root = str(gone)
        self.eng.roots = [self.a, self.b]
        self.fullness(driveA=40, driveB=40)
        # The drive is unplugged: the folder is not there any more.
        for entry in sorted(gone.iterdir()):
            if entry.is_dir():
                entry.rmdir()
        gone.rmdir()
        self.assertFalse(gone.exists())

        note = self.eng._reroot(job)

        self.assertTrue(note)
        self.assertEqual(job.root, str(self.a))

    def test_a_job_that_already_wrote_stays_put_when_its_drive_is_gone(self):
        job = self.pinned_job("xi")
        job.posts = 7
        gone = self.b
        job.root = str(gone)
        self.fullness(driveA=40, driveB=40)
        for entry in sorted(gone.iterdir()):
            if entry.is_dir():
                entry.rmdir()
        gone.rmdir()

        note = self.eng._reroot(job)

        # It may well be archived on that drive, so moving it would make a
        # second copy the moment the drive is plugged back in.
        self.assertEqual(note, "")
        self.assertEqual(job.root, str(gone))

    def test_a_job_with_no_root_is_left_to_the_downloads_folder(self):
        self.fullness(driveA=40, driveB=40)
        job = self.added("omicron")
        self.assertEqual(job.root, "")

        self.assertEqual(self.eng._reroot(job), "")
        self.assertEqual(job.root, "")

    def test_nothing_is_moved_when_no_other_root_has_room(self):
        job = self.pinned_job("pi")
        self.eng.roots = [self.a, self.b]
        self.fullness(driveA=96, driveB=96)

        self.assertEqual(self.eng._reroot(job), "")
        self.assertEqual(job.root, str(self.b))


class WritesWhereItWasPinned(RootChoiceCase):
    """The write side really uses the pin -- not just a field on a dataclass."""

    def test_the_profile_folder_is_created_on_the_pinned_drive(self):
        self.fullness(driveA=96, driveB=40)
        job = self.added("rho")
        self.assertEqual(job.root, str(self.b))

        # No downloader: the command line and the process are stubbed, so what
        # is under test is only which folder the run writes into.
        seen = []
        self.eng._exec = lambda job, cmd, target_dir: (seen.append(Path(target_dir)), "ok")[1]
        self.eng._count_media = lambda target_dir: 3
        self.eng._is_partial = lambda job, target_dir: (False, "")

        self.eng._process(job)

        self.assertEqual(seen, [self.b / "rho"])
        self.assertTrue((self.b / "rho").is_dir(),
                        "the folder was not created on the pinned drive")
        self.assertFalse((self.a / "rho").exists(),
                         "the profile was also written to the downloads folder")


if __name__ == "__main__":
    unittest.main(verbosity=2)
