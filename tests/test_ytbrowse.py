import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import app as server  # noqa: E402
import throttle  # noqa: E402
import ytbrowse  # noqa: E402
from helpers import founder_client  # noqa: E402

INFO = {"title": "MKBHD - Videos", "channel": "MKBHD", "channel_follower_count": 19000000,
        "thumbnails": [{"id": "0", "url": "https://banner", "width": 1060, "height": 175}, {"id": "avatar_uncropped", "url": "https://avatar", "width": 900, "height": 900}],
        "entries": [{"id": "a1", "title": "One", "duration": 60, "view_count": 5, "live_status": "not_live", "url": "https://www.youtube.com/watch?v=a1"},
                    {"id": "b2", "title": "Live", "live_status": "is_live"},
                    {"_type": "playlist", "id": "p", "title": "a tab"}, None]}


class ListingTests(unittest.TestCase):
    def setUp(self):
        ytbrowse._cache.clear()

    def test_the_kinds(self):
        self.assertEqual(ytbrowse.kind("https://www.youtube.com/@mkbhd/streams"), "channel")
        self.assertEqual(ytbrowse.kind("https://youtube.com/playlist?list=PL1"), "playlist")
        self.assertEqual(ytbrowse.kind("https://www.youtube.com/watch?v=x&list=PL1"), "video")  # a video in a list plays as a video
        self.assertIsNone(ytbrowse.kind("https://evil.example/youtube.com/@x"))

    def test_a_channel_listing_becomes_cards_and_is_remembered(self):
        fetch = mock.Mock(return_value=INFO)
        out = ytbrowse.listing("https://www.youtube.com/@mkbhd", "streams", fetch=fetch)
        fetch.assert_called_once_with("https://www.youtube.com/@mkbhd/streams", 30)
        self.assertEqual((out["channel"], out["avatar"], out["subscribers"], out["tab"]), ("MKBHD", "https://avatar", 19000000, "streams"))
        self.assertEqual([c["title"] for c in out["entries"]], ["One", "Live"])  # the nested tab entry and the None are left out
        self.assertEqual(out["entries"][1]["url"], "https://www.youtube.com/watch?v=b2")
        self.assertTrue(out["entries"][1]["live"])
        ytbrowse.listing("https://www.youtube.com/@mkbhd", "streams", fetch=fetch)
        self.assertEqual(fetch.call_count, 1)

    def test_not_a_channel_is_refused(self):
        with self.assertRaises(ValueError):
            ytbrowse.listing("https://www.youtube.com/watch?v=x", fetch=mock.Mock())


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir)):
            p.start(); self.addCleanup(p.stop)
        throttle.fails.clear(); ytbrowse._cache.clear()
        self.c = founder_client(server)

    def test_the_route(self):
        with mock.patch.object(server, "_youtube_fetch", return_value=INFO):
            r = self.c.get("/api/youtube/list?u=https://www.youtube.com/@mkbhd").get_json()
        self.assertTrue(r["ok"] and len(r["entries"]) == 2)
        self.assertEqual(self.c.get("/api/youtube/list?u=https://kick.com/x").status_code, 400)
        with mock.patch.object(server, "_youtube_fetch", side_effect=OSError("down")):
            self.assertEqual(self.c.get("/api/youtube/list?u=https://www.youtube.com/@other").status_code, 502)


if __name__ == "__main__":
    unittest.main()
