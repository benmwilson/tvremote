import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import app as server  # noqa: E402
import hlsproxy  # noqa: E402
import throttle  # noqa: E402
import tvpreview  # noqa: E402
from helpers import founder_client  # noqa: E402

PL = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nhttps://cdn.example.com/a.ts\n#EXTINF:2,\nb.ts\n"


class RelayTests(unittest.TestCase):
    def setUp(self):
        tvpreview._sessions.clear()
        p = mock.patch.object(hlsproxy, "_check")  # no DNS in tests
        p.start(); self.addCleanup(p.stop)

    def test_segments_point_at_the_app_and_only_listed_ones_are_served(self):
        t = tvpreview.create(lambda: PL, "https://cdn.example.com/p/live.m3u8")
        out = tvpreview.playlist(t)
        segs = [l for l in out.splitlines() if l.startswith("seg?")]
        self.assertEqual(len(segs), 2)
        self.assertEqual(unquote(parse_qs(urlparse(segs[1]).query)["u"][0]), "https://cdn.example.com/p/b.ts")  # relative one resolved
        with mock.patch.object(hlsproxy, "_get", return_value=b"x") as g:
            self.assertEqual(tvpreview.segment(t, "https://cdn.example.com/a.ts"), b"x")
            self.assertIsNone(tvpreview.segment(t, "https://evil.example/x.ts"))
            self.assertIsNone(tvpreview.segment("nope", "https://cdn.example.com/a.ts"))
            self.assertEqual(g.call_count, 1)

    def test_unknown_token_and_session_cap(self):
        self.assertIsNone(tvpreview.playlist("nope"))
        for _ in range(5):
            tvpreview.create(lambda: PL, "https://cdn.example.com/")
        self.assertEqual(len(tvpreview._sessions), tvpreview.MAX_SESSIONS)

    def test_refused_hosts_are_left_out(self):
        with mock.patch.object(hlsproxy, "_check", side_effect=ValueError("non-public")):
            t = tvpreview.create(lambda: PL, "https://cdn.example.com/")
            self.assertNotIn("seg?", tvpreview.playlist(t))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir)):
            p.start(); self.addCleanup(p.stop)
        throttle.fails.clear(); tvpreview._sessions.clear()
        founder_client(server)
        self.c = server.app.test_client()

    def test_start_needs_the_key_and_a_channel(self):
        u = "/tv/pv?u=https://kick.com/capped"
        self.assertIn(self.c.get(u, headers={"Accept": "application/json"}).status_code, (302, 303, 401, 403))
        self.assertEqual(self.c.get("/tv/pv?u=https://kick.com/video/abc&k=" + server.TV_KEY).status_code, 404)
        with mock.patch.object(server, "stream_url", return_value="https://cdn.example.com/p.m3u8"):
            r = self.c.get(u + "&k=" + server.TV_KEY).get_json()
        self.assertTrue(r["ok"] and r["src"].endswith(".m3u8"))

    def test_playlist_and_segment_work_by_token_alone(self):
        with mock.patch.object(hlsproxy, "_check"), mock.patch.object(hlsproxy, "_get", return_value=PL.encode()):
            t = tvpreview.create(lambda: PL, "https://cdn.example.com/p.m3u8")
            body = self.c.get(f"/tv/pv/{t}.m3u8").get_data(as_text=True)
            seg = [l for l in body.splitlines() if l.startswith("seg?")][0]
            self.assertEqual(self.c.get("/tv/pv/" + seg).status_code, 200)
        self.assertEqual(self.c.get("/tv/pv/nope.m3u8").status_code, 404)
        self.assertEqual(self.c.get("/tv/pv/seg?t=nope&u=x").status_code, 404)


if __name__ == "__main__":
    unittest.main()
