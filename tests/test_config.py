import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import app as server  # noqa: E402
import kick  # noqa: E402
import sessions  # noqa: E402
import throttle  # noqa: E402
from helpers import founder_client  # noqa: E402


class PhoneViewing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir),
                  mock.patch.object(server, "FAV_FILE", os.path.join(self.dir, "favourites.json")),
                  mock.patch.object(server, "SETTINGS_FILE", os.path.join(self.dir, "settings.json")),
                  mock.patch.dict(server.settings, {}, clear=False), mock.patch.object(server, "HOME", None),
                  mock.patch.object(kick, "DATA", self.dir), mock.patch.dict(sessions.key_cache, {"v": None}),
                  mock.patch.object(server, "CAST", None), mock.patch.object(server.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)
        throttle.fails.clear()
        self.admin = founder_client(server)

    def guest(self):
        url = self.admin.post("/api/invites", json={"hours": 1}).get_json()["url"]
        c = server.app.test_client()
        c.post("/" + url.split("/", 3)[3])
        return c

    def test_an_admin_sets_the_phone_picture_size_and_bad_values_are_refused(self):
        self.assertEqual(self.admin.get("/api/config").get_json()["phone_quality"], 720)
        self.assertTrue(self.admin.post("/api/config", json={"phone_quality": 480}).get_json()["ok"])
        self.assertEqual(self.admin.get("/api/config").get_json()["phone_quality"], 480)
        self.assertEqual(self.admin.post("/api/config", json={"phone_quality": 999}).status_code, 400)

    def test_guests_cannot_change_settings(self):
        g = self.guest()
        self.assertEqual(g.post("/api/config", json={"phone_quality": 360}).status_code, 403)
        self.assertEqual(g.get("/api/config").status_code, 403)

    def test_the_guest_switch_decides_whether_a_guest_may_watch(self):
        g = self.guest()
        self.assertTrue(g.get("/api/auth").get_json()["watch"])
        self.admin.post("/api/config", json={"phone_guests": False})
        self.assertFalse(g.get("/api/auth").get_json()["watch"])
        self.assertEqual(g.get("/api/watch").status_code, 403)
        self.assertTrue(self.admin.get("/api/auth").get_json()["watch"])  # an admin always may

    def test_watch_says_when_nothing_is_playing_and_never_sends_twitch(self):
        self.assertEqual(self.admin.get("/api/watch").status_code, 409)
        with mock.patch.dict(server.state, {"url": "https://www.twitch.tv/xqc"}), \
                mock.patch.object(server, "resolve", return_value=("Twitch", "streamlink")):
            self.assertEqual(self.admin.get("/api/watch").status_code, 422)

    def test_a_live_stream_gets_its_own_address_at_the_chosen_size(self):
        self.admin.post("/api/config", json={"phone_quality": 480})
        with mock.patch.dict(server.state, {"url": "https://kick.com/capped"}), \
                mock.patch.object(server, "resolve", return_value=("Kick", "streamlink")), \
                mock.patch.object(server, "stream_url", return_value="https://cdn.example/live.m3u8") as su, \
                mock.patch.object(server, "sprop", return_value=12.0):
            j = self.admin.get("/api/watch").get_json()
        self.assertEqual((j["url"], j["kind"], j["live"], j["quality"]), ("https://cdn.example/live.m3u8", "hls", True, 480))
        self.assertTrue(su.call_args[0][1].startswith("480p60,480p"))
        self.assertEqual(j["start"], 0)  # a live stream has no position to start from


    def test_with_several_streams_the_phone_can_pick_any_and_defaults_to_the_controlled_one(self):
        urls = {"https://kick.com/a": "A", "https://kick.com/b": "B", "https://kick.com/c": "C"}
        extras = [{"url": "https://kick.com/b"}, {"url": "https://kick.com/c"}]
        with mock.patch.dict(server.state, {"url": "https://kick.com/a"}), \
                mock.patch.object(server.wall, "layout", "grid"), mock.patch.object(server.wall, "extras", extras), \
                mock.patch.object(server.wall, "main_url", "https://kick.com/a"), mock.patch.object(server.wall, "selected", 2), \
                mock.patch.object(server, "resolve", return_value=("Kick", "streamlink")), mock.patch.object(server, "sprop", return_value=None), \
                mock.patch.object(server, "stream_url", side_effect=lambda u, q: "https://cdn.example/" + urls[u]):
            default = self.admin.get("/api/watch").get_json()
            first = self.admin.get("/api/watch?i=0").get_json()
            bad = self.admin.get("/api/watch?i=9")
        self.assertEqual((default["i"], default["url"]), (2, "https://cdn.example/C"))  # the stream being controlled
        self.assertEqual((first["i"], first["url"]), (0, "https://cdn.example/A"))
        self.assertEqual(bad.status_code, 409)  # no such stream


if __name__ == "__main__":
    unittest.main()
