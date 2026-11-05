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
import roku  # noqa: E402
import targets  # noqa: E402
import tvconfig  # noqa: E402
from helpers import founder_client  # noqa: E402

LIVING = {"ip": "192.168.1.141", "name": "MAIN ROOM", "brand": "TCL", "model": "55C803", "size": 55, "app_id": "tvinput.hdmi1"}
BEDROOM = {"ip": "192.168.1.142", "name": "Ben's Roku TV", "brand": "TCL", "model": "43S425-CA", "size": 43, "app_id": "tvinput.hdmi2"}
CAST = {"name": "Bedroom TV", "model": "Chromecast", "host": "192.168.1.156"}


class Parsing(unittest.TestCase):
    def test_ssdp_answers(self):
        ok = "HTTP/1.1 200 OK\r\nCache-Control: max-age=3600\r\nST: roku:ecp\r\nLOCATION: http://192.168.1.141:8060/\r\nUSN: uuid:roku:ecp:X\r\n\r\n"
        self.assertEqual(discover.parse_ssdp(ok), "192.168.1.141")
        self.assertIsNone(discover.parse_ssdp(ok.replace("roku:ecp\r\nLOC", "upnp:rootdevice\r\nLOC")))  # not a Roku
        self.assertIsNone(discover.parse_ssdp("garbage"))

    def test_the_media_player_answer_is_read_without_sending_anything(self):
        xml = '<player error="false" state="play"><plugin id="837" name="YouTube"/><position>35700 ms</position><duration>212000 ms</duration></player>'
        with mock.patch.object(roku, "_call", return_value=xml) as call:
            self.assertEqual(roku.media("192.168.1.141"), {"state": "play", "duration": 212.0})
            call.assert_called_once_with("192.168.1.141", "/query/media-player")  # a plain GET: no keypress, no POST
        with mock.patch.object(roku, "_call", return_value='<player error="false" state="close"/>'):
            self.assertEqual(roku.media("192.168.1.141"), {"state": "close", "duration": None})


class Guessing(unittest.TestCase):
    def test_the_pi_tv_is_found_by_its_hdmi_name_and_the_leftover_roku_goes_with_the_chromecast(self):
        g = discover.guess([BEDROOM, LIVING], [CAST], True, "55C803", "1.0.0.0")
        self.assertEqual((g["living"]["roku"], g["living"]["input"], g["living"]["enabled"]), (LIVING["ip"], "HDMI1", True))
        self.assertEqual((g["bedroom"]["roku"], g["bedroom"]["input"], g["bedroom"]["device"]), (BEDROOM["ip"], "HDMI2", "Bedroom TV"))
        self.assertTrue(g["living"]["why"] and g["bedroom"]["why"])  # every guess says why

    def test_nothing_is_claimed_without_evidence(self):
        g = discover.guess([LIVING, BEDROOM], [CAST], True, "SomethingElse", "1.0.0.0")
        self.assertEqual(g["living"]["roku"], "")  # the HDMI name matches neither
        self.assertEqual(g["bedroom"]["roku"], "")  # and two Rokus are left, so it won't pick one
        self.assertIn("pick one", " ".join(g["bedroom"]["why"]).lower())

    def test_no_screen_and_no_chromecast(self):
        g = discover.guess([LIVING], [], False, None, "1.0.0.0")
        self.assertFalse(g["living"]["enabled"])
        self.assertFalse(g["bedroom"]["enabled"])
        self.assertIsNone(g["bedroom"]["device"])

    def test_a_tv_that_would_not_answer_is_still_listed(self):
        def info(ip):
            if ip == "192.168.1.9":
                raise roku.RokuError("TV unreachable")
            return {"name": "x", "brand": "TCL", "model": "m", "size": 50, "app_id": None, "power": "PowerOn"}
        out = discover.describe(["192.168.1.8", "192.168.1.9"], info)
        self.assertEqual([o["ip"] for o in out], ["192.168.1.8", "192.168.1.9"])
        self.assertIn("error", out[1])


class Config(unittest.TestCase):
    def test_validation(self):
        ok = tvconfig.clean({"living": {"name": "  Living   room ", "roku": "192.168.1.141", "input": "HDMI1"},
                             "bedroom": {"roku": "", "input": "", "device": "Bedroom TV", "enabled": False}})
        self.assertEqual(ok["living"], {"name": "Living room", "roku": "192.168.1.141", "input": "HDMI1", "enabled": True})
        self.assertEqual((ok["bedroom"]["name"], ok["bedroom"]["enabled"], ok["bedroom"]["device"]), ("Bedroom", False, "Bedroom TV"))
        for bad_ip in ("8.8.8.8", "127.0.0.1", "169.254.1.1", "example.com", "192.168.1.1/24", "::1"):
            with self.assertRaises(ValueError, msg=bad_ip):
                tvconfig.clean({"living": {"roku": bad_ip}})  # a TV is on the home network, never "out there"
        with self.assertRaises(ValueError):
            tvconfig.clean({"living": {"input": "HDMI9"}})

    def test_saving_keeps_other_keys(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "targets.json"), "w") as f:
            json.dump({"bedroom": {"via": "proxy"}}, f)
        tvconfig.save(d, tvconfig.clean({"bedroom": {"name": "Guest room", "roku": "192.168.1.142"}}))
        saved = tvconfig.load(d)["bedroom"]
        self.assertEqual((saved["via"], saved["name"], saved["roku"]), ("proxy", "Guest room", "192.168.1.142"))


class Endpoints(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir),
                  mock.patch.object(server, "FAV_FILE", os.path.join(self.dir, "favourites.json")),
                  mock.patch.object(kick, "DATA", self.dir), mock.patch.dict(server._key, {"v": None}),
                  mock.patch.dict(server.ROKU_IPS, {}), mock.patch.dict(server.INPUTS, {}), mock.patch.dict(server.LABELS, {}),
                  mock.patch.dict(server.TV_ENABLED, {}), mock.patch.object(server, "CAST", None), mock.patch.object(server.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)
        server._fails.clear()
        self.admin = founder_client(server)

    def test_discovery_returns_what_it_found_with_guesses_and_is_admin_only(self):
        with mock.patch.object(discover, "roku_scan", return_value=[LIVING["ip"], BEDROOM["ip"]]), \
                mock.patch.object(discover, "describe", return_value=[LIVING, BEDROOM]), \
                mock.patch.object(discover, "display_connected", return_value=True), \
                mock.patch.object(server, "tv_cec", return_value={"name": "55C803", "power": "on"}):
            j = self.admin.get("/api/discover").get_json()
            self.assertEqual([r["ip"] for r in j["rokus"]], [LIVING["ip"], BEDROOM["ip"]])
            self.assertEqual(j["cec_name"], "55C803")
            self.assertEqual(j["guess"]["living"]["roku"], LIVING["ip"])
            accounts.add("sam", "secret1")
            sam = server.app.test_client()
            sam.post("/login", data={"username": "sam", "password": "secret1"})
            self.assertEqual(sam.get("/api/discover").status_code, 403)

    def test_saving_applies_at_once_and_survives_a_restart(self):
        body = {"living": {"name": "Lounge", "roku": LIVING["ip"], "input": "HDMI1"},
                "bedroom": {"name": "Spare room", "roku": BEDROOM["ip"], "input": "HDMI2", "device": "Bedroom TV", "enabled": False}}
        self.assertEqual(self.admin.post("/api/targets/config", json=body).status_code, 200)
        self.assertEqual((server.ROKU_IPS["living"], server.INPUTS["bedroom"], server.LABELS["living"]), (LIVING["ip"], "HDMI2", "Lounge"))
        self.assertFalse(server.TV_ENABLED["bedroom"])
        server.ROKU_IPS["living"] = ""
        server.apply_tv_config(tvconfig.load(self.dir))  # what startup does
        self.assertEqual(server.ROKU_IPS["living"], LIVING["ip"])
        self.assertEqual(self.admin.post("/api/targets/config", json={"living": {"roku": "8.8.8.8"}}).status_code, 400)

    def test_a_switched_off_tv_is_not_listed(self):
        provider = lambda: {"name": "x", "power": "PowerOn"}  # noqa: E731
        fresh = targets.Cards({"living": provider, "bedroom": provider}, ["living", "bedroom"], timeout=0.3)
        fresh.refresh_now()
        with mock.patch.object(server, "cards", fresh):
            self.assertEqual([c["id"] for c in self.admin.get("/api/targets").get_json()], ["living", "bedroom"])
            server.TV_ENABLED["living"] = False
            self.assertEqual([c["id"] for c in self.admin.get("/api/targets").get_json()], ["bedroom"])

    def test_the_roku_test_presses_info_only_and_only_for_a_home_network_address(self):
        with mock.patch.object(roku, "key") as key:
            self.assertEqual(self.admin.post("/api/targets/test", json={"roku": "8.8.8.8"}).status_code, 400)
            self.assertEqual(self.admin.post("/api/targets/test", json={"roku": LIVING["ip"]}).status_code, 200)
            key.assert_called_once_with(LIVING["ip"], "Info")


class WhatTheTvIsPlayingByItself(unittest.TestCase):
    """The TV was started by hand (its own YouTube app). The page shows it, and nothing here ever sends the TV a key."""

    def setUp(self):
        for p in (mock.patch.object(accounts, "initialised", lambda: True), mock.patch.object(server, "session_user", lambda tok: accounts.OWNER),
                  mock.patch.object(server, "pi_status", return_value={"running": False}), mock.patch.object(server, "CAST", None),
                  mock.patch.dict(server.ROKU_IPS, {"living": LIVING["ip"], "bedroom": ""})):
            p.start()
            self.addCleanup(p.stop)
        self.client = server.app.test_client()

    def details(self, **kw):
        base = {"power": "PowerOn", "app": "YouTube", "app_id": "837", "media": {"state": "play", "duration": 212.0}, "brand": "TCL", "model": "55C803"}
        return mock.patch.object(server, "roku_details", return_value={**base, **kw})

    def test_which_apps_count(self):
        ext = server.external_app
        self.assertEqual(ext({"power": "PowerOn", "app": "YouTube", "app_id": "837"}), "YouTube")
        self.assertIsNone(ext({"power": "PowerOn", "app": "Pi", "app_id": "tvinput.hdmi1"}))  # an HDMI input: the Pi, or a console
        self.assertIsNone(ext({"power": "PowerOn", "app": "Roku", "app_id": None}))  # the home screen
        self.assertIsNone(ext({"power": "Ready", "app": "YouTube", "app_id": "837"}))  # in standby
        self.assertIsNone(ext({}))

    def test_status_shows_it_and_sends_nothing(self):
        with self.details(), mock.patch.object(roku, "key") as key, mock.patch.object(roku, "_call") as call:
            j = self.client.get("/api/status").get_json()
        self.assertEqual((j["external"]["app"], j["external"]["paused"], j["running"]), ("YouTube", False, False))
        key.assert_not_called()
        call.assert_not_called()  # the answer came from the cached details: not even a read

    def test_paused_and_the_card_line(self):
        with self.details(media={"state": "pause", "duration": None}):
            self.assertTrue(self.client.get("/api/status").get_json()["external"]["paused"])
            card = server.make_card("living", {"running": False})
        self.assertEqual((card["external"]["app"], card["now"]), ("YouTube", None))

    def test_nothing_external_when_this_remote_is_playing_or_the_tv_is_on_the_pi(self):
        with self.details():
            with mock.patch.object(server, "pi_status", return_value={"running": True, "title": "x"}):
                self.assertIsNone(self.client.get("/api/status").get_json().get("external"))
        with self.details(app_id="tvinput.hdmi1", app="Pi"):
            self.assertIsNone(self.client.get("/api/status").get_json()["external"])


if __name__ == "__main__":
    unittest.main()
