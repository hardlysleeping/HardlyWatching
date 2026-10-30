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
import tempfile
import unittest
from pathlib import Path

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


class SavedVideosTests(SandboxCase):
    """The saved-videos pass reads a TikTok export; nothing here hits the network.

    The export is a file the account holder downloads, so the fixture is written
    into the sandbox's own tree and the routes are asked about it. The stub
    yt-dlp means a queued job cannot download anything real.
    """

    def export_file(self, name="user_data_tiktok.json", rows=None, doc=None):
        rows = rows if rows is not None else [
            {"Date": "2024-01-01 10:00:00", "Link": "https://www.tiktok.com/@a/video/111"},
            {"Date": "2024-02-01 10:00:00", "Link": "https://www.tiktok.com/@b/video/222"},
            {"Date": "2024-03-01 10:00:00", "Link": "https://www.tiktok.com/@b/video/222"},
        ]
        path = self.sb.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc if doc is not None else {
            "Your Activity": {"Favorite Videos": {"FavoriteVideoList": rows}}}))
        return path

    def test_share_links_count(self):
        """The export hands out tiktokv.com/share/video/<id>, not watch links."""
        path = self.export_file(rows=[
            {"Date": "2026-10-27 20:57:45",
             "Link": "https://www.tiktokv.com/share/video/7701400658804526366/"},
            {"Date": "2026-10-26 11:00:00",
             "Link": "https://www.tiktokv.com/share/video/7694113282067746051/"},
        ])
        dry = self.post_json("/api/tiktok/saved", {"path": str(path), "dry_run": True})
        self.assertEqual(dry["count"], 2, dry)

    def test_likes_are_not_favourites(self):
        """Likes sit beside favourites under "Likes and Favorites"; only one counts."""
        doc = {"Likes and Favorites": {
            "Favorite Videos": {"FavoriteVideoList": [
                {"Date": "2026-01-01 00:00:00",
                 "Link": "https://www.tiktokv.com/share/video/111/"}]},
            "Like List": {"ItemFavoriteList": [
                {"date": "2026-01-02 00:00:00",
                 "link": "https://www.tiktokv.com/share/video/222/"}]},
        }}
        path = self.export_file("likes.json", doc=doc)
        dry = self.post_json("/api/tiktok/saved", {"path": str(path), "dry_run": True})
        self.assertEqual(dry["count"], 1, "a liked video was counted as saved")

    def test_newest_first(self):
        import tiktok_engine as tt
        path = self.export_file(rows=[
            {"Date": "2024-01-01 10:00:00", "Link": "https://www.tiktok.com/@old/video/111"},
            {"Date": "2025-01-01 10:00:00", "Link": "https://www.tiktok.com/@new/video/222"},
        ])
        links, _ = tt.saved_export_links(path)
        self.assertIn("222", links[0], "the list is not newest-first: %r" % (links,))

    def test_a_pasted_folder_resolves(self):
        """TikTok delivers the export as a folder, so a folder has to work."""
        path = self.export_file("TikTok_Data_123/user_data_tiktok.json")
        dry = self.post_json("/api/tiktok/saved",
                             {"path": str(path.parent), "dry_run": True})
        self.assertIs(dry["ok"], True, dry)
        self.assertEqual(dry["count"], 2)
        self.assertTrue(dry["file"].endswith("user_data_tiktok.json"))

    def test_dry_run_counts_without_queueing(self):
        path = self.export_file()
        dry = self.post_json("/api/tiktok/saved", {"path": str(path), "dry_run": True})
        self.assert_keys(dry, ["ok", "count", "file", "source"], "/api/tiktok/saved dry run")
        self.assertIs(dry["ok"], True)
        self.assertEqual(dry["count"], 2, "duplicate links should count once")
        self.assertEqual([j for j in self.get_json("/api/state")["jobs"]
                          if j.get("kind") == "saved"], [],
                         "a dry run queued a job")

    def test_queue_creates_one_saved_job(self):
        self.post_json("/api/pause", {"platform": "tiktok"})
        self.addCleanup(self.sb.post, "/api/resume", {"platform": "tiktok"})
        path = self.export_file()

        queued = self.post_json("/api/tiktok/saved", {"path": str(path)})
        self.assert_keys(queued, ["ok", "count", "queued"], "/api/tiktok/saved")
        self.assertIs(queued["ok"], True)
        self.assertEqual(queued["count"], 2)

        jobs = [j for j in self.get_json("/api/state")["jobs"] if j.get("kind") == "saved"]
        self.assertEqual(len(jobs), 1, "the saved job is not in /api/state: %r" % (jobs,))
        self.assertEqual(jobs[0]["username"], "saved")
        self.assertEqual(jobs[0]["platform"], "tiktok")

        # Queuing the same file again must not add a second row.
        self.post_json("/api/tiktok/saved", {"path": str(path)})
        jobs = [j for j in self.get_json("/api/state")["jobs"] if j.get("kind") == "saved"]
        self.assertEqual(len(jobs), 1, "re-queuing duplicated the saved job")

    def test_missing_and_empty_files_are_refused(self):
        missing = self.post_json("/api/tiktok/saved",
                                 {"path": str(self.sb.home / "nope.json")})
        self.assertIs(missing["ok"], False)
        self.assertIn("No TikTok export at", missing["error"])

        empty = self.export_file("empty.json", rows=[])
        refused = self.post_json("/api/tiktok/saved", {"path": str(empty)})
        self.assertIs(refused["ok"], False)
        self.assertIn("no saved videos", refused["error"].lower())

    def test_find_returns_a_list(self):
        found = self.post_json("/api/tiktok/saved/find", {})
        self.assert_keys(found, ["ok", "found"], "/api/tiktok/saved/find")
        self.assertIsInstance(found["found"], list)


class SavedLedgerTests(SandboxCase):
    """The ledger and the bookmark category, over a throwaway archive tree."""

    def engine(self):
        import engine as E
        return E.Engine(output_root=self.sb.output)

    def test_ledger_remembers_ids_and_survives_a_refetch(self):
        import profile_db as pdb
        db = pdb.ProfileDB(self.sb.root / "ledger.db")
        self.assertEqual(db.mark_saved([("111", "aaa", "111.mp4")]), 1)
        self.assertIn("111", db.saved_ids())
        # Recording it again is not a new row, and keeps the first grab time.
        self.assertEqual(db.mark_saved([("111", "aaa", "111.mp4")]), 0)
        self.assertEqual(db.saved_count(), 1)
        db.close()

    def test_bookmark_many_does_not_re_file_existing_bookmarks(self):
        import profile_db as pdb
        db = pdb.ProfileDB(self.sb.root / "bookmarks.db")
        mine = db.add_bookmark_category("Mine")
        db.set_bookmark("tiktok", "aaa", "111.mp4", True, mine["id"])
        favs = db.add_bookmark_category("TikTok Favorites")
        added = db.bookmark_many("tiktok", [("aaa", "111.mp4"), ("bbb", "222.mp4")],
                                 favs["id"])
        self.assertEqual(added, 1, "the bulk pass should only add the missing one")
        # A post can now be in several categories at once, so the bulk pass ADDS
        # a filing rather than moving the post: the user's own category is still
        # there afterwards, which is the property this test is about.
        rows = {(r["profile"], r["file"]): sorted(r["categories"]) for r in db.bookmarks()}
        self.assertEqual(rows[("aaa", "111.mp4")], sorted([mine["id"], favs["id"]]))
        self.assertEqual(rows[("bbb", "222.mp4")], [favs["id"]])
        # And the same post appears under each category when filtered by one.
        self.assertEqual(len(db.bookmarks(category=mine["id"])), 1)
        self.assertEqual(len(db.bookmarks(category=favs["id"])), 2)
        db.close()

    def test_creator_folders_are_what_counts_as_having_it(self):
        """A finished file in a creator folder is 'already here'; .part is not."""
        import engine as E
        tt_root = self.sb.output / "tiktok"
        (tt_root / "creatorA").mkdir(parents=True, exist_ok=True)
        (tt_root / "creatorA" / "123.mp4").write_bytes(b"x")
        (tt_root / "creatorB").mkdir(parents=True, exist_ok=True)
        (tt_root / "creatorB" / "456.mp4.part").write_bytes(b"x")
        on_disk = E.tiktok_ids_on_disk(tt_root)
        self.assertEqual(on_disk.get("123"), "creatorA")
        self.assertNotIn("456", on_disk, "a half-written file counted as downloaded")

    def test_destination_line_names_creator_and_id(self):
        import tiktok_engine as tt
        line = ("[download] Destination: /x/tiktok/missmixbackup/"
                "7701400658804526366.mp4")
        self.assertEqual(tt.parse_destination(line),
                         ("missmixbackup", "7701400658804526366",
                          "/x/tiktok/missmixbackup/7701400658804526366.mp4"))
        self.assertIsNone(tt.parse_destination("[download] 100% of 1MiB"))
        merged = '[Merger] Merging formats into "/x/tiktok/who/999.mp4"'
        self.assertEqual(tt.parse_destination(merged)[:2], ("who", "999"))

    def test_creators_are_queued_unless_the_switch_is_off(self):
        """In-process engine, pointed at the sandbox: never at the live state."""
        import engine as E
        import profile_db as pdb

        # Engine() reads its state path from a module global and the profile
        # database from the shared factory, so both are repointed at the sandbox
        # before it is built. Without this the test drives the live queue.
        old_state = E.STATE_FILE
        old_shared = E._shared_db
        tmp_db = pdb.ProfileDB(self.sb.root / "toggle-profiles.db")
        self.addCleanup(setattr, E, "STATE_FILE", old_state)
        self.addCleanup(setattr, E, "_shared_db", old_shared)
        self.addCleanup(tmp_db.close)
        E.STATE_FILE = self.sb.root / "toggle-state.json"
        E._shared_db = lambda: tmp_db

        eng = self.engine()
        job = E.Job(username="saved", kind="saved", platform="tiktok")
        job.saved_creators = ["creatorone", "creatortwo"]
        eng.jobs[job.id] = job
        self.addCleanup(eng.save_state)

        eng.settings = E.Settings.from_dict({**eng.settings.to_dict(),
                                             "queue_saved_creators": False})
        eng._queue_saved_creators(job)
        self.assertEqual([j.username for j in eng.jobs.values()
                          if j.platform == "tiktok" and j.kind != "saved"], [],
                         "the switch was off and a profile was queued anyway")

        eng.settings = E.Settings.from_dict({**eng.settings.to_dict(),
                                             "queue_saved_creators": True})
        eng._queue_saved_creators(job)
        queued = {j.username for j in eng.jobs.values()
                  if j.platform == "tiktok" and j.kind != "saved"}
        self.assertEqual(queued, {"creatorone", "creatortwo"})

        # Running it again must not duplicate the rows.
        eng._queue_saved_creators(job)
        self.assertEqual(len([j for j in eng.jobs.values()
                              if j.platform == "tiktok" and j.kind != "saved"]), 2)


class BookmarkDatesTests(SandboxCase):
    """The date a bookmark is ordered by, and applying the export's own dates."""

    def engine(self, db):
        import engine as E
        import profile_db as pdb
        old_state, old_shared = E.STATE_FILE, E._shared_db
        old_pdb = pdb._shared_db
        self.addCleanup(setattr, E, "STATE_FILE", old_state)
        self.addCleanup(setattr, E, "_shared_db", old_shared)
        # `bookmarked_posts` reaches the database through profile_db's own
        # factory, not the engine's alias, so both have to be repointed or the
        # test reads the live collection.
        self.addCleanup(setattr, pdb, "_shared_db", old_pdb)
        E.STATE_FILE = self.sb.root / "dates-state.json"
        E._shared_db = lambda: db
        pdb._shared_db = lambda: db
        return E.Engine(output_root=self.sb.output)

    def test_a_saved_video_is_dated_by_when_it_was_saved(self):
        import profile_db as pdb
        db = pdb.ProfileDB(self.sb.root / "dates.db")
        self.addCleanup(db.close)
        tt = self.sb.output.parent / "tiktok"
        (tt / "CreatorA").mkdir(parents=True, exist_ok=True)
        (tt / "CreatorA" / "111.mp4").write_bytes(b"x")
        # A liked/favourited file proves the section is resolved, not assumed.
        (tt / "CreatorA" / "favorites").mkdir(exist_ok=True)
        (tt / "CreatorA" / "favorites" / "222.mp4").write_bytes(b"x")
        favs = db.add_bookmark_category("TikTok Favorites")
        db.bookmark_many("tiktok", [("creatora", "111.mp4"),
                                    ("creatora", "222.mp4")], favs["id"])
        db.mark_saved([("111", "creatora", "111.mp4", 1_700_000_000)])

        eng = self.engine(db)
        posts = {p["items"][0]["file"]: p for p in eng.bookmarked_posts("all")}
        self.assertEqual(set(posts), {"111.mp4", "222.mp4"},
                         "a bookmark whose folder is spelled CreatorA was dropped")
        self.assertEqual(posts["111.mp4"]["ts"], 1_700_000_000,
                         "the ledger's save date is not the sort date")
        self.assertEqual(posts["111.mp4"]["ts_src"], "saved")
        self.assertTrue(posts["111.mp4"]["date"].startswith("2023-11-14"))
        self.assertEqual(posts["111.mp4"]["section"], "main")
        self.assertEqual(posts["222.mp4"]["section"], "favorites")
        self.assertEqual(posts["222.mp4"]["ts_src"], "bookmarked",
                         "a video with no export date must not claim one")

    def test_an_undated_bulk_pass_does_not_move_a_known_date(self):
        """A later pass that knows no date must not flatten the collection."""
        import profile_db as pdb
        db = pdb.ProfileDB(self.sb.root / "undated.db")
        self.addCleanup(db.close)
        favs = db.add_bookmark_category("TikTok Favorites")
        db.bookmark_many("tiktok", [("creatora", "111.mp4", 1_700_000_000)],
                         favs["id"])
        db.bookmark_many("tiktok", [("creatora", "111.mp4"),
                                    ("creatora", "222.mp4")], favs["id"])
        rows = {r["file"]: r for r in db.bookmarks()}
        self.assertAlmostEqual(rows["111.mp4"]["created"], 1_700_000_000,
                              msg="an undated pass overwrote a date it did not know")
        self.assertGreater(rows["222.mp4"]["created"], 1_700_000_000,
                           "a new undated bookmark should still be dated now")

    def test_restamp_dates_the_ledger_and_only_the_saved_category(self):
        import profile_db as pdb
        db = pdb.ProfileDB(self.sb.root / "restamp.db")
        self.addCleanup(db.close)
        mine = db.add_bookmark_category("Mine")
        favs = db.add_bookmark_category("TikTok Favorites")
        db.set_bookmark("tiktok", "creatora", "111.mp4", True, mine["id"])
        db.bookmark_many("tiktok", [("creatora", "222.mp4"),
                                    ("creatora", "333.mp4")], favs["id"])
        db.mark_saved([("222", "creatora", "222.mp4"),
                       ("333", "creatora", "333.mp4")])

        moved = db.restamp_saved({"222": 1_700_000_000, "333": 1_600_000_000},
                                 favs["id"])
        self.assertEqual(moved, 4, "two ledger rows and two bookmarks should move")

        rows = {r["file"]: r for r in db.bookmarks()}
        self.assertAlmostEqual(rows["222.mp4"]["created"], 1_700_000_000)
        self.assertAlmostEqual(rows["333.mp4"]["created"], 1_600_000_000)
        self.assertNotAlmostEqual(rows["111.mp4"]["created"], 1_700_000_000,
                                  msg="a bookmark in the user's own category was restamped")
        saved = {r["video_id"]: r["saved_at"] for r in db.saved_rows()}
        self.assertEqual(saved["222"], 1_700_000_000)
        self.assertEqual(saved["333"], 1_600_000_000)


class QueueClearTests(SandboxCase):
    """"Clear queue" is a move to Finished, never a deletion."""

    def test_clearing_the_queue_keeps_every_job(self):
        import engine as E
        import profile_db as pdb
        old_state, old_shared = E.STATE_FILE, E._shared_db
        self.addCleanup(setattr, E, "STATE_FILE", old_state)
        self.addCleanup(setattr, E, "_shared_db", old_shared)
        db = pdb.ProfileDB(self.sb.root / "clear.db")
        self.addCleanup(db.close)
        E.STATE_FILE = self.sb.root / "clear-state.json"
        E._shared_db = lambda: db
        eng = E.Engine(output_root=self.sb.output)
        self.addCleanup(eng.save_state)

        wanted = ("queued", "queued", "running", "done", "failed")
        for i, st in enumerate(wanted):
            j = E.Job(username="u%d" % i, platform="instagram")
            j.status = st
            eng.jobs[j.id] = j
            if st == "queued":
                eng.pending.append(j.id)

        cleared, live = eng.clear_queue()
        self.assertEqual(cleared, 2)
        self.assertEqual(live, 1, "the job that is downloading must keep running")
        self.assertEqual(len(eng.jobs), len(wanted),
                         "clearing the queue deleted a job instead of finishing it")
        self.assertEqual(sorted(j.status for j in eng.jobs.values()),
                         ["cancelled", "cancelled", "done", "failed", "running"])
        self.assertEqual(eng.pending, [],
                         "a cleared job stayed in the pending order")


class SavedPhotoPostTests(SandboxCase):
    """A slideshow link becomes images in the creator's folder, once."""

    def engine(self, db):
        import engine as E
        import profile_db as pdb
        old_state, old_shared = E.STATE_FILE, E._shared_db
        old_pdb = pdb._shared_db
        self.addCleanup(setattr, E, "STATE_FILE", old_state)
        self.addCleanup(setattr, E, "_shared_db", old_shared)
        self.addCleanup(setattr, pdb, "_shared_db", old_pdb)
        db_obj = db
        E.STATE_FILE = self.sb.root / "photos-state.json"
        E._shared_db = lambda: db_obj
        pdb._shared_db = lambda: db_obj
        return E.Engine(output_root=self.sb.output)

    def test_a_slideshow_link_becomes_images_in_the_creators_folder(self):
        import engine as E
        import profile_db as pdb
        import tiktok_engine as tt

        db = pdb.ProfileDB(self.sb.root / "photos.db")
        self.addCleanup(db.close)
        eng = self.engine(db)
        job = E.Job(username="saved", kind="saved", platform="tiktok")
        eng.jobs[job.id] = job
        self.addCleanup(eng.save_state)

        tt_root = self.sb.output.parent / "tiktok"
        (tt_root / "saved").mkdir(parents=True, exist_ok=True)
        tmp = tt_root / "saved" / "photos"
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "111.jpg").write_bytes(b"x")
        (tmp / "111_2.jpg").write_bytes(b"x")
        (tmp / "111.info.json").write_text('{"timestamp": 1700000000}')

        rows = [{"id": "111", "status": 0, "author": "creatorA", "images": 2,
                 "files": [str(tmp / "111.jpg"), str(tmp / "111_2.jpg")],
                 "error": ""}]
        seen = []

        def fake_photo_posts(ids, **kw):
            seen.append(list(ids))
            return rows

        old = tt.photo_posts
        self.addCleanup(setattr, tt, "photo_posts", old)
        tt.photo_posts = fake_photo_posts

        link = "https://www.tiktokv.com/share/video/111/"
        posts, images = eng._grab_saved_photos(job, tt_root, [link],
                                               {"111": 1_700_000_000})
        self.assertEqual((posts, images), (1, 2))
        self.assertTrue((tt_root / "creatorA" / "111.jpg").is_file())
        self.assertTrue((tt_root / "creatorA" / "111_2.jpg").is_file())
        self.assertTrue((tt_root / "creatorA" / "111.info.json").is_file(),
                        "the sidecar must travel with the images, or the post is undated")

        saved = {r["video_id"]: r for r in db.saved_rows()}
        self.assertEqual(saved["111"]["creator"], "creatora",
                         "the ledger lowercases handles, as everywhere else")
        self.assertEqual(saved["111"]["file"], "111.jpg")
        self.assertEqual(saved["111"]["saved_at"], 1_700_000_000)
        cat = db.add_bookmark_category("TikTok Favorites")
        rows_bm = {r["file"]: r for r in db.bookmarks(cat["id"])}
        self.assertIn("111.jpg", rows_bm)
        self.assertEqual(rows_bm["111.jpg"]["created"], 1_700_000_000)

        # Second run: the ledger answers "already have it", so the tool is not
        # asked about the same link again.
        eng._grab_saved_photos(job, tt_root, [link], {"111": 1_700_000_000})
        self.assertEqual(seen, [["111"]], "a saved photo post was asked for twice")

    def test_a_link_that_is_neither_video_nor_photo_is_recorded_not_retried(self):
        import engine as E
        import profile_db as pdb
        import tiktok_engine as tt

        db = pdb.ProfileDB(self.sb.root / "gone.db")
        self.addCleanup(db.close)
        eng = self.engine(db)
        job = E.Job(username="saved", kind="saved", platform="tiktok")
        eng.jobs[job.id] = job
        self.addCleanup(eng.save_state)

        tt_root = self.sb.output.parent / "tiktok"
        (tt_root / "saved").mkdir(parents=True, exist_ok=True)
        calls = []

        def fake_photo_posts(ids, **kw):
            calls.append(list(ids))
            return [{"id": "999", "status": 10204, "author": "", "images": 0,
                     "files": [], "error": ""}]

        old = tt.photo_posts
        self.addCleanup(setattr, tt, "photo_posts", old)
        tt.photo_posts = fake_photo_posts

        link = "https://www.tiktokv.com/share/video/999/"
        self.assertEqual(eng._grab_saved_photos(job, tt_root, [link], {}), (0, 0))
        self.assertTrue((tt_root / "saved" / ".not-photos.txt").is_file())
        # Asked once: the next run reads the record instead of re-requesting it.
        eng._grab_saved_photos(job, tt_root, [link], {})
        self.assertEqual(calls, [["999"]])


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


class ProfilePhotoPostTests(SandboxCase):
    """A profile's slideshows become images in that profile's folder, once.

    A normal profile pass is yt-dlp with `-f bv*+ba/b`, and a TikTok slideshow
    offers no video format -- so the post leaves nothing at all. The photos are
    in the same item detail blob, so the same tool takes them; what this proves
    is that only posts with NO file are asked about, that the sidecar travels
    with the images, and that a post which is neither video nor photo is asked
    about exactly once.
    """

    def engine(self, db):
        import engine as E
        import profile_db as pdb
        old_state, old_shared = E.STATE_FILE, E._shared_db
        old_pdb = pdb._shared_db
        self.addCleanup(setattr, E, "STATE_FILE", old_state)
        self.addCleanup(setattr, E, "_shared_db", old_shared)
        self.addCleanup(setattr, pdb, "_shared_db", old_pdb)
        E.STATE_FILE = self.sb.root / "pphotos-state.json"
        E._shared_db = lambda: db
        pdb._shared_db = lambda: db
        return E.Engine(output_root=self.sb.output)

    def test_slideshows_in_a_profile_are_saved_and_videos_are_not_re_asked(self):
        import engine as E
        import profile_db as pdb
        import tiktok_engine as tt

        db = pdb.ProfileDB(self.sb.root / "pphotos.db")
        self.addCleanup(db.close)
        eng = self.engine(db)
        job = E.Job(username="someone", platform="tiktok")
        eng.jobs[job.id] = job
        self.addCleanup(eng.save_state)

        prof = self.sb.output.parent / "tiktok" / "someone"
        prof.mkdir(parents=True, exist_ok=True)
        (prof / "111.mp4").write_bytes(b"v")          # a downloaded video
        (prof / "333.jpg").write_bytes(b"p")          # a slideshow, already here
        (prof / "333_2.jpg").write_bytes(b"p")
        (prof / "444.mp4").write_bytes(b"v")
        (prof / ".not-photos.txt").write_text("444\n")  # proven: not a photo post

        rows = [{"id": "222", "status": 0, "author": "someone", "images": 2,
                 "files": [str(prof / ".photos" / "222.jpg"),
                           str(prof / ".photos" / "222_2.jpg")], "error": ""}]
        asked = []

        def fake_list_ids(handle, count=0, timeout=150):
            return ["111", "222", "333", "444", "555"]

        def fake_photo_posts(ids, **kw):
            asked.append([i for i in ids])
            out = prof / ".photos"
            out.mkdir(parents=True, exist_ok=True)
            (out / "222.jpg").write_bytes(b"i")
            (out / "222_2.jpg").write_bytes(b"i")
            (out / "222.info.json").write_text('{"id": "222"}')
            # 555 is asked about and turns out to be nothing downloadable.
            return rows + [{"id": "555", "status": 10204, "files": [],
                            "images": 0, "error": ""}]

        for name, fake in (("list_ids", fake_list_ids),
                           ("photo_posts", fake_photo_posts)):
            old = getattr(tt, name)
            self.addCleanup(setattr, tt, name, old)
            setattr(tt, name, fake)

        posts, images = eng._grab_profile_photos(job, prof, 0)
        self.assertEqual((posts, images), (1, 2))
        self.assertEqual(asked, [["222", "555"]],
                         "a post that already has a file (or a .mp4) was asked about")
        self.assertTrue((prof / "222.jpg").is_file())
        self.assertTrue((prof / "222_2.jpg").is_file())
        self.assertTrue((prof / "222.info.json").is_file(),
                        "the sidecar must travel with the images, or the post is undated")
        self.assertEqual((prof / ".not-photos.txt").read_text().split(),
                         ["444", "555"], "a proven non-photo post must be recorded")
        self.assertFalse((prof / ".photos").exists(),
                         "the temp folder should not be left in the archive")

        # Second run: nothing new to ask about, so the tool is not called at all.
        asked.clear()
        self.assertEqual(eng._grab_profile_photos(job, prof, 0), (0, 0))
        self.assertEqual(asked, [], "a profile photo pass re-asked about posts it had")


class TaggedPostTests(SandboxCase):
    """Posts somebody else made, which land in a folder via instaloader --tagged.

    The only marker the archive has for them is the sidecar's owner, so these
    tests pin that: same folder, same naming, and the one field that separates
    "they posted this" from "they were tagged in this".
    """

    def engine(self, root):
        import engine as E
        import profile_db as pdb
        old_state, old_shared = E.STATE_FILE, E._shared_db
        old_pdb = pdb._shared_db
        self.addCleanup(setattr, E, "STATE_FILE", old_state)
        self.addCleanup(setattr, E, "_shared_db", old_shared)
        self.addCleanup(setattr, pdb, "_shared_db", old_pdb)
        db = pdb.ProfileDB(self.sb.root / "tagged.db")
        self.addCleanup(db.close)
        E.STATE_FILE = self.sb.root / "tagged-state.json"
        E._shared_db = lambda: db
        pdb._shared_db = lambda: db
        eng = E.Engine(output_root=root)
        eng.roots = [root]
        self.addCleanup(eng.save_state)
        return eng

    @staticmethod
    def post(folder, stamp, owner, parts=1):
        """One post on disk: `parts` files plus the sidecar beside them."""
        for n in range(1, parts + 1):
            name = stamp if parts == 1 else "%s_%d" % (stamp, n)
            (folder / (name + ".jpg")).write_bytes(b"x" * 100)
        if owner is not None:
            side = {"node": {"owner": {"username": owner}}}
            (folder / (stamp + ".json")).write_text(json.dumps(side))

    def test_only_posts_by_other_accounts_are_flagged_tagged(self):
        root = self.sb.root / "tagged-archive"
        folder = root / "taggedprofile"
        folder.mkdir(parents=True)
        self.post(folder, "2024-03-01_12-00-00_UTC", "taggedprofile")
        self.post(folder, "2024-03-02_12-00-00_UTC", "brandco")
        # A carousel's sidecar is <stamp>.json while its files are _1/_2, which
        # is the case that breaks a "sidecar sits beside the file" assumption.
        self.post(folder, "2024-03-03_12-00-00_UTC", "anotherbrand", parts=2)
        self.post(folder, "2024-03-04_12-00-00_UTC", None)

        eng = self.engine(root)
        posts = eng.gallery_posts("taggedprofile", "main")
        by_stamp = {p["ts"]: p for p in posts}
        self.assertEqual(len(posts), 4)
        flags = sorted((p["owner"], p["tagged"]) for p in posts)
        self.assertEqual(flags, [("", False),
                                 ("anotherbrand", True),
                                 ("brandco", True),
                                 ("taggedprofile", False)],
                         "tagged posts must be exactly the ones owned elsewhere")
        carousel = [p for p in posts if p["owner"] == "anotherbrand"][0]
        self.assertEqual(carousel["count"], 2,
                         "the carousel's parts were not grouped into one post")
        self.assertTrue(by_stamp)

    def test_the_all_view_keeps_the_flag_and_its_platform(self):
        root = self.sb.root / "tagged-all"
        folder = root / "someone"
        (folder / "favorites").mkdir(parents=True)
        self.post(folder / "favorites", "2024-04-01_12-00-00_UTC", "someone")
        self.post(folder / "favorites", "2024-04-02_12-00-00_UTC", "othersite")

        eng = self.engine(root)
        # gallery_all serves the cross-profile views only (SUBDIRS = favourites,
        # trash), so that is the section to exercise it with.
        posts = eng.gallery_all("favorites")
        mine = [p for p in posts if p["username"] == "someone"]
        self.assertEqual(len(mine), 2, "the across-all view dropped a profile")
        self.assertEqual(sum(1 for p in mine if p["tagged"]), 1)
        for p in mine:
            self.assertEqual(p["platform"], "instagram")
            self.assertEqual(p["section"], "favorites")


class PeopleClipTests(unittest.TestCase):
    """A clip is judged from stills taken out of it, not skipped."""

    @classmethod
    def setUpClass(cls):
        import shutil
        if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            raise unittest.SkipTest("needs ffmpeg and ffprobe")
        import subprocess
        cls.tmp = Path(tempfile.mkdtemp(prefix="hw-clip-"))
        cls.clip = cls.tmp / "clip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "color=c=black:s=320x240:d=3", "-pix_fmt", "yuv420p",
                        str(cls.clip)], check=True)
        cls.junk = cls.tmp / "junk.mp4"
        cls.junk.write_bytes(b"not a video" * 40)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_frames_are_taken_from_across_the_clip(self):
        import engine as E
        frames = E.video_frames(self.clip)
        self.addCleanup(E.cleanup_video_frames, frames)
        self.assertEqual(len(frames), E.PEOPLE_VIDEO_FRAMES)
        for f in frames:
            self.assertTrue(f.is_file() and f.stat().st_size > 0)

    def test_a_clean_clip_is_clean_and_every_frame_is_looked_at(self):
        import engine as E
        seen = []

        def fake(path):
            seen.append(Path(path).name)
            return (0, 0.02)

        old = E._people_probe_image
        self.addCleanup(setattr, E, "_people_probe_image", old)
        E._people_probe_image = fake
        self.assertEqual(E.people_probe(self.clip)[0], 0)
        self.assertEqual(len(seen), E.PEOPLE_VIDEO_FRAMES,
                         "a clip judged clean must have had all its frames read")

    def test_one_person_in_one_frame_is_enough(self):
        import engine as E
        seen = []

        def fake(path):
            seen.append(Path(path).name)
            return (1, 0.91) if len(seen) == 2 else (0, 0.02)

        old = E._people_probe_image
        self.addCleanup(setattr, E, "_people_probe_image", old)
        E._people_probe_image = fake
        self.assertEqual(E.people_probe(self.clip)[0], 1)
        self.assertEqual(len(seen), 2, "it kept decoding frames after finding a person")

    def test_an_unreadable_clip_is_unknown_not_clean(self):
        import engine as E
        self.assertIsNone(E.people_probe(self.junk))
        self.assertIsNone(E.people_probe(Path("/nonexistent/clip.mp4")))

    def test_pictures_do_not_go_through_ffmpeg(self):
        # The dispatcher must not hand a .jpg to ffprobe: a picture is decoded
        # by Pillow, and paying two subprocesses per photo would make the whole
        # pass an order of magnitude slower.
        import engine as E
        img = self.tmp / "a.jpg"
        img.write_bytes(b"x" * 10)
        calls = []
        old_img = E._people_probe_image
        self.addCleanup(setattr, E, "_people_probe_image", old_img)
        E._people_probe_image = lambda p: (calls.append(p) or (0, 0.01))
        self.assertEqual(E.people_probe(img), (0, 0.01))
        self.assertEqual(calls, [img], "a picture took the clip path")


class PeoplePassTests(SandboxCase):
    """The people pass: per-picture verdicts, per-profile runs, resumable."""

    def engine(self, root):
        import engine as E
        import profile_db as pdb
        old_state, old_shared = E.STATE_FILE, E._shared_db
        old_pdb = pdb._shared_db
        self.addCleanup(setattr, E, "STATE_FILE", old_state)
        self.addCleanup(setattr, E, "_shared_db", old_shared)
        self.addCleanup(setattr, pdb, "_shared_db", old_pdb)
        db = pdb.ProfileDB(self.sb.root / "people.db")
        self.addCleanup(db.close)
        E.STATE_FILE = self.sb.root / "people-state.json"
        E._shared_db = lambda: db
        pdb._shared_db = lambda: db
        eng = E.Engine(output_root=root)
        eng.roots = [root]
        self.addCleanup(eng.save_state)
        return eng

    def fake_probe(self, verdicts):
        """Replace the model with a lookup: {filename fragment: has_people}."""
        import engine as E
        seen = []

        def probe(path):
            seen.append(path.name)
            for frag, has in verdicts.items():
                if frag in path.name:
                    return (1, 0.9) if has else (0, 0.05)
            return (0, 0.05)

        old = E.people_probe
        self.addCleanup(setattr, E, "people_probe", old)
        E.people_probe = probe
        return seen

    def test_a_profile_is_scanned_once_and_posts_get_the_verdict(self):
        root = self.sb.root / "people-archive"
        folder = root / "ppl"
        folder.mkdir(parents=True)
        for stamp in ("2024-05-01_12-00-00_UTC",
                      "2024-05-02_12-00-00_UTC",
                      "2024-05-03_12-00-00_UTC"):
            (folder / f"{stamp}.jpg").write_bytes(b"x" * 300)
        (folder / "2024-05-04_12-00-00_UTC.mp4").write_bytes(b"v" * 300)

        seen = self.fake_probe({"2024-05-01": True,
                                "2024-05-02": False,
                                "2024-05-03": False})
        eng = self.engine(root)

        first = eng.people_scan_profile("ppl")
        # Three pictures and the clip: a video is looked at now, from stills.
        # (The model is faked here, so the clip answers like a picture does.)
        self.assertEqual(first["checked"], 4)
        self.assertEqual(first["people"], 1)
        self.assertEqual(first["clean"], 3)

        # Second run has nothing to do: the answers are already stored.
        second = eng.people_scan_profile("ppl")
        self.assertEqual(second["checked"], 0, "the pass redid work it had done")
        self.assertEqual(len(seen), 4, "a file was probed twice")

        posts = {p["post"]: p for p in eng.gallery_posts("ppl", "main")}
        self.assertIs(posts["2024-05-01_12-00-00_UTC"]["people"], True)
        self.assertIs(posts["2024-05-02_12-00-00_UTC"]["people"], False)
        self.assertIs(posts["2024-05-04_12-00-00_UTC"]["people"], False,
                      "a clip nobody was found in counts as clean")

    def test_progress_is_visible_while_a_profile_is_still_running(self):
        # A big profile takes minutes. If the counter only moves when the profile
        # ends, the pass looks stuck for all of it -- and the worker used to add
        # the profile's totals on top of what the profile itself published, so a
        # finished run counted everything twice.
        root = self.sb.root / "people-live"
        folder = root / "live"
        folder.mkdir(parents=True)
        for n in range(4):
            (folder / f"2024-06-0{n + 1}_12-00-00_UTC.jpg").write_bytes(b"x" * 200)
        self.fake_probe({"2024-06-02": True})
        eng = self.engine(root)

        out = eng.people_scan_profile("live")
        self.assertEqual(out["checked"], 4)
        self.assertEqual(eng._people_scan["checked"], 4,
                         "the running pass did not publish what it had done")
        self.assertEqual(eng._people_scan["with_people"], 1)
        self.assertEqual(eng._people_scan["clean"], 3)

        # And the worker does not add the same numbers again.
        eng._people_scan.update({"checked": 0, "clean": 0, "with_people": 0,
                                 "profiles_done": 0, "stop": False})
        eng._people_worker(workers=1)
        self.assertEqual(eng._people_scan["checked"], 0,
                         "nothing was left to do, so nothing should be counted")
        self.assertEqual(eng._people_scan["profiles_done"], 1)

    def test_a_replaced_picture_is_looked_at_again(self):
        root = self.sb.root / "people-replace"
        folder = root / "swap"
        folder.mkdir(parents=True)
        target = folder / "2024-06-01_12-00-00_UTC.jpg"
        target.write_bytes(b"x" * 300)

        seen = self.fake_probe({"2024-06-01": False})
        eng = self.engine(root)
        self.assertEqual(eng.people_scan_profile("swap")["checked"], 1)
        self.assertEqual(eng.people_scan_profile("swap")["checked"], 0)

        # Same name, different bytes: a different picture, so the old verdict
        # cannot stand.
        target.write_bytes(b"y" * 900)
        self.assertEqual(eng.people_scan_profile("swap")["checked"], 1)
        self.assertEqual(len(seen), 2)

    def test_a_half_scanned_folder_never_reads_as_people_free(self):
        root = self.sb.root / "people-half"
        folder = root / "half"
        folder.mkdir(parents=True)
        (folder / "2024-07-01_12-00-00_UTC.jpg").write_bytes(b"x" * 200)
        (folder / "2024-07-02_12-00-00_UTC_1.jpg").write_bytes(b"x" * 200)
        (folder / "2024-07-02_12-00-00_UTC_2.jpg").write_bytes(b"x" * 200)

        self.fake_probe({"2024-07-01": False, "2024-07-02": False})
        eng = self.engine(root)
        # Answer only the single-picture post by hand; the carousel stays unknown.
        db = __import__("profile_db")._shared_db()
        db.people_set_many([(str(folder / "2024-07-01_12-00-00_UTC.jpg"), 200, 0, 0.02)])

        posts = {p["post"]: p for p in eng.gallery_posts("half", "main")}
        self.assertIs(posts["2024-07-01_12-00-00_UTC"]["people"], False)
        self.assertIsNone(posts["2024-07-02_12-00-00_UTC"]["people"],
                          "a carousel with unknown parts was called people-free")


class SavedFailureLedgerTests(SandboxCase):
    """A refused saved link is remembered, so the next pass does not re-ask."""

    def test_a_permanent_refusal_is_parsed_and_recorded_once(self):
        import tiktok_engine as tt

        line = ("ERROR: [TikTok] 7638990020770139422: You do not have permission "
                "to view this post. Log into an account that has access.")
        self.assertEqual(tt.error_post(line)[0], "7638990020770139422")
        self.assertTrue(tt.permanent_error(tt.error_post(line)[1]))

        # A throttle is not permanent: the next run may well work.
        transient = "ERROR: [TikTok] 7638990020770139423: HTTP Error 429: Too Many Requests"
        self.assertFalse(tt.permanent_error(tt.error_post(transient)[1]))

        # A line about the whole page, not one post, is not a per-post verdict.
        self.assertIsNone(tt.error_post("ERROR: Unable to download webpage: timed out"))

    def test_the_failure_file_is_read_back_as_ids(self):
        import engine as E

        path = self.sb.root / "saved" / ".saved-failed.txt"
        eng = E.Engine(output_root=self.sb.output)
        self.addCleanup(eng.save_state)
        eng._note_saved_failures(path, [("7638990020770139422", "permission"),
                                        ("7638990020770139423", "format")])
        text = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(text), 2)
        ids = {ln.split("\t")[0] for ln in text}
        self.assertEqual(ids, {"7638990020770139422", "7638990020770139423"})
        # Three fields per line: id, reason, date -- so the file stays readable.
        self.assertEqual(len(text[0].split("\t")), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
