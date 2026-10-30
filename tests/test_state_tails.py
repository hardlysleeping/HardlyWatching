"""The queue file stays small: a job's output lives in its own file.

state.json had grown to 18.6 MB, 15 MB of it the saved downloader output of
every finished job (2,455 of them, about 1,000 failed and therefore worth
reading). Every save dumped the whole thing, fsynced it, then read it back and
parsed it to prove it was valid -- and a running download triggers a save about
once a second, so every API call queued behind a 19 MB rewrite.

What is pinned here:

  * a finished job's output is written to ``logs/<id>.txt`` and is NOT in
    state.json;
  * ``job_log()`` still returns it, before and after a restart;
  * the line count survives the restart, so the card can still say
    "log (N lines)" without the lines being loaded;
  * a live job's output IS in state.json, because that is what a crash has to
    keep;
  * saving is coalesced while something is running, and the trailing save still
    writes the last update.

In-process against a sandbox, so the live queue file and the real logs are
never touched.
"""

from __future__ import annotations

import json
import time
import unittest

try:  # imported as `tests.test_state_tails` (discover from the repo root)
    from . import sandbox
except ImportError:  # imported as a top-level module (discover -s tests)
    import sandbox


class StateTailCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.sb = sandbox.Sandbox(seed_profile=None)
        cls.sb.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sb.stop()

    def engine(self, tag: str):
        import engine as E
        import profile_db as pdb

        old_state, old_shared, old_cache = E.STATE_FILE, E._shared_db, E._cache
        old_pdb = pdb._shared_db
        self.addCleanup(setattr, E, "STATE_FILE", old_state)
        self.addCleanup(setattr, E, "_shared_db", old_shared)
        self.addCleanup(setattr, E, "_cache", old_cache)
        self.addCleanup(setattr, pdb, "_shared_db", old_pdb)

        root = self.sb.root / ("tails-" + tag)
        root.mkdir(parents=True, exist_ok=True)
        db = pdb.ProfileDB(self.sb.root / ("tails-%s.db" % tag))
        self.addCleanup(db.close)
        E.STATE_FILE = self.sb.root / ("tails-%s-state.json" % tag)
        E._shared_db = lambda: db
        pdb._shared_db = lambda: db
        E._cache = E._Cache(self.sb.root / ("tails-%s-cache.db" % tag))
        eng = E.Engine(output_root=root)
        eng.roots = [root]
        return eng, E

    def a_failed_job(self, eng, E, name="somebody"):
        job = E.Job(username=name, platform="instagram")
        job.status = "failed"
        job.finished_at = time.time()
        eng.jobs[job.id] = job
        for line in ("first line", "second line", "third line"):
            eng._log(job, line)
        return job

    def test_a_finished_jobs_output_leaves_the_queue_file(self):
        eng, E = self.engine("spill")
        job = self.a_failed_job(eng, E)
        eng.save_state()

        raw = E.STATE_FILE.read_text()
        self.assertNotIn("second line", raw,
                         "a finished job's output is still in state.json")
        spilled = E.read_tail(job.id)
        self.assertEqual(spilled, ["first line", "second line", "third line"],
                         "the output was not written to its own file")
        # And it is still readable through the endpoint the log panel uses.
        got = eng.job_log(job.id)
        self.assertTrue(got["ok"])
        self.assertEqual(len(got["tail"]), 3)

    def test_a_live_job_keeps_its_output_in_the_queue_file(self):
        eng, E = self.engine("live")
        job = E.Job(username="running", platform="instagram")
        job.status = "running"
        eng.jobs[job.id] = job
        eng._log(job, "downloading something")
        eng.save_state()
        raw = E.STATE_FILE.read_text()
        self.assertIn("downloading something", raw,
                      "a running job's output must survive a crash")

    def test_the_line_count_survives_a_restart(self):
        eng, E = self.engine("restart")
        job = self.a_failed_job(eng, E)
        jid = job.id
        eng.save_state()

        # A fresh Engine over the same state file, as a restart would build.
        again = E.Engine(output_root=eng.output_root)
        again.roots = list(eng.roots)
        loaded = again.jobs.get(jid)
        self.assertIsNotNone(loaded, "the job did not come back")
        self.assertEqual(loaded.tail, [], "the lines were persisted anyway")
        self.assertEqual(loaded.tail_len, 3, "the line count was lost")
        payload = again._job_summary(loaded)
        self.assertEqual(payload["tail_len"], 3)
        self.assertEqual(payload["tail"], [])
        got = again.job_log(jid)
        self.assertEqual(len(got["tail"]), 3,
                         "the log panel lost the output after a restart")

    def test_saving_is_coalesced_while_something_is_running(self):
        eng, E = self.engine("coalesce")
        job = E.Job(username="busy", platform="instagram")
        job.status = "running"
        eng.jobs[job.id] = job

        eng._last_save = 0.0
        eng.save_state()                      # first save goes through
        first = E.STATE_FILE.stat().st_mtime_ns
        eng.save_state()                      # immediately again: coalesced
        self.assertEqual(E.STATE_FILE.stat().st_mtime_ns, first,
                         "a second save rewrote the file inside the gap")
        self.assertIsNotNone(eng._save_timer,
                             "no trailing save was armed, the update could be lost")
        # The trailing save lands once the gap has passed.
        time.sleep(E.SAVE_MIN_GAP + 0.4)
        self.assertGreater(E.STATE_FILE.stat().st_mtime_ns, first,
                           "the coalesced update never reached disk")

    def test_nothing_running_still_saves_immediately(self):
        eng, E = self.engine("idle")
        eng._last_save = 0.0
        eng.save_state()
        first = E.STATE_FILE.stat().st_mtime_ns
        eng.save_state()
        self.assertGreater(E.STATE_FILE.stat().st_mtime_ns, first,
                           "an idle save was throttled")


if __name__ == "__main__":
    unittest.main()
