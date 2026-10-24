"""The version the bundle is named after must be the version the server reports.

Three places carry a version and they are allowed to disagree only on purpose:

  VERSION                    what /api/version answers; bumped by scripts/sync-upstream.sh
                             from the upstream tag
  src-tauri/tauri.conf.json  what the DEB/AppImage is named after and what the window
                             shows; forced from VERSION by src-tauri/build-in-container.sh
  src-tauri/Cargo.toml       crate metadata; also forced from VERSION at build time

A release that ships "HardlyWatching_0.1.0_amd64.deb" containing a server that answers
0.2.0 is the kind of thing nobody notices until they have to pick a file from a folder,
so it fails here instead. This is the VERSION-TAURI follow-up, made permanent.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class ReleaseVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.version = (REPO / "VERSION").read_text(encoding="utf-8").strip()

    def test_version_is_semver(self) -> None:
        self.assertRegex(
            self.version,
            r"^\d+\.\d+\.\d+$",
            "VERSION must be a plain x.y.z; the DEB filename and the tag both use it",
        )

    def test_tauri_config_matches_version(self) -> None:
        conf = json.loads((REPO / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        self.assertEqual(
            conf["version"],
            self.version,
            "src-tauri/tauri.conf.json has a different version than VERSION; the bundle "
            "would be named after the old one. `src-tauri/build-in-container.sh` fixes "
            "this before a build, but a checked-in mismatch means someone edited one file.",
        )

    def test_cargo_manifest_matches_version(self) -> None:
        cargo = (REPO / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
        package = cargo.split("[package]", 1)[1].split("\n[", 1)[0]
        found = re.search(r'^version = "([^"]+)"', package, re.MULTILINE)
        self.assertIsNotNone(found, "no version in the [package] section")
        assert found is not None
        self.assertEqual(found.group(1), self.version)

    def test_bundle_targets_do_not_ask_tauri_for_appimage(self) -> None:
        # Tauri's appimage step runs linuxdeploy, which aborts on the frozen server's
        # hashed private libraries. The AppImage is built by pack-appimage.sh instead,
        # so "all" (or "appimage") in bundle.targets makes `cargo tauri build` fail.
        conf = json.loads((REPO / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        targets = conf["bundle"]["targets"]
        self.assertNotEqual(targets, "all")
        self.assertNotIn("appimage", [t.lower() for t in targets])


if __name__ == "__main__":
    unittest.main()
