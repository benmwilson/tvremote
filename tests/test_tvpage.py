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
    return {"id": name, "kind": "stream", "name": name, "url": "https://kick.com/" + name, "live": True, "thumb": thumb, "avatar": avatar, "members": None}


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
        self.assertTrue(all(t["members"] is None for t in out))
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


    def test_a_group_tile_carries_up_to_four_faces_as_ids(self):
        mem = [{"name": f"m{i}", "avatar": f"https://av/{i}.jpg"} for i in range(6)]
        g = {"id": "g1", "kind": "group", "name": "a, b", "url": None, "live": False, "avatar": None, "thumb": None, "members": mem}
        with mock.patch.object(server, "launcher_state", return_value=([{"title": "Groups", "tiles": [g]}], 0, 0)):
            t = self.c.get("/tv/data?k=" + server.TV_KEY).get_json()["rows"][0]["tiles"][0]
        self.assertEqual(len(t["members"]), 4)
        self.assertTrue(all(m["avatar"].startswith("/tv/img?id=") for m in t["members"]))


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


class FocusAndAvatarTests(unittest.TestCase):
    def test_kick_avatars_come_from_channels_then_users_and_are_remembered(self):
        from unittest import mock as m
        import kick
        kick._avatars.clear()
        calls = []
        def fake(req, timeout=10):
            calls.append(req.full_url)
            if "/channels" in req.full_url:
                return '{"data":[{"slug":"omie","broadcaster_user_id":7}]}'
            return '{"data":[{"user_id":7,"profile_picture":"https://files.kick.com/p.webp"}]}'
        with m.patch.object(kick, "enabled", return_value=True), m.patch.object(kick, "_app_token", return_value="t"), m.patch.object(kick, "_open", fake):
            self.assertEqual(kick.avatars(["Omie"]), {"omie": "https://files.kick.com/p.webp"})
            self.assertEqual(kick.avatars(["omie"]), {"omie": "https://files.kick.com/p.webp"})
        self.assertEqual(len(calls), 2)  # two requests the first time, none the second

    def test_fill_avatars_sets_only_missing_pictures(self):
        items = [{"url": "https://kick.com/omie", "source": "Kick"}, {"url": "https://kick.com/xqc", "source": "Kick", "avatar": "keep"}]
        with mock.patch.object(server.kick, "avatars", return_value={"omie": "https://p/omie"}), mock.patch.object(server.twitch, "users", return_value={}):
            server.fill_avatars(items)
        self.assertEqual([i.get("avatar") for i in items], ["https://p/omie", "keep"])


class WeatherTests(unittest.TestCase):
    def test_geocode_and_current_read_open_meteo_and_remember(self):
        import weather
        weather._cache.clear()
        def get(url):
            if "geocoding" in url:
                return {"results": [{"name": "Vancouver", "admin1": "British Columbia", "country_code": "CA", "latitude": 49.2827, "longitude": -123.1207}]}
            return {"current": {"temperature_2m": 9.6, "weather_code": 61, "is_day": 0}, "daily": {"temperature_2m_max": [11.2], "temperature_2m_min": [6.4]}, "utc_offset_seconds": -28800}
        with mock.patch.object(weather, "_get", side_effect=get) as g:
            place = weather.geocode(" Vancouver ")
            self.assertEqual(place, {"name": "Vancouver, British Columbia, CA", "lat": 49.283, "lon": -123.121})
            w = weather.current(place, "C")
            self.assertEqual((w["temp"], w["hi"], w["lo"], w["code"], w["day"], w["offset"]), (10, 11, 6, 61, False, -28800))
            weather.current(place, "C")
            self.assertEqual(g.call_count, 2)  # the second look is from memory
        self.assertIsNone(weather.geocode(""))
        with mock.patch.object(weather, "_get", return_value={"results": []}):
            self.assertIsNone(weather.geocode("zzzz"))
        self.assertIsNone(weather.current(None))

    def test_a_stale_answer_beats_none_when_the_service_is_down(self):
        import weather, time
        place = {"name": "X", "lat": 1.0, "lon": 2.0}
        weather._cache[(1.0, 2.0, "C")] = (time.time() - 9999, {"temp": 3})
        with mock.patch.object(weather, "_get", side_effect=OSError):
            self.assertEqual(weather.current(place, "C"), {"temp": 3})

