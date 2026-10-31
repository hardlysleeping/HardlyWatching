"""Thumbnail building is bounded, so a gallery cannot eat the whole CPU quota.

Building thumbnails is bounded, and one thumbnail is built once however many
requests ask for it in the same instant. The service runs under a 250% CPU
quota, so an unbounded burst of ffmpeg jobs is what makes a gallery click feel
slow; and the grid and the popup can both want the same frame at once.

What is pinned here:

  * at most THUMB_SLOTS builds run at the same time;
  * a cache hit does not take a slot at all;
  * a request that waited for a slot reuses a thumb another request just built
    rather than rebuilding it.
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path

import engine as E


class ThumbSlotsCase(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(__file__).parent / "_thumb_slots_tmp"
        self.tmp.mkdir(exist_ok=True)
        self.old_thumb_path = E._thumb_path
        self.old_build = E._build_thumb
        self.addCleanup(setattr, E, "_thumb_path", self.old_thumb_path)
        self.addCleanup(setattr, E, "_build_thumb", self.old_build)
        E._thumb_path = lambda kind, name: self.tmp / (name + ".jpg")

    def _src(self, name: str) -> Path:
        p = self.tmp / name
        p.write_bytes(b"x" * 16)
        return p

    def test_only_three_builds_run_at_once(self):
        live = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def slow_build(src, ext, dest):
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            time.sleep(0.08)
            with lock:
                live["now"] -= 1
            dest.write_bytes(b"jpg")
            return dest

        E._build_thumb = slow_build
        srcs = [self._src(f"v{i}.mp4") for i in range(9)]
        ths = [threading.Thread(target=E.make_thumb, args=(s, ".mp4")) for s in srcs]
        [t.start() for t in ths]
        [t.join() for t in ths]
        self.assertLessEqual(live["peak"], E.THUMB_SLOTS._value,
                             "more than THUMB_SLOTS thumbnails were built at once")
        self.assertGreaterEqual(live["peak"], 2, "nothing ran in parallel at all")

    def test_a_cache_hit_takes_no_slot(self):
        src = self._src("cached.mp4")
        # Plant the thumbnail rather than building it: this test is about the
        # cache-hit path, and the real builder would call ffmpeg.
        built = E._thumb_path("v", E._cache_key(src))
        built.write_bytes(b"jpg")

        called = {"n": 0}

        def never(src, ext, dest):
            called["n"] += 1
            return dest

        E._build_thumb = never
        self.assertEqual(E.make_thumb(src, ".mp4"), built)
        self.assertEqual(called["n"], 0, "a cache hit went through the builder")

    def test_a_waiter_reuses_what_another_request_built(self):
        src = self._src("shared.mp4")
        dest = E._thumb_path("v", E._cache_key(src))
        built = {"n": 0}

        def once(src, ext, dest):
            # Stand in for the real builder: the first caller writes the file.
            built["n"] += 1
            time.sleep(0.05)
            dest.write_bytes(b"jpg")
            return dest

        E._build_thumb = once
        got = []
        ths = [threading.Thread(target=lambda: got.append(E.make_thumb(src, ".mp4")))
               for _ in range(4)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        self.assertEqual(len(got), 4)
        self.assertTrue(all(g == dest for g in got))
        self.assertEqual(built["n"], 1,
                         "the same thumbnail was built more than once")


if __name__ == "__main__":
    unittest.main()
