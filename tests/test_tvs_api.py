import json
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
import discover  # noqa: E402
import kick  # noqa: E402
import sessions  # noqa: E402
import targets  # noqa: E402
import throttle  # noqa: E402
import tvconfig  # noqa: E402
from helpers import founder_client  # noqa: E402

BED, LIV = "192.168.1.142", "192.168.1.141"
TVS = {"tvs": [{"id": "tv1", "name": "Bedroom", "roku": BED, "pi": {"input": "HDMI3"}, "chromecast": {"device": "Bedroom TV", "input": "HDMI1"}},
               {"id": "tv2", "name": "Living room", "roku": LIV, "pi": None, "chromecast": None}]}


class TvsApi(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir),
                  mock.patch.object(server, "FAV_FILE", os.path.join(self.dir, "favourites.json")),
                  mock.patch.object(kick, "DATA", self.dir), mock.patch.dict(sessions.key_cache, {"v": None}),
                  mock.patch.dict(server.ROKU_IPS, {}, clear=True), mock.patch.dict(server.INPUTS, {}, clear=True), mock.patch.dict(server.LABELS, {}, clear=True),
                  mock.patch.dict(server.TV_ENABLED, {}, clear=True), mock.patch.dict(server.KINDS, {}, clear=True), mock.patch.dict(server.TV_ONLY, {}, clear=True),
                  mock.patch.object(server, "ORDER", ["living", "bedroom"]), mock.patch.object(server, "CAST", None),
                  mock.patch.object(server, "cards", targets.Cards({}, [])), mock.patch.object(server.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)
        server.KINDS.update(living=("pi", "Pi on HDMI"), bedroom=("chromecast", "Chromecast"))
        throttle.fails.clear()
        self.admin = founder_client(server)

    def test_saving_applies_at_once_and_gives_the_tv_with_nothing_plugged_in_its_own_card(self):
        r = self.admin.post("/api/tvs", json=TVS)
        self.assertEqual(r.status_code, 200, r.get_data())
        self.assertEqual((server.ROKU_IPS["living"], server.ROKU_IPS["bedroom"], server.ROKU_IPS["tv2"]), (BED, BED, LIV))
        self.assertEqual((server.INPUTS["living"], server.INPUTS["bedroom"]), ("HDMI3", "HDMI1"))
        self.assertEqual((server.LABELS["living"], server.LABELS["bedroom"], server.LABELS["tv2"]),
                         ("Bedroom · Pi", "Bedroom · Chromecast", "Living room"))
        self.assertEqual(server.KINDS["tv2"][0], "tv")
        self.assertEqual(server.ORDER, ["living", "bedroom", "tv2"])
        self.assertEqual(sorted(server.cards.providers), ["bedroom", "living", "tv2"])

    def test_removing_a_tv_removes_its_card_and_settings(self):
        self.admin.post("/api/tvs", json=TVS)
        self.admin.post("/api/tvs", json={"tvs": TVS["tvs"][:1]})
        self.assertNotIn("tv2", server.ROKU_IPS)
        self.assertNotIn("tv2", server.TV_ONLY)
        self.assertEqual(server.ORDER, ["living", "bedroom"])

    def test_no_pi_means_the_pi_endpoint_is_off(self):
        self.admin.post("/api/tvs", json={"tvs": [{"name": "Den", "roku": LIV, "chromecast": {"device": "X"}}]})
        self.assertFalse(server.TV_ENABLED["living"])
        self.assertTrue(server.TV_ENABLED["bedroom"])

    def test_bad_lists_are_refused_with_a_sentence_and_change_nothing(self):
        r = self.admin.post("/api/tvs", json={"tvs": [{"name": "A", "pi": {}}, {"name": "B", "pi": {}}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Pi", r.get_json()["error"])
        self.assertFalse(any(server.ROKU_IPS.values()))  # nothing was applied
        self.assertFalse(os.path.exists(os.path.join(self.dir, "tvs.json")))

    def test_a_tv_with_nothing_plugged_in_reports_idle_and_refuses_to_play(self):
        self.admin.post("/api/tvs", json=TVS)
        s = self.admin.get("/api/status?target=tv2").get_json()
        self.assertEqual((s["running"], s["tvonly"]), (False, True))
        r = self.admin.post("/api/play?target=tv2", json={"url": "https://kick.com/x"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("plugged in", r.get_json()["error"])  # never falls back to the Pi

    def test_the_tv_list_endpoints_are_admin_only(self):
        accounts.add("sam", "secret1")
        sam = server.app.test_client()
        sam.post("/login", data={"username": "sam", "password": "secret1"})
        self.assertEqual(sam.get("/api/tvs").status_code, 403)
        self.assertEqual(sam.post("/api/tvs", json=TVS).status_code, 403)

    def test_discover_returns_a_suggestion_per_tv(self):
        info = {"ip": LIV, "name": "MAIN ROOM", "brand": "TCL", "model": "55C803", "size": 55, "app_id": None}
        with mock.patch.object(discover, "roku_scan", return_value=[LIV]), mock.patch.object(discover, "describe", return_value=[info]), \
                mock.patch.object(discover, "display_connected", return_value=True), mock.patch.object(server, "cec_phys", return_value="3.0.0.0"), \
                mock.patch.object(server, "tv_cec", return_value={"name": "55C803", "power": "on"}):
            j = self.admin.get("/api/discover").get_json()
        self.assertEqual((j["pi_input"], j["tvs"][0]["roku"], j["tvs"][0]["pi"]), ("HDMI3", LIV, {"input": "HDMI3"}))

    def test_startup_migrates_the_old_file_once_and_leaves_it_in_place(self):
        old = {"living": {"name": "Living room", "roku": LIV, "input": "HDMI1", "enabled": True},
               "bedroom": {"name": "Bedroom", "roku": BED, "input": "HDMI1", "enabled": True, "device": "Bedroom TV"}}
        with open(os.path.join(self.dir, "targets.json"), "w") as f:
            json.dump(old, f)
        server.init_tvs(self.dir)
        self.assertEqual((server.ROKU_IPS["living"], server.ROKU_IPS["bedroom"]), (LIV, BED))
        self.assertEqual(len(tvconfig.load_tvs(self.dir)), 2)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "tvs.json")))
        with open(os.path.join(self.dir, "targets.json")) as f:
            self.assertEqual(json.load(f), old)

    def test_a_fresh_install_starts_with_no_tvs(self):
        server.init_tvs(self.dir)
        self.assertFalse(server.TV_ENABLED["living"] or server.TV_ENABLED["bedroom"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "tvs.json")))


if __name__ == "__main__":
    unittest.main()
