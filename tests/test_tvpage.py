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

    def test_preview_sound_is_only_offered_when_the_browser_has_a_sound_connection(self):
        rows = ([{"title": "Live now", "tiles": [tile("a", "https://img/a.jpg")]}], 0, 0)
        with mock.patch.object(server, "launcher_state", return_value=rows), mock.patch.dict(server.settings, {"launcher_preview_sound": True}):
            with mock.patch.object(server, "PREVIEW_AUDIO", False):
                self.assertFalse(self.c.get("/tv/data?k=" + server.TV_KEY).get_json()["sound"])  # asking for sound with no output stalls the video
            with mock.patch.object(server, "PREVIEW_AUDIO", True):
                self.assertTrue(self.c.get("/tv/data?k=" + server.TV_KEY).get_json()["sound"])

    def test_the_boot_animation_plays_once_after_the_app_starts(self):
        with mock.patch.object(server, "launcher_state", return_value=None):
            server._tv_intro["pending"] = True
            first = self.c.get("/tv?k=" + server.TV_KEY).get_data(as_text=True)
            second = self.c.get("/tv?k=" + server.TV_KEY).get_data(as_text=True)
        self.assertIn('<body class="intro">', first)
        self.assertNotIn('<body class="intro">', second)
        with mock.patch.dict(server.settings, {"boot_intro": False}):
            server._tv_intro["pending"] = True
            self.assertNotIn('<body class="intro">', self.c.get("/tv?k=" + server.TV_KEY).get_data(as_text=True))  # switched off

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

    def test_a_thumbnail_gets_a_new_id_every_two_minutes_so_the_browser_refetches(self):
        tiles = [tile("a", "https://img/a.jpg")]
        ids = []
        for now in (960.0, 1070.0, 1300.0):  # buckets of 120 s: 8, 8, 10
            with mock.patch.object(server, "launcher_state", return_value=([{"title": "x", "tiles": tiles}], 0, 0)), mock.patch.object(server.time, "time", return_value=now):
                ids.append(self.c.get("/tv/data?k=" + server.TV_KEY).get_json()["rows"][0]["tiles"][0]["thumb"])
        self.assertEqual(ids[0], ids[1])
        self.assertNotEqual(ids[1], ids[2])

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
        mem = [{"name": f"m{i}", "live": i == 1, "avatar": f"https://av/{i}.jpg"} for i in range(6)]
        g = {"id": "g1", "kind": "group", "name": "a, b", "url": None, "live": False, "avatar": None, "thumb": None, "members": mem}
        with mock.patch.object(server, "launcher_state", return_value=([{"title": "Groups", "tiles": [g]}], 0, 0)):
            t = self.c.get("/tv/data?k=" + server.TV_KEY).get_json()["rows"][0]["tiles"][0]
        self.assertEqual(len(t["members"]), 4)
        self.assertEqual([m["live"] for m in t["members"]], [False, True, False, False])
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



class CitySearchTests(unittest.TestCase):
    def test_search_lists_matches_best_first_and_ignores_short_text(self):
        import weather
        rows = {"results": [{"name": "Springfield", "admin1": "Illinois", "country_code": "US", "latitude": 39.8, "longitude": -89.65},
                            {"name": "Springfield", "admin1": "Missouri", "country_code": "US", "latitude": 37.2, "longitude": -93.3}, {"bad": 1}]}
        with mock.patch.object(weather, "_get", return_value=rows) as g:
            out = weather.search("Spring")
        self.assertEqual([p["name"] for p in out], ["Springfield, Illinois, US", "Springfield, Missouri, US"])
        self.assertIn("count=6", g.call_args[0][0])
        with mock.patch.object(weather, "_get") as g:
            self.assertEqual(weather.search("S"), [])
            g.assert_not_called()
        with mock.patch.object(weather, "_get", side_effect=OSError):
            self.assertEqual(weather.search("Vancouver"), [])

    def test_a_picked_place_is_saved_only_when_valid(self):
        import accounts, shutil, tempfile
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.dict(os.environ, {"TVREMOTE_DATA": d}), mock.patch.object(server, "DATA", d), mock.patch.object(server, "SETTINGS_FILE", os.path.join(d, "settings.json")), mock.patch.object(server.time, "sleep"):
            owner = founder_client(server)
            r = owner.post("/api/shell", json={"weather_place": {"name": "Calgary, Alberta, CA", "lat": 51.05, "lon": -114.07}})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["weather_place"], "Calgary, Alberta, CA")
            for bad in ({"name": "x", "lat": 99, "lon": 0}, {"name": "", "lat": 1, "lon": 1}, {"lat": 1, "lon": 1}, "text"):
                self.assertEqual(owner.post("/api/shell", json={"weather_place": bad}).status_code, 400)
            self.assertEqual(server.settings["weather"]["name"], "Calgary, Alberta, CA")
            server.settings.pop("weather", None)
            with mock.patch.object(server.weather, "search", return_value=[{"name": "A", "lat": 1.0, "lon": 2.0}]):
                self.assertEqual(owner.get("/api/weather/search?q=Al").get_json(), {"places": [{"name": "A", "lat": 1.0, "lon": 2.0}]})
            self.assertEqual(server.app.test_client().get("/api/weather/search?q=Al").status_code in (401, 403, 302, 303), True)


class VendorTests(unittest.TestCase):
    def test_the_bundled_hls_player_is_served_to_logged_in_users_only(self):
        import shutil, tempfile
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.dict(os.environ, {"TVREMOTE_DATA": d}), mock.patch.object(server, "DATA", d), mock.patch.object(server.time, "sleep"):
            owner = founder_client(server)
            r = owner.get("/vendor/hls.min.js")
            self.assertEqual(r.status_code, 200)
            self.assertIn("text/javascript", r.headers["Content-Type"])
            self.assertIn(b"Hls", r.data[-300:])
            self.assertIn(server.app.test_client().get("/vendor/hls.min.js").status_code, (302, 303, 401, 403))
        self.assertIn("/vendor/hls.min.js", server.GUEST_GET)  # a guest allowed to watch needs it too
