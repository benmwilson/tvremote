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
from helpers import founder_client  # noqa: E402

_REAL_SESSION_USER = server.session_user

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


class PiPort(unittest.TestCase):
    def test_the_pis_hdmi_port_is_read_from_cec_output(self):
        out = "Driver Info:\n\tPhysical Address       : 2.0.0.0\n"
        self.assertEqual(discover.parse_phys(out), "2.0.0.0")
        self.assertEqual(discover.phys_input("2.0.0.0"), "HDMI2")
        self.assertIsNone(discover.parse_phys("Physical Address : f.f.f.f"))  # no TV plugged in
        self.assertIsNone(discover.parse_phys(""))
        self.assertEqual(discover.phys_input("0.0.0.0"), "")  # not a port on the TV

class Guessing(unittest.TestCase):
    def test_a_tv_that_would_not_answer_is_still_listed(self):
        def info(ip):
            if ip == "192.168.1.9":
                raise roku.RokuError("TV unreachable")
            return {"name": "x", "brand": "TCL", "model": "m", "size": 50, "app_id": None, "power": "PowerOn"}
        out = discover.describe(["192.168.1.8", "192.168.1.9"], info)
        self.assertEqual([o["ip"] for o in out], ["192.168.1.8", "192.168.1.9"])
        self.assertIn("error", out[1])


class Suggesting(unittest.TestCase):
    def tv(self, id, name, roku, pi=None, cc=None):
        return {"id": id, "name": name, "roku": roku, "pi": pi, "chromecast": cc}

    def test_the_pi_is_ticked_on_the_roku_whose_model_matches_the_cec_name(self):
        out = discover.suggest([BEDROOM, LIVING], [CAST], True, "55C803", "HDMI1", [])
        by = {t["roku"]: t for t in out}
        self.assertEqual(by[LIVING["ip"]]["pi"], {"input": "HDMI1"})
        self.assertIsNone(by[BEDROOM["ip"]]["pi"])
        self.assertEqual(by[BEDROOM["ip"]]["chromecast"], {"device": "Bedroom TV", "input": "HDMI2"})  # the one leftover Roku, showing HDMI 2
        self.assertTrue(all(t["why"] for t in out))
        self.assertTrue(all(t["found"] and t["id"] == "" for t in out))  # new TVs get ids when saved

    def test_saved_values_win_and_a_changed_hdmi_port_is_reported(self):
        saved = [self.tv("tv1", "Bedroom", BEDROOM["ip"], {"input": "HDMI1"}, {"device": "Bedroom TV", "input": "HDMI1"})]
        out = discover.suggest([LIVING, BEDROOM], [CAST], True, None, "HDMI3", saved)
        bed = next(t for t in out if t["roku"] == BEDROOM["ip"])
        self.assertEqual((bed["id"], bed["name"], bed["pi"]), ("tv1", "Bedroom", {"input": "HDMI1"}))
        self.assertEqual(bed["pi_detected"], "HDMI3")  # differs from the saved HDMI1: the screen offers "Use HDMI 3"
        liv = next(t for t in out if t["roku"] == LIVING["ip"])
        self.assertEqual((liv["pi"], liv["chromecast"], liv["pi_detected"]), (None, None, ""))  # nothing is guessed once things are saved

    def test_a_saved_tv_the_scan_missed_is_kept_and_marked(self):
        saved = [self.tv("tv1", "Den", "192.168.1.77", {"input": "HDMI1"}), self.tv("tv2", "Cast only", "", None, {"device": "X", "input": ""})]
        out = discover.suggest([], [], True, None, "HDMI1", saved)
        self.assertEqual([(t["name"], t["found"]) for t in out], [("Den", False), ("Cast only", True)])  # no Roku set is not "didn't answer"

    def test_no_screen_on_the_pi_means_no_pi_is_suggested(self):
        out = discover.suggest([LIVING], [], False, None, "", [])
        self.assertIsNone(out[0]["pi"])
        self.assertIn("screen", " ".join(out[0]["why"]).lower())

    def test_two_rokus_and_no_hdmi_name_guess_nothing_and_say_why(self):
        out = discover.suggest([LIVING, BEDROOM], [CAST], True, None, "HDMI1", [])
        self.assertTrue(all(t["pi"] is None and t["chromecast"] is None for t in out))
        self.assertIn("Scan again", " ".join(out[0]["why"] + out[1]["why"]))

    def test_a_roku_that_came_back_at_a_new_address_says_how_to_move_the_old_settings(self):
        saved = [self.tv("tv1", "Bedroom", "192.168.1.200", {"input": "HDMI1"})]  # the TV is now at .142: the saved address is gone
        out = discover.suggest([BEDROOM], [], True, None, "HDMI1", saved)
        new, old = out[0], out[1]
        self.assertEqual((new["roku"], new["id"], old["found"]), (BEDROOM["ip"], "", False))
        self.assertIn("new address", " ".join(new["why"]))

    def test_a_saved_pi_with_no_screen_attached_warns(self):
        saved = [self.tv("tv1", "Den", LIVING["ip"], {"input": "HDMI1"})]
        out = discover.suggest([LIVING], [], False, None, "", saved)
        self.assertIn("No screen", " ".join(out[0]["why"]))
        self.assertEqual(out[0]["pi_detected"], "")

    def test_one_roku_gets_the_pi_and_the_chromecast(self):
        out = discover.suggest([BEDROOM], [CAST], True, None, "HDMI3", [])
        self.assertEqual((out[0]["pi"], out[0]["chromecast"]["device"]), ({"input": "HDMI3"}, "Bedroom TV"))


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

    def test_a_move_and_an_app_launch_are_recorded_in_the_activity_log(self):
        import activity
        activity.reset()
        with mock.patch.object(server, "play_on"), mock.patch.object(server, "stop_pi"), mock.patch.object(server, "tv_is_off", return_value=False),                 mock.patch.object(server, "refresh_roku_soon"), mock.patch.object(server, "power_back_off", return_value=False):
            self.move("living", "bedroom")
        self.assertEqual(activity.last("owner")["text"], "Moved capped from living to bedroom")
        with mock.patch.object(roku, "_call", return_value=APPS_XML), mock.patch.object(server, "tv_is_off", return_value=False),                 mock.patch.dict(server.ROKU_IPS, {"living": LIVING["ip"]}):
            server._apps.clear()
            self.client.post("/api/roku/launch?target=living", json={"id": "837"})
        self.assertEqual(activity.last("owner")["text"], "Opened a TV app (living)")

    def test_bad_requests_are_refused(self):
        self.assertEqual(self.move("living", "living").status_code, 400)
        self.assertEqual(self.move("living", "attic").status_code, 400)
        self.assertEqual(self.client.post("/api/move", json={"from": "living", "to": "bedroom", "url": "not a link"}).status_code, 400)


APPS_XML = """<apps>
<app id="tvinput.hdmi1" type="tvin" version="1.0.0">HDMI 1</app>
<app id="837" type="appl" version="2.1.4">YouTube</app>
<app id="12" type="appl" version="4.9.1">Netflix</app>
<app id="13535" type="appl" version="5.1">Plex - Free Movies &amp; TV</app>
<app id="562859" type="menu" version="1.0.0">Roku Dynamic Menu</app>
</apps>"""


PNG = bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"pixels"


class RokuApps(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(accounts, "initialised", lambda: True), mock.patch.object(server, "session_user", lambda tok: accounts.OWNER),
                  mock.patch.object(server, "CAST", None), mock.patch.dict(server.ROKU_IPS, {"living": LIVING["ip"], "bedroom": ""}),
                  mock.patch.object(server, "refresh_roku_soon")):
            p.start()
            self.addCleanup(p.stop)
        server._apps.clear()
        server._icons.clear()
        self.client = server.app.test_client()

    def test_only_real_apps_are_listed_with_clean_names(self):
        with mock.patch.object(roku, "_call", return_value=APPS_XML):
            self.assertEqual(roku.apps("1.2.3.4"), [{"id": "837", "name": "YouTube"}, {"id": "12", "name": "Netflix"},
                                                    {"id": "13535", "name": "Plex - Free Movies & TV"}])  # no HDMI input, no menu; &amp; decoded

    def test_an_app_name_with_an_ampersand_is_decoded(self):
        def fake(ip, path, method="GET", timeout=4):
            if path == "/query/device-info":
                return "<device-info><power-mode>PowerOn</power-mode></device-info>"
            return '<active-app><app id="13535" type="appl" version="5.1">Plex - Free Movies &amp; TV</app></active-app>'
        with mock.patch.object(roku, "_call", side_effect=fake):
            self.assertEqual(roku.info("1.2.3.4")["app"], "Plex - Free Movies & TV")

    def test_the_list_is_kept_for_a_while(self):
        with mock.patch.object(roku, "_call", return_value=APPS_XML) as call:
            self.assertEqual(len(self.client.get("/api/roku/apps?target=living").get_json()), 3)
            self.client.get("/api/roku/apps?target=living")
            self.assertEqual(call.call_count, 1)

    def test_launching_an_installed_app_posts_to_that_app_only(self):
        with mock.patch.object(roku, "_call", return_value=APPS_XML) as call, mock.patch.object(server, "tv_is_off", return_value=False):
            self.assertEqual(self.client.post("/api/roku/launch?target=living", json={"id": "837"}).get_json(), {"ok": True})
            call.assert_called_with(LIVING["ip"], "/launch/837", "POST")

    def test_an_app_that_is_not_installed_or_a_made_up_id_is_refused(self):
        with mock.patch.object(roku, "_call", return_value=APPS_XML) as call:
            for bad in ("99999", "../keypress/PowerOff", "837/../x", ""):
                self.assertEqual(self.client.post("/api/roku/launch?target=living", json={"id": bad}).status_code, 400, bad)
            self.assertNotIn("/launch/", " ".join(str(c) for c in call.call_args_list))
        with self.assertRaises(roku.RokuError):
            roku.launch("1.2.3.4", "1; rm")

    def test_a_tv_in_standby_is_woken_first_and_the_app_follows(self):
        calls, waits = [], []

        class Now:
            def __init__(self, target=None, args=(), daemon=None):
                self.target = target

            def start(self):
                self.target()

        with mock.patch.object(roku, "_call", side_effect=lambda ip, path, method="GET", timeout=4: calls.append((path, method)) or APPS_XML),                 mock.patch.object(roku, "key", side_effect=lambda ip, k: calls.append(("key", k))), mock.patch.object(server, "tv_is_off", return_value=True),                 mock.patch.object(server.time, "sleep", side_effect=waits.append), mock.patch.object(server.threading, "Thread", Now):
            self.assertTrue(self.client.post("/api/roku/launch?target=living", json={"id": "12"}).get_json()["ok"])
        self.assertEqual([c for c in calls if c[0] in ("key", "/launch/12")], [("key", "PowerOn"), ("/launch/12", "POST")])
        self.assertEqual(waits, [6])

    def test_icons_come_through_for_installed_apps_only(self):
        with mock.patch.object(roku, "_call", return_value=APPS_XML), mock.patch.object(roku, "icon", return_value=(PNG, "image/png")) as icon:
            r = self.client.get("/api/roku/icon?target=living&id=837")
            self.assertEqual((r.status_code, r.data, r.mimetype), (200, PNG, "image/png"))
            self.client.get("/api/roku/icon?target=living&id=837")
            self.assertEqual(icon.call_count, 1)  # fetched once, then kept
            self.assertEqual((r.headers["X-Content-Type-Options"], "sandbox" in r.headers["Content-Security-Policy"]), ("nosniff", True))
            self.assertEqual(self.client.get("/api/roku/icon?target=living&id=555").status_code, 404)
            self.assertEqual(self.client.get("/api/roku/icon?target=living&id=a/b").status_code, 404)

    def test_an_icon_that_is_not_a_known_raster_picture_is_never_passed_on(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        for body in (svg, b"<html>hi</html>", b""):
            server._icons.clear()
            with mock.patch.object(roku, "_call", return_value=APPS_XML), mock.patch.object(roku, "icon", return_value=(body, "image/svg+xml")):
                self.assertEqual(self.client.get("/api/roku/icon?target=living&id=837").status_code, 404)
        webp = b"RIFF" + bytes(4) + b"WEBPxx"
        for body, kind in ((bytes([255, 216, 255]) + b"jfif", "image/jpeg"), (b"GIF89a....", "image/gif"), (webp, "image/webp")):
            self.assertEqual(server.raster_type(body), kind)

    def test_a_tv_with_no_roku_has_no_apps(self):
        self.assertEqual(self.client.get("/api/roku/apps?target=bedroom").status_code, 404)

    def test_guests_may_open_apps_like_they_may_press_remote_keys(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        url = "/api/roku/launch?target=living"
        with mock.patch.dict(os.environ, {"TVREMOTE_DATA": d}), mock.patch.dict(sessions.key_cache, {"v": None}),                 mock.patch.object(accounts, "initialised", lambda: True), mock.patch.object(server, "session_user", _REAL_SESSION_USER):
            tok = server.new_session(accounts.GUEST, 3600, {"label": "g"})
            g = server.app.test_client()
            g.set_cookie("tv_auth", tok)
            self.assertNotEqual(g.post(url, json={"id": "1"}).status_code, 403)
            self.assertNotEqual(g.get("/api/roku/apps?target=living").status_code, 403)


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
        self.assertIsNone(ext({"power": "PowerOn", "app": "Roku Dynamic Menu", "app_id": "562859"}))  # the Roku's own menu (seen on the real bedroom TV)
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

    def test_a_pause_button_only_for_an_app_whose_player_is_really_open(self):
        for state, controllable in (("play", True), ("pause", True), ("buffer", True), ("close", False), ("stop", False), (None, False)):
            media = {"state": state, "duration": None} if state else {}
            with self.details(media=media):
                got = self.client.get("/api/status").get_json()["external"]
            self.assertEqual(got["controllable"], controllable, state)  # an app that is only browsing has nothing to pause

    def test_the_roku_menu_is_not_reported_as_something_playing(self):
        with self.details(app="Roku Dynamic Menu", app_id="562859", media={"state": "close", "duration": None}):
            self.assertIsNone(self.client.get("/api/status").get_json()["external"])
            self.assertIsNone(server.make_card("living", {"running": False})["external"])

    def test_nothing_external_when_this_remote_is_playing_or_the_tv_is_on_the_pi(self):
        with self.details():
            with mock.patch.object(server, "pi_status", return_value={"running": True, "title": "x"}):
                self.assertIsNone(self.client.get("/api/status").get_json().get("external"))
        with self.details(app_id="tvinput.hdmi1", app="Pi"):
            self.assertIsNone(self.client.get("/api/status").get_json()["external"])


if __name__ == "__main__":
    unittest.main()
