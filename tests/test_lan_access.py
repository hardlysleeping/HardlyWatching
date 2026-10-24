"""Phone access: the address to type, and who is allowed in.

The server listens on every interface so the switch can be flipped without a
restart, which means the gate -- not the bind address -- is what keeps the app
private. Two things must hold:

  * this machine always gets in, whatever the switch says;
  * another machine gets in only when the switch is on.

There is no password anywhere, so a bug in the second rule hands the app to the
whole network.

The gate is exercised in a child process: importing server.py resolves its
state, port and output paths from the environment, and this must not be able to
reach the real ones.
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

try:  # imported as `tests.test_lan_access`
    from . import sandbox
except ImportError:  # discovered with tests/ on sys.path
    import sandbox

REPO_ROOT = Path(__file__).resolve().parent.parent

PROBE = textwrap.dedent('''
    import json, sys
    sys.path.insert(0, sys.argv[1])
    import server
    cases = [
        ("127.0.0.1", False), ("127.1.2.3", False), ("::1", False),
        ("::ffff:127.0.0.1", False),
        ("192.168.1.44", False), ("10.0.0.7", False), ("100.79.162.94", False),
        ("::ffff:192.168.1.44", False),
        ("", False), ("0.0.0.0", False), ("::", False), (None, False),
        ("192.168.1.44", True), ("10.0.0.7", True),
        ("127.0.0.1", True), ("127.1.2.3", True), ("::1", True),
        ("::ffff:127.0.0.1", True),
    ]
    print(json.dumps({
        "allowed": [server._lan_gate_allows(a, e) for a, e in cases],
        "cases": [[a, e] for a, e in cases],
        "default_host": server.HOST,
        "local": [server._is_local_client(a) if a else server._is_local_client("")
                  for a in ("127.0.0.1", "::1", "0.0.0.0", "", None)],
    }))
''')


class GateTests(unittest.TestCase):
    """The decision itself, without a socket."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="hwa-lan-")
        root = Path(cls._tmp.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith("INSTA_")}
        env.update({
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / ".config"),
            "XDG_CACHE_HOME": str(root / ".cache"),
            "INSTA_STATE": str(root / "state.json"),
            "INSTA_CACHE_DB": str(root / "cache.db"),
            "INSTA_OUTPUT": str(root / "out"),
            "INSTA_PORT": "8912",
        })
        result = subprocess.run([sys.executable, "-c", PROBE, str(REPO_ROOT)],
                                cwd=str(REPO_ROOT), env=env, capture_output=True,
                                text=True, timeout=120)
        if result.returncode != 0:
            raise AssertionError("probe failed: %s" % result.stderr[-3000:])
        cls.data = json.loads(result.stdout.strip().splitlines()[-1])
        cls.table = {tuple(c): ok for c, ok in
                     zip(cls.data["cases"], cls.data["allowed"])}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_loopback_is_always_allowed(self):
        for addr in ("127.0.0.1", "127.1.2.3", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(self.table[(addr, False)], addr)
            self.assertTrue(self.table[(addr, True)], addr)

    def test_another_machine_is_refused_while_the_switch_is_off(self):
        for addr in ("192.168.1.44", "10.0.0.7", "100.79.162.94",
                     "::ffff:192.168.1.44"):
            self.assertFalse(self.table[(addr, False)], addr)

    def test_another_machine_is_allowed_once_the_switch_is_on(self):
        self.assertTrue(self.table[("192.168.1.44", True)])
        self.assertTrue(self.table[("10.0.0.7", True)])

    def test_an_unknown_peer_is_not_treated_as_local(self):
        # What a socket reports when it cannot tell. "Nobody" must never mean
        # "this machine".
        for addr in ("", "0.0.0.0", "::", None):
            self.assertFalse(self.table[(addr, False)], repr(addr))
        self.assertEqual(self.data["local"], [True, True, False, False, False])

    def test_the_server_binds_every_interface_so_the_switch_works_live(self):
        self.assertEqual(self.data["default_host"], "0.0.0.0")


class NetworkRouteTests(unittest.TestCase):
    """The card's data, end to end on a sandbox server."""

    def setUp(self):
        self.sb = sandbox.Sandbox(seed_profile=None)
        self.sb.start()

    def tearDown(self):
        self.sb.stop()

    def network(self):
        r = self.sb.get("/api/network")
        self.assertEqual(r.status, 200, r.body[:300])
        return r.json()

    def test_it_reports_the_port_and_a_loopback_address(self):
        n = self.network()
        self.assertEqual(n["port"], self.sb.port)
        self.assertEqual(n["loopback"], "http://127.0.0.1:%d" % self.sb.port)

    def test_phone_access_is_off_on_a_fresh_install(self):
        self.assertFalse(self.network()["enabled"])

    def test_the_addresses_listed_are_not_loopback(self):
        for url in self.network()["urls"]:
            self.assertTrue(url.startswith("http://"), url)
            host = url.split("//", 1)[1].rsplit(":", 1)[0]
            self.assertFalse(host.startswith("127."), url)

    def test_the_switch_is_persisted(self):
        on = self.sb.post("/api/settings", {"settings": {"lan_access": True}})
        self.assertEqual(on.status, 200, on.body[:300])
        self.assertTrue(on.json()["settings"]["lan_access"])
        self.assertTrue(self.network()["enabled"])

        off = self.sb.post("/api/settings", {"settings": {"lan_access": False}})
        self.assertEqual(off.status, 200)
        self.assertFalse(self.network()["enabled"])

    def test_this_machine_keeps_working_either_way(self):
        for value in (True, False):
            self.sb.post("/api/settings", {"settings": {"lan_access": value}})
            self.assertEqual(self.sb.get("/api/health").status, 200)


if __name__ == "__main__":
    unittest.main()
