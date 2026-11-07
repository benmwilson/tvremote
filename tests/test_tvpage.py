import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import accounts  # noqa: E402
import app as server  # noqa: E402
import throttle  # noqa: E402
from helpers import founder_client  # noqa: E402


def tile(name, thumb=None, avatar=None):
    return {"id": name, "kind": "stream", "name": name, "url": "https://kick.com/" + name, "live": True, "thumb": thumb, "avatar": avatar, "members": [1]}


class TvPageTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir), mock.patch.object(server.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)
        throttle.fails.clear()
        founder_client(server)  # setup finished: logins are required from now on
        server._tv_imgs.clear(); server._tv_img_cache.clear()
        self.c = server.app.test_client()  # no login, like the TV's browser

    def test_without_the_key_the_page_and_pictures_need_a_login(self):
        for p in ("/tv", "/tv/data", "/tv/img?id=x"):
            self.assertIn(self.c.get(p, headers={"Accept": "application/json"}).status_code, (302, 303, 401, 403))
            self.assertIn(self.c.get(p + ("&" if "?" in p else "?") + "k=wrong", headers={"Accept": "application/json"}).status_code, (302, 303, 401, 403))

    def test_the_key_opens_the_page_and_data(self):
        with mock.patch.object(server, "launcher_state", return_value=None):
            self.assertEqual(self.c.get("/tv?k=" + server.TV_KEY).status_code, 200)
            self.assertEqual(self.c.get("/tv/data?k=" + server.TV_KEY).get_json(), {"rows": [], "row": 0, "col": 0})

    def test_only_six_tiles_keep_a_stream_picture_and_addresses_become_ids(self):
        tiles = [tile(f"s{i}", f"https://img/{i}.jpg", f"https://av/{i}.jpg") for i in range(8)]
        with mock.patch.object(server, "launcher_state", return_value=([{"title": "Live now", "tiles": tiles}], 0, 2)):
            d = self.c.get("/tv/data?k=" + server.TV_KEY).get_json()
        out = d["rows"][0]["tiles"]
        self.assertEqual(sum(1 for t in out if t["thumb"]), server.TV_THUMBS)
        self.assertTrue(all(t["avatar"].startswith("/tv/img?id=") for t in out))
        self.assertTrue(all("members" not in t for t in out))
        self.assertEqual((d["row"], d["col"]), (0, 2))
        self.assertNotIn("https://img", str(d)); self.assertNotIn("https://av", str(d))  # the browser is never given a real picture address

    def test_only_pictures_from_the_last_data_can_be_fetched(self):
        self.assertEqual(self.c.get("/tv/img?id=nothere&k=" + server.TV_KEY).status_code, 404)
        i = server._tv_img_id("http://insecure/x.jpg")
        self.assertEqual(self.c.get(f"/tv/img?id={i}&k=" + server.TV_KEY).status_code, 404)  # https only

    def test_a_picture_is_fetched_once_and_must_be_an_image(self):
        i = server._tv_img_id("https://cdn/x.jpg")
        resp = mock.MagicMock(); resp.__enter__.return_value = resp
        resp.headers = {"Content-Type": "image/jpeg"}; resp.read.return_value = b"JPEG"
        with mock.patch.object(server.urllib.request, "urlopen", return_value=resp) as op:
            for _ in range(2):
                r = self.c.get(f"/tv/img?id={i}&k=" + server.TV_KEY)
            self.assertEqual((r.status_code, r.data), (200, b"JPEG"))
            self.assertEqual(op.call_count, 1)
        j = server._tv_img_id("https://cdn/page.html")
        resp.headers = {"Content-Type": "text/html"}
        with mock.patch.object(server.urllib.request, "urlopen", return_value=resp):
            self.assertEqual(self.c.get(f"/tv/img?id={j}&k=" + server.TV_KEY).status_code, 502)


if __name__ == "__main__":
    unittest.main()


class BrowserFlagTests(unittest.TestCase):
    def test_the_browser_is_wanted_only_on_the_idle_launcher(self):
        home = mock.Mock(kind="idle")
        with mock.patch.object(server, "launcher_on", return_value=True), mock.patch.object(server, "HOME", home), \
                mock.patch.object(server, "pi_playing", return_value=False), mock.patch.object(server.wall, "active", return_value=False), \
                mock.patch.dict(server.state, {"starting": False}):
            self.assertTrue(server.tv_browser_wanted())
            home.kind = "loading"
            self.assertFalse(server.tv_browser_wanted())
            home.kind = "idle"
            with mock.patch.object(server, "pi_playing", return_value=True):
                self.assertFalse(server.tv_browser_wanted())
            with mock.patch.object(server.wall, "active", return_value=True):
                self.assertFalse(server.tv_browser_wanted())
            with mock.patch.dict(server.state, {"starting": True}):
                self.assertFalse(server.tv_browser_wanted())
            with mock.patch.object(server, "launcher_on", return_value=False):
                self.assertFalse(server.tv_browser_wanted())
