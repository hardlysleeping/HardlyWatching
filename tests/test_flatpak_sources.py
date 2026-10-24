"""The flatpak ships the server modules by an explicit list.

That list is written by hand in the manifest, so a new module imported by
server.py is invisible until the packaged app dies on startup with
ModuleNotFoundError -- which is exactly how dupes.py shipped broken in 0.3.2's
first build. This walks the local imports of the entry points and demands every
module they need be in the install command.
"""

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "flatpak" / "io.github.hardlysleeping.HardlyWatching.json"
ENTRY_POINTS = ["server.py", "engine.py", "shorts.py", "tiktok_engine.py"]

# `import x` / `from x import y` at column 0: module-level only, which is where
# a packaged app breaks. Function-local imports are a different (working) thing.
_IMPORT_RE = re.compile(r"^(?:import|from)\s+([A-Za-z_][\w]*)")


def installed_modules() -> set[str]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for module in data["modules"]:
        for command in module.get("build-commands", []):
            if "install -m644" in command and ".py" in command:
                return {w for w in command.split() if w.endswith(".py")}
    raise AssertionError("no python install command found in the manifest")


def local_imports(path: Path) -> set[str]:
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _IMPORT_RE.match(line)
        if m:
            name = m.group(1)
            if (REPO / f"{name}.py").exists():      # local module, not stdlib/dep
                out.add(f"{name}.py")
    return out


class FlatpakSourcesTest(unittest.TestCase):
    def test_every_module_the_server_imports_is_shipped(self):
        installed = installed_modules()
        needed = set()
        for entry in ENTRY_POINTS:
            needed |= local_imports(REPO / entry)
        missing = sorted(needed - installed)
        self.assertEqual(missing, [],
                         "flatpak manifest does not install " + ", ".join(missing))

    def test_the_static_files_are_shipped(self):
        installed = installed_modules()
        # Not python, but the same install line: a missing page is a blank window.
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        joined = " ".join(c for m in data["modules"]
                          for c in m.get("build-commands", []))
        for name in ("index.html", "shorts.html", "VERSION"):
            self.assertIn(name, joined, f"{name} is not installed into the flatpak")
        self.assertTrue(installed, "nothing installed")


if __name__ == "__main__":
    unittest.main()
