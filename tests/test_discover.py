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
import throttle  # noqa: E402
import sessions  # noqa: E402
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

    def test_a_tv_that_is_off_and_silent_over_hdmi_says_so_and_pairs_nothing(self):
        g = discover.guess([LIVING, BEDROOM], [CAST], True, None, "1.0.0.0")  # what happened on the real Pi: the living-room TV was off
        self.assertEqual((g["living"]["roku"], g["bedroom"]["roku"]), ("", ""))
        self.assertIn("didn't tell us its name", " ".join(g["living"]["why"]))
        self.assertIn("Look again", " ".join(g["living"]["why"]))

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
                  mock.patch.object(kick, "DATA", self.dir), mock.patch.dict(sessions.key_cache, {"v": None}),
                  mock.patch.dict(server.ROKU_IPS, {}), mock.patch.dict(server.INPUTS, {}), mock.patch.dict(server.LABELS, {}),
                  mock.patch.dict(server.TV_ENABLED, {}), mock.patch.object(server, "CAST", None), mock.patch.object(server.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)
        throttle.fails.clear()
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

    def test_refresh_asks_every_tv_again_and_returns_the_cards(self):
        server.ROKU_IPS.update(living=LIVING["ip"], bedroom="")
        with mock.patch.object(server, "fetch_roku") as fetch, mock.patch.object(server.cards, "refresh_now") as again:
            r = self.admin.post("/api/targets/refresh")
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.get_json(), list)
        fetch.assert_called_once_with("living")  # only TVs that have a Roku
        again.assert_called_once()

    def test_the_roku_test_presses_info_only_and_only_for_a_home_network_address(self):
        with mock.patch.object(roku, "key") as key:
            self.assertEqual(self.admin.post("/api/targets/test", json={"roku": "8.8.8.8"}).status_code, 400)
            self.assertEqual(self.admin.post("/api/targets/test", json={"roku": LIVING["ip"]}).status_code, 200)
            key.assert_called_once_with(LIVING["ip"], "Info")


class StatusFreshness(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(accounts, "initialised", lambda: True), mock.patch.object(server, "session_user", lambda tok: accounts.OWNER),
                  mock.patch.object(server, "CAST", None), mock.patch.dict(server.ROKU_IPS, {"living": LIVING["ip"], "bedroom": ""})):
            p.start()
            self.addCleanup(p.stop)
        self.client = server.app.test_client()

    def test_the_tv_is_asked_often_while_a_phone_is_looking_and_slowly_otherwise(self):
        server._viewed["t"] = 0.0
        self.assertEqual(server.roku_ttl(), server.ROKU_TTL)
        self.client.get("/api/targets")
        self.assertEqual(server.roku_ttl(), server.ROKU_TTL_LIVE)
        self.assertLess(server.ROKU_TTL_LIVE, 5)
        t = server.time.time()
        with mock.patch.object(server.time, "time", return_value=t + 31):
            self.assertEqual(server.roku_ttl(), server.ROKU_TTL)  # the phone went away

    def test_power_and_input_keys_trigger_the_quick_recheck_but_other_keys_do_not(self):
        with mock.patch.object(roku, "key"), mock.patch.object(server, "refresh_roku_soon") as soon:
            for k in ("PowerOn", "PowerOff", "InputHDMI2"):
                self.client.post("/api/roku/key?target=living", json={"key": k})
            self.assertEqual(soon.call_count, 3)
            self.client.post("/api/roku/key?target=living", json={"key": "VolumeUp"})
            self.assertEqual(soon.call_count, 3)

    def test_the_recheck_asks_the_tv_at_one_three_six_and_ten_seconds(self):
        waits = []

        class Now:  # run the background thread's work right here, so the test sees exactly its sleeps and fetches
            def __init__(self, target=None, args=(), daemon=None):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        with mock.patch.object(server.time, "sleep", side_effect=waits.append), mock.patch.object(server, "fetch_roku") as fetch,                 mock.patch.object(server.threading, "Thread", Now):
            server.refresh_roku_soon("living")
        self.assertEqual((waits, fetch.call_count), ([1, 2, 3, 4], 4))


class MoveSwitchesBackOff(unittest.TestCase):
    """Move wakes a TV that was off, and puts it back to sleep when you move away from it, but only when that is safe."""

    def setUp(self):
        self.power = {"living": "PowerOn", "bedroom": "Ready"}  # the bedroom TV is off
        self.app_id = {"living": "tvinput.hdmi1", "bedroom": "tvinput.hdmi1"}
        ips = {"living": LIVING["ip"], "bedroom": BEDROOM["ip"]}
        info = lambda ip: {"power": self.power[next(k for k, v in ips.items() if v == ip)], "app_id": self.app_id[next(k for k, v in ips.items() if v == ip)]}  # noqa: E731
        self.keys = []
        self.played = []
        for p in (mock.patch.object(accounts, "initialised", lambda: True), mock.patch.object(server, "session_user", lambda tok: accounts.OWNER),
                  mock.patch.object(server, "CAST", mock.MagicMock()), mock.patch.dict(server.ROKU_IPS, ips),
                  mock.patch.dict(server.INPUTS, {"living": "HDMI1", "bedroom": "HDMI1"}), mock.patch.object(roku, "info", side_effect=info),
                  mock.patch.object(roku, "key", side_effect=lambda ip, k: self.keys.append((ip, k))),
                  mock.patch.object(server, "play_on", side_effect=lambda c, url, q, via=None: self.played.append((getattr(c, "id", "living"), url))),
                  mock.patch.object(server, "stop_pi"), mock.patch.object(server, "refresh_roku_soon")):
            p.start()
            self.addCleanup(p.stop)
        server._woken.clear()
        self.addCleanup(server._woken.clear)
        self.client = server.app.test_client()

    def move(self, src, dst):
        return self.client.post("/api/move", json={"from": src, "to": dst, "url": "https://kick.com/capped"})

    def test_moving_to_an_off_tv_marks_it_and_moving_back_switches_it_off(self):
        r = self.move("living", "bedroom")
        self.assertEqual((r.status_code, r.get_json()["switched_off"]), (200, False))  # the living room was on before: left on
        self.assertIn("bedroom", server._woken)
        self.power["bedroom"] = "PowerOn"  # the move woke it
        r = self.move("bedroom", "living")
        self.assertTrue(r.get_json()["switched_off"])
        self.assertEqual(self.keys, [(BEDROOM["ip"], "PowerOff")])
        self.assertNotIn("bedroom", server._woken)

    def test_a_tv_that_was_already_on_is_never_switched_off(self):
        self.power["bedroom"] = "PowerOn"
        self.move("living", "bedroom")
        self.assertNotIn("bedroom", server._woken)
        self.assertFalse(self.move("bedroom", "living").get_json()["switched_off"])
        self.assertEqual(self.keys, [])

    def test_a_tv_someone_switched_to_a_console_in_the_meantime_is_left_on(self):
        self.move("living", "bedroom")
        self.power["bedroom"] = "PowerOn"
        self.app_id["bedroom"] = "tvinput.hdmi3"  # the roommate is on the Switch
        self.assertFalse(self.move("bedroom", "living").get_json()["switched_off"])
        self.assertEqual(self.keys, [])

    def test_a_tv_switched_off_by_hand_is_forgotten(self):
        self.move("living", "bedroom")
        with mock.patch.object(server, "ROKU_TTL_LIVE", 0), mock.patch.object(roku, "info", return_value={"power": "Ready", "app": "Roku"}):
            server.fetch_roku("bedroom")
        self.assertNotIn("bedroom", server._woken)

    def test_an_old_mark_expires(self):
        server._woken["bedroom"] = server.time.time() - server.WOKEN_TTL - 1
        self.power["bedroom"] = "PowerOn"
        self.assertFalse(server.power_back_off("bedroom"))
        self.assertEqual(self.keys, [])

    def test_a_tv_with_no_chosen_input_is_never_switched_off(self):
        server.INPUTS["bedroom"] = ""
        self.move("living", "bedroom")
        self.power["bedroom"] = "PowerOn"
        self.assertFalse(self.move("bedroom", "living").get_json()["switched_off"])  # we can't tell it is still on our input

    def test_a_failed_start_leaves_everything_alone(self):
        with mock.patch.object(server, "play_on", side_effect=RuntimeError("Chromecast couldn't load the stream")):
            r = self.move("living", "bedroom")
        self.assertEqual(r.status_code, 502)
        self.assertEqual((server._woken, self.keys), ({}, []))
        server.stop_pi.assert_not_called()  # what is playing here keeps playing

    def test_bad_requests_are_refused(self):
        self.assertEqual(self.move("living", "living").status_code, 400)
        self.assertEqual(self.move("living", "attic").status_code, 400)
        self.assertEqual(self.client.post("/api/move", json={"from": "living", "to": "bedroom", "url": "not a link"}).status_code, 400)


class TurnOn(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(accounts, "initialised", lambda: True), mock.patch.object(server, "session_user", lambda tok: accounts.OWNER),
                  mock.patch.object(server, "CAST", None), mock.patch.object(server, "switch_tv_to_pi"), mock.patch.object(server, "switch_input"),
                  mock.patch.object(server, "refresh_roku_soon")):
            p.start()
            self.addCleanup(p.stop)
        self.client = server.app.test_client()

    def wake(self, target, ips, inputs, power="Ready"):
        with mock.patch.dict(server.ROKU_IPS, ips), mock.patch.dict(server.INPUTS, inputs),                 mock.patch.object(roku, "info", return_value={"power": power}), mock.patch.object(roku, "key") as key:
            self.assertEqual(self.client.post(f"/api/tv/wake?target={target}").status_code, 200)
            for t in __import__("threading").enumerate():
                if t is not __import__("threading").current_thread() and t.daemon:
                    t.join(2)
            return key

    def test_a_roku_with_an_input_picks_it(self):
        self.wake("bedroom", {"bedroom": BEDROOM["ip"]}, {"bedroom": "HDMI1"})
        server.switch_input.assert_called_once_with("bedroom")

    def test_a_roku_with_no_input_is_still_turned_on(self):  # the bedroom's Turn on used to do nothing here
        key = self.wake("bedroom", {"bedroom": BEDROOM["ip"]}, {"bedroom": ""})
        key.assert_called_once_with(BEDROOM["ip"], "PowerOn")
        server.switch_input.assert_not_called()

    def test_a_tv_that_is_already_on_is_left_alone(self):
        key = self.wake("bedroom", {"bedroom": BEDROOM["ip"]}, {"bedroom": ""}, power="PowerOn")
        key.assert_not_called()

    def test_the_living_room_is_also_woken_over_hdmi(self):
        self.wake("living", {"living": LIVING["ip"]}, {"living": ""})
        server.switch_tv_to_pi.assert_called_once()


class InputSwitchSaysWhyItFailed(unittest.TestCase):
    def test_no_saved_settings_and_a_refused_command_are_both_logged(self):
        with mock.patch.dict(server.ROKU_IPS, {"living": "", "bedroom": ""}), mock.patch.dict(server.INPUTS, {"living": "", "bedroom": ""}):
            with self.assertLogs(server.app.logger, "WARNING") as cm:
                server.switch_input("living")
        self.assertIn("no Roku address", cm.output[0])
        with mock.patch.dict(server.ROKU_IPS, {"living": LIVING["ip"]}), mock.patch.dict(server.INPUTS, {"living": "HDMI1"}),                 mock.patch.object(roku, "info", return_value={"power": "PowerOn", "app_id": "837"}),                 mock.patch.object(roku, "key", side_effect=roku.RokuError("TV refused: set Control by mobile apps to Permissive")):
            with self.assertLogs(server.app.logger, "WARNING") as cm:
                server.switch_input("living")
        self.assertIn("Permissive", cm.output[0])


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
