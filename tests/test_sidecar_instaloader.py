"""The bundle must still be able to download from Instagram.

`engine.py` does not import instaloader -- it resolves a *program* named
`instaloader` with `shutil.which()` and runs it. That makes the bundle's contract
two pieces, and dropping either one silently turns the desktop app into a viewer:

* `packaging/sidecar/hardlywatching.spec` must collect the `instaloader` package,
  or `--run-instaloader` has nothing to call;
* `packaging/sidecar/build.sh` must write the `bin/instaloader` wrapper, and that
  wrapper must exec `--run-instaloader`, or the app never finds a program by that
  name;
* `packaging/sidecar/entry.py` must answer `--run-instaloader` *before* it parses
  its own flags or imports `server` (which reads `sys.argv` at import time), or
  instaloader's arguments get eaten by the server's argument handling.

These are greps against the three files on purpose: there is no frozen bundle in
a test checkout, and the failure this guards against (someone tidying the wrapper
away) is a text-level one.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "sidecar" / "hardlywatching.spec"
BUILD = ROOT / "packaging" / "sidecar" / "build.sh"
ENTRY = ROOT / "packaging" / "sidecar" / "entry.py"


class SidecarInstaloaderTest(unittest.TestCase):
    def test_spec_collects_instaloader(self) -> None:
        text = SPEC.read_text(encoding="utf-8")
        self.assertIn('collect_submodules("instaloader")', text)

    def test_build_writes_the_wrapper(self) -> None:
        text = BUILD.read_text(encoding="utf-8")
        self.assertIn('"$BIN/instaloader"', text)
        # The wrapper must exec the bundle's own server with the passthrough flag --
        # anything else is a Python script that needs a host interpreter.
        self.assertIn('exec "$here/../hardlywatching-server" --run-instaloader "$@"', text)
        self.assertIn("chmod 0755", text)

    def test_entry_answers_before_parsing_its_own_flags(self) -> None:
        text = ENTRY.read_text(encoding="utf-8")
        self.assertIn("def run_instaloader(", text)
        main = text[text.index("def main() -> int:"):]
        run_at = main.index('"--run-instaloader"')
        parse_at = main.index("parse_args(")
        import_server_at = main.index("import server")
        self.assertLess(run_at, parse_at, "--run-instaloader must be handled before parse_args")
        self.assertLess(run_at, import_server_at,
                        "--run-instaloader must be handled before server is imported")
        # And the passthrough must keep argv intact.
        self.assertTrue(re.search(r'sys\.argv = \["instaloader"\] \+ rest', text))


if __name__ == "__main__":
    unittest.main()
