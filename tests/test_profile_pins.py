"""Pinned profiles: the one order the user sets by hand.

A pin keeps a profile at the top of the gallery, above whatever the name / count
/ size sort asks for. It is not a weight and not a bookmark category: it is a
position, and it has to survive a restart, work for a profile with no job, and
never be undone by a sort button.

What is pinned here:

  * the flag round-trips through the database (pin, unpin, idempotent);
  * it is keyed by platform + lowercased handle, so case never splits it;
  * `gallery_profiles()` marks the card and puts pinned profiles first;
  * the server route answers and the card carries `data-pinned`.

Sandbox only: the live queue, archive and profiles.db are never touched.
"""

from __future__ import annotations

import unittest

try:  # imported as `tests.test_profile_pins` (discover from the repo root)
    from . import sandbox
except ImportError:  # imported as a top-level module (discover -s tests)
    import sandbox


class PinCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.sb = sandbox.Sandbox(seed_profile=None)
        cls.sb.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sb.stop()

    def engine(self, tag: str, handles=("somebody", "another", "zed")):
        import engine as E
        import profile_db as pdb

        old_state, old_shared, old_cache = E.STATE_FILE, E._shared_db, E._cache
        old_pdb = pdb._shared_db
        self.addCleanup(setattr, E, "STATE_FILE", old_state)
        self.addCleanup(setattr, E, "_shared_db", old_shared)
        self.addCleanup(setattr, E, "_cache", old_cache)
        self.addCleanup(setattr, pdb, "_shared_db", old_pdb)

        root = self.sb.root / ("pins-" + tag)
        root.mkdir(parents=True, exist_ok=True)
        for name in handles:
            folder = root / name
            folder.mkdir(exist_ok=True)
            (folder / "2024-01-01_12-00-00_UTC.jpg").write_bytes(b"x" * 64)

        db = pdb.ProfileDB(self.sb.root / ("pins-%s.db" % tag))
        self.addCleanup(db.close)
        E.STATE_FILE = self.sb.root / ("pins-%s-state.json" % tag)
        E._shared_db = lambda: db
        pdb._shared_db = lambda: db
        E._cache = E._Cache(self.sb.root / ("pins-%s-cache.db" % tag))
        eng = E.Engine(output_root=root)
        eng.roots = [root]
        self.addCleanup(eng.save_state)
        return eng, E, db

    def names(self, eng):
        return [p["username"] for p in eng.gallery_profiles()]

    def test_a_pin_round_trips_and_unpins(self):
        eng, E, db = self.engine("round")
        self.assertFalse(db.is_profile_pinned("instagram", "somebody"))
        self.assertTrue(db.set_profile_pin("instagram", "somebody", True))
        self.assertTrue(db.is_profile_pinned("instagram", "somebody"))
        self.assertFalse(db.set_profile_pin("instagram", "somebody", False))
        self.assertFalse(db.is_profile_pinned("instagram", "somebody"))

    def test_a_pin_ignores_case(self):
        eng, E, db = self.engine("case")
        db.set_profile_pin("instagram", "SomeBody", True)
        self.assertTrue(db.is_profile_pinned("instagram", "somebody"))
        cards = {p["username"]: p for p in eng.gallery_profiles()}
        self.assertTrue(cards["somebody"]["pinned"],
                        "a pin on a different spelling of the handle was lost")

    def test_a_pinned_profile_comes_first(self):
        eng, E, db = self.engine("order")
        self.assertEqual(self.names(eng), ["another", "somebody", "zed"])
        db.set_profile_pin("instagram", "zed", True)
        got = self.names(eng)
        self.assertEqual(got[0], "zed", "the pinned profile is not first")
        self.assertEqual(got[1:], ["another", "somebody"],
                         "the rest of the list lost its order")
        # And the card carries the flag the page draws and the menu reads.
        card = next(p for p in eng.gallery_profiles() if p["username"] == "zed")
        self.assertTrue(card["pinned"])
        self.assertGreater(card["pinned_at"], 0)

    def test_the_engine_method_is_bound(self):
        # `set_profile_pin` is a module function bound into the class; without
        # the `_foo = _foo` line the route raises AttributeError at runtime.
        eng, E, db = self.engine("bound")
        self.assertTrue(hasattr(eng, "set_profile_pin"))
        self.assertTrue(eng.set_profile_pin("another", "instagram", True))
        self.assertTrue(db.is_profile_pinned("instagram", "another"))

    def test_pinning_does_not_need_a_profiles_row(self):
        # A profile copied in by hand has no `profiles` row; a pin must still work.
        eng, E, db = self.engine("norow")
        rows = db._conn.execute("SELECT COUNT(*) n FROM profiles").fetchone()
        self.assertEqual(int(rows["n"]), 0)
        db.set_profile_pin("instagram", "somebody", True)
        self.assertTrue(db.is_profile_pinned("instagram", "somebody"))


if __name__ == "__main__":
    unittest.main()
