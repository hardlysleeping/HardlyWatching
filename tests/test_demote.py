"""Moving a job to the back of its platform's queue, running or not.

The running case is the one that needs care: a download cannot be re-ordered,
so it has to be stopped, and the run's own exit path marks the job cancelled.
`demote()` records the intent and `_finish_demoted()` corrects the record once
the run is over. Both halves are exercised here without a child process.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class DemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hw-demote-")
        self.env = {}
        for key, name in (("INSTA_STATE", "state.json"), ("INSTA_OUTPUT", "out"),
                          ("INSTA_PROFILES_DB", "profiles.db"),
                          ("INSTA_CACHE_DB", "cache.db")):
            self.env[key] = os.environ.get(key)
            os.environ[key] = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(self.tmp, "out"), exist_ok=True)
        import engine
        self.engine_mod = engine
        self.eng = engine.Engine()
        # Three waiting jobs plus one that is downloading right now.
        self.jobs = {}
        for name, status in (("one", "queued"), ("two", "queued"),
                             ("three", "queued"), ("live", "running")):
            job = engine.Job(name)
            job.status = status
            job.platform = "instagram"
            self.jobs[job.id] = job
        self.eng.jobs = self.jobs
        by_name = {j.username: j.id for j in self.jobs.values()}
        self.eng.pending = [by_name["one"], by_name["two"], by_name["three"]]
        self.eng.current["instagram"] = "live"

    def tearDown(self):
        for key, old in self.env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _by_name(self, name):
        return next(j for j in self.eng.jobs.values() if j.username == name)

    def test_a_running_job_is_stopped_and_remembered(self):
        job = self._by_name("live")
        ok, msg = self.eng.demote(job.id)
        self.assertTrue(ok, msg)
        # The worker is told to abandon this run, and only this platform's.
        self.assertTrue(self.eng.cancel_current["instagram"])
        self.assertFalse(self.eng.cancel_current["tiktok"])
        # Remembered, because the run itself will mark the job cancelled later.
        self.assertIn(job.id, self.eng.demote_ids)
        self.assertIsNone(self.eng.current["instagram"])
        self.assertIn("back", job.message)

    def test_the_exit_path_turns_it_back_into_a_waiting_job_at_the_end(self):
        job = self._by_name("live")
        self.eng.demote(job.id)
        # What the run's own exit path does before the correction lands.
        job.status = "cancelled"
        job.attempts = 1
        self.eng.pending.append(job.id)
        self.eng._finish_demoted("instagram", job)
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.attempts, 0, "being moved is not a failed attempt")
        self.assertEqual(job.error, "")
        # Behind everything still waiting, and nothing else moved.
        self.assertEqual(self.eng.pending[-1], job.id)
        self.assertEqual([self.eng.jobs[j].username for j in self.eng.pending],
                         ["one", "two", "three", "live"])
        self.assertNotIn(job.id, self.eng.demote_ids)
        # The next profile must start at once, not after the pacing gap.
        self.assertTrue(self.eng._skip_gap["instagram"])

    def test_a_waiting_job_just_moves_to_the_back(self):
        job = self._by_name("one")
        ok, msg = self.eng.demote(job.id)
        self.assertTrue(ok, msg)
        self.assertEqual([self.eng.jobs[j].username for j in self.eng.pending],
                         ["two", "three", "one"])
        self.assertNotIn(job.id, self.eng.demote_ids)
        self.assertFalse(self.eng.cancel_current["instagram"])

    def test_a_finished_job_is_refused(self):
        job = self._by_name("two")
        job.status = "done"
        ok, msg = self.eng.demote(job.id)
        self.assertFalse(ok)
        self.assertIn("done", msg)
        self.assertEqual([self.eng.jobs[j].username for j in self.eng.pending],
                         ["one", "two", "three"])

    def test_an_unknown_id_is_refused(self):
        ok, msg = self.eng.demote("nope")
        self.assertFalse(ok)
        self.assertIn("no such job", msg)

    def test_tiktok_is_its_own_queue(self):
        job = self._by_name("three")
        job.platform = "tiktok"
        job.status = "running"
        self.eng.current["tiktok"] = "three"
        ok, _ = self.eng.demote(job.id)
        self.assertTrue(ok)
        # Only TikTok's run is stopped; an Instagram download is untouched.
        self.assertTrue(self.eng.cancel_current["tiktok"])
        self.assertFalse(self.eng.cancel_current["instagram"])


if __name__ == "__main__":
    unittest.main()
