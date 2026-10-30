"""Where a TikTok archive lives, and why it must never nest inside Instagram.

TikTok sits BESIDE the Instagram folder -- `<mount>/hardlywatching/tiktok` next
to `<mount>/hardlywatching/instagram`. It used to be `<root>/tiktok`, which read
as "TikTok is part of Instagram", and the fallback that produced it on a
brand-new drive was worse than cosmetic: the app creates the sibling folder
before the first file lands in it, then asked its own rule what that EMPTY
folder was, decided "not recognisably mine", and nested the whole archive one
level down. The marker it wrote inside the nested folder then made the nested
copy look like the real archive, so the mistake became permanent.

The rules pinned here:

  * a new drive writes to the sibling, not inside the Instagram folder;
  * an EMPTY sibling folder is this app's own placeholder and is adopted;
  * a sibling holding the app's marker or a real profile folder is used;
  * a sibling holding anything else is a stranger's directory and is left
    alone -- the archive stays inside the root rather than scattering into it.

Every case gets its own throwaway tree, and the sandbox repoints HOME first, so
nothing here can touch the real archive or state.
"""

from __future__ import annotations

import unittest
from pathlib import Path

try:  # imported as `tests.test_tiktok_layout` (discover from the repo root)
    from . import sandbox
except ImportError:  # imported as a top-level module (discover -s tests)
    import sandbox


class TikTokLayoutCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.sb = sandbox.Sandbox(seed_profile=None)
        cls.sb.start()
        import engine as E
        cls.E = E

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sb.stop()

    def drive(self, name: str) -> Path:
        """A fresh fake drive: <base>/<name>/hardlywatching/instagram."""
        base = self.sb.root / ("tt-" + name)
        root = base / "hardlywatching" / "instagram"
        root.mkdir(parents=True)
        return root

    def test_a_new_drive_writes_beside_instagram(self) -> None:
        root = self.drive("fresh")
        sibling = root.parent / "tiktok"
        self.assertFalse(sibling.exists())
        got = self.E.tiktok_roots(root)
        self.assertEqual(got[0], sibling,
                         "a new drive must write TikTok beside Instagram")

    def test_an_empty_sibling_folder_is_this_apps_placeholder(self) -> None:
        # The regression: the writer creates the folder before the first file,
        # and an empty folder used to be judged "not mine", nesting the archive.
        root = self.drive("empty")
        sibling = root.parent / "tiktok"
        sibling.mkdir()
        got = self.E.tiktok_roots(root)
        self.assertEqual(got[0], sibling,
                         "an empty sibling folder must be adopted, not refused")
        self.assertNotIn(root / "tiktok", got[:1])

    def test_a_marked_sibling_is_used(self) -> None:
        root = self.drive("marked")
        sibling = root.parent / "tiktok"
        sibling.mkdir()
        (sibling / self.E.TIKTOK_MARKER).write_text("ours\n", encoding="utf-8")
        self.assertEqual(self.E.tiktok_roots(root)[0], sibling)

    def test_a_sibling_with_a_profile_folder_is_used(self) -> None:
        root = self.drive("profiles")
        sibling = root.parent / "tiktok"
        profile = sibling / "someone"
        profile.mkdir(parents=True)
        (profile / "123.mp4").write_bytes(b"x")
        self.assertEqual(self.E.tiktok_roots(root)[0], sibling)

    def test_a_strangers_sibling_folder_is_left_alone(self) -> None:
        # A folder named tiktok that this app did not make: never adopted, so
        # the archive stays inside the root instead of writing into it.
        root = self.drive("stranger")
        sibling = root.parent / "tiktok"
        sibling.mkdir()
        (sibling / "notes.txt").write_text("not an archive\n", encoding="utf-8")
        got = self.E.tiktok_roots(root)
        self.assertEqual(got[0], root / "tiktok")
        self.assertEqual(list(sibling.iterdir()), [sibling / "notes.txt"],
                         "nothing may be written into a stranger's folder")


if __name__ == "__main__":
    unittest.main()
