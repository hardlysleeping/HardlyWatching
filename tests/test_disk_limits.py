"""When the "drive is filling up" warning fires.

The warning is meant to be a heads-up a little before the STOP point the person
set, so it has to move with that limit. It used to be capped at a fixed 90%,
which meant a 99% limit warned at 90 -- nine points of "nearly full" for a drive
that was not nearly at the limit its owner chose.

No server, no archive: this is arithmetic over engine._DISK_LIMITS.
"""

from __future__ import annotations

import collections
import unittest

import engine


class DiskWarnTests(unittest.TestCase):
    """warn = five points below the limit; 100 when there is no limit."""

    def tearDown(self) -> None:
        # The guard is module state: put it back the way the app starts.
        engine.apply_disk_limits(stop_pct=engine.DISK_STOP_PCT, off=False)

    def test_warn_sits_five_points_below_the_limit(self):
        for stop, want in ((99, 94.0), (95, 90.0), (60, 55.0), (85, 80.0)):
            engine.apply_disk_limits(stop_pct=stop, off=False)
            self.assertAlmostEqual(engine._DISK_LIMITS["warn"], want, places=1,
                                   msg="limit %s should warn at %s" % (stop, want))
            self.assertEqual(engine._DISK_LIMITS["stop"], float(stop))

    def test_a_limit_at_the_top_is_clamped_then_followed(self):
        engine.apply_disk_limits(stop_pct=100, off=False)
        self.assertEqual(engine._DISK_LIMITS["stop"], 99.9)
        self.assertAlmostEqual(engine._DISK_LIMITS["warn"], 94.9, places=1)

    def test_a_floor_of_fifty_keeps_a_low_limit_warning(self):
        engine.apply_disk_limits(stop_pct=52, off=False)
        self.assertAlmostEqual(engine._DISK_LIMITS["warn"], 50.0, places=1)

    def test_no_limit_means_no_warning(self):
        engine.apply_disk_limits(stop_pct=99, off=True)
        self.assertEqual(engine._DISK_LIMITS["warn"], 100.0)
        # And the level really is "ok" on a drive that is nearly full, because
        # a warning with the guard switched off is the guard talking.
        usage = collections.namedtuple("usage", "total used free")
        total = 1000 * 2 ** 30
        old = engine.shutil.disk_usage
        self.addCleanup(setattr, engine.shutil, "disk_usage", old)
        engine.shutil.disk_usage = lambda _p: usage(total, int(total * 0.995),
                                                    total - int(total * 0.995))
        got = engine.disk_usage("/nonexistent-probe")
        assert got is not None, "a stubbed disk_usage must answer"
        self.assertEqual(got["level"], "ok")

        # With a limit on, the same drive is a stop, not a warning.
        engine.apply_disk_limits(stop_pct=99, off=False)
        got = engine.disk_usage("/nonexistent-probe")
        assert got is not None, "a stubbed disk_usage must answer"
        self.assertEqual(got["level"], "stop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
