"""A rate-limit pause may not clear itself unless the user said it could.

The 429 path sets ``paused`` and ``cooldown_until`` together, and the worker
used to drop the pause the instant the countdown hit zero -- straight back into
the same limit, with no way to say "not yet". ``hold_after_limit`` (on by
default) makes the end of the cooldown a permission rather than an order:
requests are allowed again, the queue is not resumed until someone presses
Resume.

These run the engine in a child process with every path pointed inside a temp
dir, because ``engine`` resolves its state, database and output paths from the
environment at import time. Nothing here touches the real app, a real archive,
or the network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

# Run in the child: four cases and one persistence round-trip, printed as JSON.
# Keeping it in one process is deliberate -- each engine costs an import and a
# state load, and the cases are cheap once it is up.
PROBE = textwrap.dedent('''
    import json, os, sys, time
    from pathlib import Path
    sys.path.insert(0, os.environ["HWA_REPO"])
    import engine

    e = engine.Engine(output_root=Path(os.environ["INSTA_OUTPUT"]))
    out = {}

    def arm(hold, cooldown=-1, user=False):
        """Put the engine in a pause: hold on/off, cooldown past/future, user/limit."""
        e.settings.hold_after_limit = hold
        e.paused = True
        e._limit_paused = not user
        e.cooldown_until = time.time() + cooldown

    arm(True)
    out["hold_on_released"] = e._release_limit_pause()
    out["hold_on_paused"] = e.paused
    out["hold_on_wait"] = e.wait_reason("instagram")

    arm(False)
    out["hold_off_released"] = e._release_limit_pause()
    out["hold_off_paused"] = e.paused
    out["hold_off_wait"] = e.wait_reason("instagram")

    arm(False, cooldown=300)
    out["cooling_released"] = e._release_limit_pause()
    out["cooling_paused"] = e.paused
    out["cooling_wait"] = e.wait_reason("instagram")["kind"]

    arm(False, user=True)
    out["user_released"] = e._release_limit_pause()
    out["user_paused"] = e.paused

    arm(True)                      # a held pause, as it would be at shutdown
    e.save_state()
    saved = json.loads(Path(os.environ["INSTA_STATE"]).read_text())
    out["saved"] = {k: saved.get(k)
                    for k in ("limit_paused", "user_paused", "cooldown_until")}
    print(json.dumps(out))
''')


def _run_child(root: Path) -> dict:
    """Run PROBE with every path the engine can write to inside `root`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("INSTA_")}
    env.update({
        "HOME": str(root),
        "XDG_CONFIG_HOME": str(root / ".config"),
        "XDG_CACHE_HOME": str(root / ".cache"),
        "INSTA_STATE": str(root / "state.json"),
        "INSTA_CACHE_DB": str(root / "cache.db"),
        "INSTA_OUTPUT": str(root / "out"),
        "INSTA_THUMBS": str(root / "thumbs"),
        "INSTA_AVATARS": str(root / "avatars"),
        "HWA_REPO": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
    })
    (root / "out").mkdir(parents=True, exist_ok=True)
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=str(REPO_ROOT),
                            env=env, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError("child failed (%d): %s"
                             % (result.returncode, result.stderr[-3000:]))
    return json.loads(result.stdout.strip().splitlines()[-1])


class RateLimitHoldTests(unittest.TestCase):
    """The decision itself, in the four states the queue can be in."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="hwa-hold-")
        cls.result = _run_child(Path(cls._tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_hold_keeps_the_queue_paused_after_the_cooldown(self):
        r = self.result
        self.assertFalse(r["hold_on_released"],
                         "an elapsed cooldown resumed the queue on its own")
        self.assertTrue(r["hold_on_paused"])

    def test_a_held_pause_says_so_instead_of_looking_stuck(self):
        wait = self.result["hold_on_wait"]
        self.assertTrue(wait.get("held"))
        self.assertEqual(wait["label"], "rate limit over")
        self.assertEqual(wait["remaining"], 0)

    def test_with_hold_off_the_old_behaviour_is_unchanged(self):
        r = self.result
        self.assertTrue(r["hold_off_released"])
        self.assertFalse(r["hold_off_paused"])
        self.assertEqual(r["hold_off_wait"], {})

    def test_a_cooldown_with_time_left_holds_either_way(self):
        r = self.result
        self.assertFalse(r["cooling_released"])
        self.assertTrue(r["cooling_paused"])
        self.assertEqual(r["cooling_wait"], "rate_limit")

    def test_a_user_pause_is_never_released(self):
        r = self.result
        self.assertFalse(r["user_released"])
        self.assertTrue(r["user_paused"])

    def test_a_held_pause_survives_a_restart_as_a_pause(self):
        saved = self.result["saved"]
        self.assertTrue(saved["limit_paused"])
        # Not a user pause: the flag that distinguishes them stays false, which
        # is what makes the restart come back held rather than merely paused.
        self.assertFalse(saved["user_paused"]["instagram"])


if __name__ == "__main__":
    unittest.main()
