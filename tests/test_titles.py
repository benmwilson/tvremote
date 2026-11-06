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

URL = "https://kick.com/omie"


class Titles(unittest.TestCase):
    def test_a_stream_with_an_author_but_no_title_in_its_details_gets_the_live_checks_title(self):
        with mock.patch.dict(server._qcache, {URL: (0, 0, {"author": "Omie"})}), \
                mock.patch.object(server.livewatch, "snapshot", return_value={URL: {"title": "GTA night"}}):
            self.assertEqual(server.display_title(URL), "Omie: GTA night")

    def test_the_details_title_wins_and_the_channel_name_is_the_last_resort(self):
        with mock.patch.dict(server._qcache, {URL: (0, 0, {"author": "Omie", "title": "From details"})}), \
                mock.patch.object(server.livewatch, "snapshot", return_value={URL: {"title": "From the check"}}):
            self.assertEqual(server.display_title(URL), "Omie: From details")
        with mock.patch.dict(server._qcache, {}, clear=True), mock.patch.object(server.livewatch, "snapshot", return_value={}):
            self.assertEqual(server.display_title(URL), server.fav_name(URL))


class FavouriteTitles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir),
                  mock.patch.object(server, "FAV_FILE", os.path.join(self.dir, "favourites.json")),
                  mock.patch.object(kick, "DATA", self.dir), mock.patch.dict(sessions.key_cache, {"v": None}),
                  mock.patch.object(server.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)
        throttle.fails.clear()
        self.admin = founder_client(server)

    def test_an_offline_favourite_still_carries_the_last_title_it_was_seen_with(self):
        snap = {"https://kick.com/4head": {"live": False, "title": "4HEAD | NOPIXEL", "game": "GTA", "last_live": 100.0, "checked": 1.0}}
        with mock.patch.object(server, "load_favs", return_value=[{"name": "4head", "url": "https://kick.com/4head", "source": "Kick"}]), \
                mock.patch.object(server.livewatch, "snapshot", return_value=snap), mock.patch.object(server.livewatch, "poke"):
            fav = self.admin.get("/api/favourites").get_json()[0]
        self.assertEqual((fav["live"], fav["title"]), (False, "4HEAD | NOPIXEL"))


if __name__ == "__main__":
    unittest.main()
