"""A paused queue must not rewrite the state file every second.

The Instagram worker, while paused or cooling down, called save_state() on every
pass of its loop -- once a second, forever. Each save is a full dump of the
queue, an fsync and a re-parse to prove the file is valid, which is a few
hundred milliseconds of the one interpreter every HTTP request shares. A paused
queue therefore spent much of its life rewriting a file nobody read: the
countdown the UI shows comes from `cooldown_until` in memory.

What is pinned here:

  * `_save_heartbeat` saves once and then holds off for its gap;
  * it saves again once the gap has passed (the file still gets freshened);
  * the two worker loops call the heartbeat, not save_state, while waiting.
"""

from __future__ import annotations

import inspect
import time
import unittest

import engine as E


class Fake:
    """Just enough object to carry the method and count the saves."""

    _save_heartbeat = E.Engine._save_heartbeat

    def __init__(self) -> None:
        self.saves = 0
        self._last_heartbeat = 0.0

    def save_state(self) -> None:
        self.saves += 1


class HeartbeatCase(unittest.TestCase):

    def test_it_saves_once_then_waits_out_the_gap(self):
        f = Fake()
        for _ in range(20):          # twenty passes of a one-second loop
            f._save_heartbeat()
        self.assertEqual(f.saves, 1,
                         "the heartbeat rewrote the state file on every pass")

    def test_it_saves_again_after_the_gap(self):
        f = Fake()
        f._save_heartbeat(gap=0.0)
        f._save_heartbeat(gap=0.0)
        self.assertEqual(f.saves, 2)

    def test_the_gap_is_measured_from_the_last_save(self):
        f = Fake()
        f._save_heartbeat(gap=60.0)
        f._last_heartbeat = time.time() - 61.0
        f._save_heartbeat(gap=60.0)
        self.assertEqual(f.saves, 2)

    def test_the_waiting_loops_use_the_heartbeat(self):
        # The regression was a literal save_state() call in the paused and
        # cooling branches; a source check keeps it from creeping back.
        src = inspect.getsource(E.Engine._run)
        self.assertIn("_save_heartbeat()", src)
        self.assertNotIn("save_state()  # keep the UI countdown fresh", src)


if __name__ == "__main__":
    unittest.main()
