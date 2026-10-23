"""Smoke tests over the routes the UI actually calls.

Every test runs against a throwaway server from ``tests/sandbox.py``: temp
``HOME``, temp state/cache/archive, stub ``instaloader``/``yt-dlp``, a free port
at or above 8899. Nothing here can reach the real archive, the real
``~/.config/hardlywatching`` or the service on 8765.

What these assert is *shape*, not values: the keys the UI reads, the type of
each, and (where it matters) that the data really comes from the sandbox. Exact
counts, ports and timestamps belong to whatever the sandbox happens to hold, so
pinning them would make the suite break for reasons that are not regressions.

Run:  python3 -m unittest discover -s tests -v      (from the repo root)
"""

from __future__ import annotations

import json
import shutil
import unittest

try:  # imported as `tests.test_api_smoke` (discover from the repo root)
    from . import sandbox
except ImportError:  # imported as a top-level module (discover -s tests)
    import sandbox

SEEDED_PROFILE = "sandbox_seed"


class SandboxCase(unittest.TestCase):
    """One sandbox per test class, plus the helpers the cases share.

    A class gets its own server and its own temp tree, so classes cannot see
    each other's queue. Within a class the tests are read-only unless the class
    says otherwise, which is what keeps them order-independent without paying
    for a server start per test.
    """

    sandbox_kwargs: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.sb = sandbox.Sandbox(seed_profile=SEEDED_PROFILE, **cls.sandbox_kwargs)
        cls.sb.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sb.stop()

    # -- helpers ---------------------------------------------------------

    def get_json(self, path: str):
        resp = self.sb.get(path)
        self.assertEqual(resp.status, 200, "%s answered %s: %r"
                         % (path, resp.status, resp.body[:400]))
        self.assertEqual(resp.content_type, "application/json",
                         "%s is not JSON: %r" % (path, resp.content_type))
        return resp.json()

    def post_json(self, path: str, body=None):
        resp = self.sb.post(path, body if body is not None else {})
        self.assertEqual(resp.status, 200, "%s answered %s: %r"
                         % (path, resp.status, resp.body[:400]))
        self.assertEqual(resp.content_type, "application/json",
                         "%s is not JSON: %r" % (path, resp.content_type))
        return resp.json()

    def assert_keys(self, obj, keys, where: str) -> None:
        """`obj` is a dict carrying at least `keys`."""
        self.assertIsInstance(obj, dict, "%s should be an object" % where)
        missing = [k for k in keys if k not in obj]
        self.assertFalse(missing, "%s is missing %s" % (where, missing))


class HealthAndStateTests(SandboxCase):
    """/api/health and /api/state: the two the page polls constantly."""

    def test_health(self):
        body = self.get_json("/api/health")
        self.assertEqual(body, {"ok": True})

    def test_state_shape(self):
        body = self.get_json("/api/state")
        self.assert_keys(body, [
            "current", "paused", "tt_paused", "queued", "jobs", "roots",
            "settings", "output_root", "instaloader", "sessions",
            "active_session", "cooldown_remaining", "autocheck",
            "session_present", "session_ok", "session_problem", "counts",
        ], "/api/state")
        self.assertIsInstance(body["current"], dict)
        self.assert_keys(body["current"], ["instagram", "tiktok"], "current")
        self.assertIsInstance(body["paused"], bool)
        self.assertIsInstance(body["tt_paused"], bool)
        self.assertIsInstance(body["queued"], int)
        self.assertIsInstance(body["jobs"], list)
        self.assertIsInstance(body["roots"], list)
        self.assertIsInstance(body["settings"], dict)
        self.assertIsInstance(body["counts"], dict)
        self.assertIsInstance(body["autocheck"], dict)
        self.assert_keys(body["autocheck"], ["on", "hours"], "autocheck")

    def test_state_is_the_sandbox_not_the_real_app(self):
        """The strongest guard in the suite: the server is looking at temp paths."""
        body = self.get_json("/api/state")
        root = str(self.sb.root)
        self.assertEqual(body["output_root"], str(self.sb.output),
                         "output_root is not the sandbox archive")
        self.assertTrue(body["output_root"].startswith(root))
        self.assertTrue(all(str(r.get("path", "")).startswith(root)
                            for r in body["roots"]),
                        "a root outside the sandbox: %r" % (body["roots"],))
        # engine.py resolves INSTALOADER at import: it must be our stub, not the
        # real binary, or a download would reach Instagram.
        self.assertEqual(body["instaloader"], str(self.sb.bin_dir / "instaloader"))

    def test_no_path_points_at_the_real_app(self):
        """Nothing the server knows about lives in the real config or archive."""
        self.assertTrue(str(self.sb.state_file).startswith(str(self.sb.root)))
        self.assertTrue(str(self.sb.cache_db).startswith(str(self.sb.root)))
        log = self.sb.log_path.read_text(errors="replace")
        self.assertNotIn("hardlywatching/state.json", log)


class GalleryRouteTests(SandboxCase):
    """The read-only browsing routes: gallery, timeline, bookmarks, peek, links."""

    def test_gallery_lists_the_seeded_profile(self):
        body = self.get_json("/api/gallery")
        self.assert_keys(body, ["profiles"], "/api/gallery")
        self.assertIsInstance(body["profiles"], list)
        self.assertTrue(body["profiles"], "the seeded profile is not listed")
        by_name = {p["username"]: p for p in body["profiles"]}
        self.assertIn(SEEDED_PROFILE, by_name)
        entry = by_name[SEEDED_PROFILE]
        self.assert_keys(entry, ["username", "platform", "count", "status",
                                 "root", "downloading"], "gallery entry")
        self.assertIsInstance(entry["count"], int)
        self.assertGreater(entry["count"], 0)
        self.assertEqual(entry["platform"], "instagram")
        self.assertEqual(entry["root"], str(self.sb.output))

    def test_timeline(self):
        body = self.get_json("/api/timeline")
        self.assert_keys(body, ["months", "total"], "/api/timeline")
        self.assertIsInstance(body["months"], dict)
        self.assertIsInstance(body["total"], int)
        for month, count in body["months"].items():
            self.assertRegex(month, r"^\d{4}-\d{2}$")
            self.assertIsInstance(count, int)
        self.assertEqual(body["total"], sum(body["months"].values()))
        self.assertGreater(body["total"], 0, "the seeded posts are not in the timeline")

    def test_timeline_month_posts(self):
        body = self.get_json("/api/timeline-month?month=all")
        self.assert_keys(body, ["month", "posts"], "/api/timeline-month")
        self.assertEqual(body["month"], "all")
        self.assertIsInstance(body["posts"], list)
        self.assertTrue(body["posts"], "no posts in the seeded month view")
        mine = [p for p in body["posts"] if p.get("username") == SEEDED_PROFILE]
        self.assertTrue(mine, "the seeded profile has no posts")
        post = mine[0]
        self.assert_keys(post, ["username", "file", "kind", "ts", "section"],
                         "timeline post")
        self.assertIsInstance(post["file"], str)
        self.assertTrue(post["file"])

    def test_bookmarks(self):
        body = self.get_json("/api/bookmarks")
        self.assert_keys(body, ["categories", "category", "posts", "total",
                                "uncategorised", "keys"], "/api/bookmarks")
        self.assertEqual(body["category"], "all")
        self.assertIsInstance(body["categories"], list)
        self.assertIsInstance(body["posts"], list)
        self.assertIsInstance(body["total"], int)
        self.assertIsInstance(body["uncategorised"], int)
        self.assertIsInstance(body["keys"], dict)

    def test_peek_needs_a_profile_and_returns_tiles(self):
        # The handler 400s without ?profile= (server.py:1284), so the route is
        # exercised the way the UI calls it rather than with a weakened check.
        bare = self.sb.get("/api/peek")
        self.assertEqual(bare.status, 400)

        body = self.get_json("/api/peek?profile=%s&platform=instagram&n=4" % SEEDED_PROFILE)
        self.assert_keys(body, ["profile", "platform", "liked", "items"], "/api/peek")
        self.assertEqual(body["profile"], SEEDED_PROFILE)
        self.assertEqual(body["platform"], "instagram")
        self.assertIsInstance(body["liked"], int)
        self.assertIsInstance(body["items"], list)
        self.assertTrue(body["items"], "peek returned no tiles for the seeded profile")
        self.assertLessEqual(len(body["items"]), 4)
        item = body["items"][0]
        self.assert_keys(item, ["file", "kind", "platform", "liked", "ts"],
                         "peek item")
        self.assertIsInstance(item["file"], str)
        self.assertIsInstance(item["liked"], bool)

    def test_peek_for_an_unknown_profile_is_empty_not_an_error(self):
        body = self.get_json("/api/peek?profile=no_such_handle_here")
        self.assertEqual(body["items"], [])
        self.assertEqual(body["liked"], 0)

    def test_links(self):
        body = self.get_json("/api/links")
        self.assert_keys(body, ["links", "splits"], "/api/links")
        self.assertIsInstance(body["links"], list)
        self.assertIsInstance(body["splits"], list)

    def test_partials_preview(self):
        body = self.get_json("/api/partials")
        self.assert_keys(body, ["count", "bytes", "files", "errors", "dry_run"],
                         "/api/partials")
        self.assertIsInstance(body["files"], list)
        self.assertTrue(body["dry_run"], "the preview must not delete anything")
        self.assertIsInstance(body["count"], int)


class FeedAndStatusTests(SandboxCase):
    """Shorts feed, static scan progress, autostart, TikTok browsers."""

    def test_shorts_feed(self):
        # It is a POST, not a GET: the feed is a weighted pick that remembers
        # what it showed (server.py:1770).
        body = self.post_json("/api/shorts/feed", {"limit": 5})
        self.assert_keys(body, ["clips", "stats", "profile", "platform"],
                         "/api/shorts/feed")
        self.assertIsInstance(body["clips"], list)
        self.assert_keys(body["stats"], ["total", "liked", "trashed"],
                         "shorts stats")
        self.assertIsInstance(body["stats"]["total"], int)
        self.assertEqual(body["profile"], "")
        self.assertEqual(body["platform"], "")

    def test_shorts_feed_accepts_an_empty_body(self):
        body = self.post_json("/api/shorts/feed")
        self.assertIsInstance(body["clips"], list)

    def test_shorts_feed_filtered_by_profile(self):
        body = self.post_json("/api/shorts/feed",
                              {"limit": 3, "profile": SEEDED_PROFILE,
                               "platform": "instagram"})
        self.assertEqual(body["profile"], SEEDED_PROFILE)
        self.assertEqual(body["platform"], "instagram")
        for clip in body["clips"]:
            self.assertIsInstance(clip.get("path"), str)

    def test_static_scan_status(self):
        body = self.get_json("/api/static")
        self.assert_keys(body, ["running", "phase", "checked", "total", "found",
                                "found_count", "bytes", "done", "error"],
                         "/api/static")
        self.assertIsInstance(body["running"], bool)
        self.assertIsInstance(body["done"], bool)
        self.assertIsInstance(body["found"], list)
        for key in ("checked", "total", "found_count", "bytes"):
            self.assertIsInstance(body[key], int, key)

    def test_autostart_status(self):
        # Read-only: systemctl --user is-enabled. The *value* depends on the
        # host, so only the shape is asserted here.
        body = self.get_json("/api/autostart")
        self.assert_keys(body, ["ok", "supported", "enabled", "state"],
                         "/api/autostart")
        self.assertIsInstance(body["supported"], bool)
        self.assertIsInstance(body["enabled"], bool)
        self.assertIsInstance(body["state"], str)

    def test_tiktok_browsers(self):
        body = self.get_json("/api/tiktok/browsers")
        self.assert_keys(body, ["browsers"], "/api/tiktok/browsers")
        self.assertIsInstance(body["browsers"], list)
        for name in body["browsers"]:
            self.assertIsInstance(name, str)


class StaticFileTests(SandboxCase):
    """The files a browser or a launcher asks for by name."""

    def test_index_html(self):
        resp = self.sb.get("/")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "text/html")
        text = resp.text()
        self.assertIn("<title>HardlyWatching</title>", text)
        # The page is only useful if it carries the script the UI runs.
        self.assertIn("fetch(", text)

    def test_shorts_html(self):
        resp = self.sb.get("/shorts")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "text/html")
        self.assertIn("<title>Shorts</title>", resp.text())

    def test_app_webmanifest(self):
        resp = self.sb.get("/app.webmanifest")
        self.assertEqual(resp.status, 200)
        self.assertIn(resp.content_type, ("application/manifest+json",
                                          "application/json"))
        manifest = resp.json()
        self.assert_keys(manifest, ["name", "short_name", "start_url", "icons"],
                         "app.webmanifest")
        self.assertEqual(manifest["name"], "HardlyWatching")
        self.assertIsInstance(manifest["icons"], list)

    def test_icon_192(self):
        resp = self.sb.get("/icon-192.png")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "image/png")
        self.assertTrue(resp.body.startswith(b"\x89PNG\r\n\x1a\n"),
                        "not a PNG: %r" % resp.body[:16])

    def test_favicon(self):
        resp = self.sb.get("/favicon.ico")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "image/vnd.microsoft.icon")
        self.assertTrue(resp.body.startswith(b"\x00\x00\x01\x00"),
                        "not an ICO: %r" % resp.body[:16])


class WriteRouteTests(SandboxCase):
    """The only mutating routes in the suite, and they only mutate the temp tree."""

    def test_pause_then_resume(self):
        paused = self.post_json("/api/pause", {"platform": "instagram"})
        self.assertEqual(paused, {"ok": True, "platform": "instagram"})
        self.assertIs(self.get_json("/api/state")["paused"], True,
                      "pause did not reach the state the UI renders")

        resumed = self.post_json("/api/resume", {"platform": "instagram"})
        self.assertEqual(resumed, {"ok": True, "platform": "instagram"})
        self.assertIs(self.get_json("/api/state")["paused"], False,
                      "resume did not clear the paused flag")

    def test_add_queues_a_job_that_state_shows(self):
        # Paused first so the job is still sitting in the queue when it is
        # asserted on; otherwise the stub downloader may finish it in the same
        # millisecond and the status becomes "done".
        self.post_json("/api/pause", {"platform": "instagram"})
        self.addCleanup(self.sb.post, "/api/resume", {"platform": "instagram"})

        added = self.post_json("/api/add", {"profiles": "sandbox_added"})
        self.assert_keys(added, ["added", "rejected"], "/api/add")
        self.assertEqual(added["added"], ["sandbox_added"])
        self.assertEqual(added["rejected"], [])

        state = self.get_json("/api/state")
        jobs = [j for j in state["jobs"] if j.get("username") == "sandbox_added"]
        self.assertEqual(len(jobs), 1, "the added job is not in /api/state: %r"
                         % (state["jobs"],))
        job = jobs[0]
        self.assert_keys(job, ["id", "username", "status", "kind", "platform",
                               "attempts", "added_at"], "state job")
        self.assertIsInstance(job["id"], str)
        self.assertTrue(job["id"])
        self.assertEqual(job["kind"], "main")
        self.assertEqual(job["platform"], "instagram")
        self.assertEqual(job["status"], "queued")
        self.assertGreaterEqual(state["queued"], 1)

        # The folder the job would write to is inside the sandbox too.
        self.assertTrue(str(self.sb.output) in json.dumps(state))

    def test_add_rejects_a_duplicate(self):
        self.post_json("/api/pause", {"platform": "instagram"})
        self.addCleanup(self.sb.post, "/api/resume", {"platform": "instagram"})
        first = self.post_json("/api/add", {"profiles": "sandbox_dupe"})
        self.assertEqual(first["added"], ["sandbox_dupe"])
        again = self.post_json("/api/add", {"profiles": "sandbox_dupe"})
        self.assertEqual(again["added"], [])
        self.assertEqual(len(again["rejected"]), 1)
        self.assertIn("sandbox_dupe", again["rejected"][0])

    def test_writes_persist_inside_the_sandbox(self):
        """A write lands in the temp state file, never in the real config dir."""
        self.post_json("/api/pause", {"platform": "instagram"})
        self.addCleanup(self.sb.post, "/api/resume", {"platform": "instagram"})
        self.assertTrue(self.sb.state_file.is_file(),
                        "a write did not reach the sandbox state file")
        saved = json.loads(self.sb.state_file.read_text())
        self.assertIn("jobs", saved)
        self.assertEqual(saved.get("output_root"), str(self.sb.output))
        self.assertIs(saved.get("user_paused", {}).get("instagram"), True)

    def test_stub_binaries_shadow_the_real_ones(self):
        """A downloader that could reach the network would invalidate every test."""
        env = self.sb.env
        self.assertEqual(env["PATH"].split(":")[0], str(self.sb.bin_dir))
        self.assertEqual(shutil.which("instaloader", path=env["PATH"]),
                         str(self.sb.bin_dir / "instaloader"))
        self.assertEqual(shutil.which("yt-dlp", path=env["PATH"]),
                         str(self.sb.bin_dir / "yt-dlp"))


class SandboxHarnessTests(unittest.TestCase):
    """The sandbox's own promises, tested without a shared server."""

    def test_a_second_sandbox_moves_to_another_port_and_stays_its_own(self):
        """Port taken between probe and bind: follow the announced URL.

        This is the case that made an earlier version flaky: two sandboxes (two
        worktrees, or two classes) racing for the same free port, and a runner
        that latched onto the other server's /api/health.
        """
        first = sandbox.Sandbox(seed_profile=None)
        second = None
        try:
            first.start()
            second = sandbox.Sandbox(seed_profile=None, port=first.port)
            second.start()
            self.assertNotEqual(second.port, first.port,
                                "the second sandbox kept a port that was in use")
            for sb in (first, second):
                state = sb.get("/api/state").json()
                self.assertEqual(state["output_root"], str(sb.output))
            self.assertNotEqual(first.get("/api/state").json()["output_root"],
                                second.get("/api/state").json()["output_root"])
        finally:
            if second is not None:
                second.stop()
            first.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
