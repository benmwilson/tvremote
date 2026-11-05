import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import app as server  # noqa: E402


ROKU = {"name": "TV One", "brand": "TCL", "model": "55C803", "size": 55, "power": "PowerOn", "app": "Roku"}
PLAYING = {"running": True, "title": "chan: a very good stream", "source": "Kick", "live": True, "paused": False,
           "position": 42.0, "quality": "1080p60,best", "drops": 1, "url": "https://kick.com/chan"}


def fresh_cards(**providers):
    from targets import Cards
    server.cards = Cards(providers or {"living": server.living_card, "bedroom": server.bedroom_card},
                         order=["living", "bedroom"], timeout=0.3)
    return server.cards


class Targets(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        server.ROKU_IPS.update(living="203.0.113.1", bedroom="203.0.113.2")
        self.patches = [mock.patch.object(server, "pi_status", return_value=dict(PLAYING)),
                        mock.patch.object(server.roku, "info", return_value=dict(ROKU)),
                        mock.patch.object(server, "CAST", None)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        server._roku.clear()
        server._roku_busy.clear()
        server.fetch_roku("living")  # warm the cache the way the background refresh would
        server.fetch_roku("bedroom")

    def test_targets_returns_both_cards_in_order(self):
        fresh_cards().refresh_now()
        cards = self.client.get("/api/targets").get_json()
        self.assertEqual([c["id"] for c in cards], ["living", "bedroom"])
        self.assertEqual(cards[0]["name"], "Living room")
        self.assertEqual(cards[1]["name"], "Bedroom")
        self.assertFalse(cards[1]["reachable"])  # no Chromecast in this test, but the card still exists

    def test_busy_tv_card_shows_now_playing(self):
        fresh_cards().refresh_now()
        living = self.client.get("/api/targets").get_json()[0]
        self.assertTrue(living["reachable"])
        self.assertEqual((living["brand"], living["size"], living["model"]), ("TCL", 55, "55C803"))
        self.assertEqual(living["now"]["title"], "chan: a very good stream")
        self.assertEqual((living["now"]["source"], living["now"]["live"], living["now"]["stream"]), ("Kick", True, "1080p60"))
        self.assertEqual(living["health"]["state"], "smooth")

    def test_starting_tv_card_is_starting_not_idle(self):
        server.pi_status.return_value = {"running": False, "starting": True, "url": "https://kick.com/chan", "source": "Kick"}
        fresh_cards().refresh_now()
        living = self.client.get("/api/targets").get_json()[0]
        self.assertTrue(living["now"]["starting"])
        self.assertEqual(living["health"]["state"], "starting")

    def test_idle_tv_card_has_no_now(self):
        server.pi_status.return_value = {"running": False}
        fresh_cards().refresh_now()
        self.assertIsNone(self.client.get("/api/targets").get_json()[0]["now"])

    def test_failed_playback_shows_its_error_on_the_card(self):
        server.pi_status.return_value = {"running": False, "error": "HTTP error 404 Not Found", "url": "https://kick.com/chan", "source": "Kick"}
        fresh_cards().refresh_now()
        living = self.client.get("/api/targets").get_json()[0]
        self.assertEqual(living["error"], "HTTP error 404 Not Found")
        self.assertIsNone(living["now"])

    def test_slow_roku_does_not_make_the_card_unreachable(self):
        getattr(server, "_roku", {}).clear()
        server.roku.info.side_effect = lambda ip, **kw: time.sleep(5) or dict(ROKU)
        fresh_cards().refresh_now()
        living = self.client.get("/api/targets").get_json()[0]
        self.assertTrue(living["reachable"])  # the Pi player answered; only the TV's own details are late
        self.assertEqual(living["now"]["title"], "chan: a very good stream")

    def test_roku_is_not_polled_on_every_refresh(self):
        server._roku.clear()
        server._roku_busy.clear()
        server.roku.info.reset_mock()  # setUp warmed the cache with the same mock
        cards = fresh_cards()
        for _ in range(5):
            cards.refresh_now()
        time.sleep(0.3)
        self.assertLessEqual(server.roku.info.call_count, 2)  # at most one fetch per TV, not one per refresh

    def test_targets_never_waits_on_a_slow_tv(self):
        fresh_cards(living=server.living_card, bedroom=lambda: time.sleep(5) or {}).refresh_now()
        t0 = time.time()
        cards = self.client.get("/api/targets").get_json()
        self.assertLess(time.time() - t0, 1.0)
        self.assertTrue(cards[0]["reachable"])
        self.assertFalse(cards[1]["reachable"])


class Routes(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_play_redirect_opens_the_pis_remote(self):
        with mock.patch.object(server, "start_player"):
            r = self.client.get("/play?u=https%3A%2F%2Fkick.com%2Fchan")
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["Location"].endswith("/?target=living"), r.headers["Location"])

    def test_unknown_target_is_refused_not_sent_to_the_pi(self):
        with mock.patch.object(server, "stop_player") as sp:
            r = self.client.post("/api/cmd?target=foo", json={"action": "stop"})
        self.assertEqual(r.status_code, 400)
        sp.assert_not_called()
        self.assertEqual(self.client.get("/api/status?target=foo").status_code, 400)

    def test_share_serves_the_page(self):
        r = self.client.get("/share?u=https%3A%2F%2Fkick.com%2Fx")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"<title>", r.data)

    def test_play_to_missing_chromecast_does_not_use_the_pi(self):
        with mock.patch.object(server, "CAST", None), mock.patch.object(server, "start_player") as sp:
            r = self.client.post("/api/play?target=bedroom", json={"url": "https://kick.com/chan"})
        self.assertEqual(r.status_code, 503)
        self.assertFalse(r.get_json()["ok"])
        self.assertIn("Chromecast", r.get_json()["error"])  # readable, not "503 Service Unavailable: ..."
        sp.assert_not_called()


class PausedOverlay(unittest.TestCase):
    """Pausing leaves a 'Paused' overlay on the TV until you resume. It uses overlay id 2; the chat overlay uses id 1."""

    def setUp(self):
        self.client = server.app.test_client()
        self.sent = []

    def pause(self, now_paused):
        def fake_mpv(cmd, timeout=1.0):
            self.sent.append(cmd)
            return {"error": "success"}
        props = {"pause": now_paused, "media-title": "chan: a stream"}
        with mock.patch.object(server, "mpv", fake_mpv), mock.patch.object(server, "prop", lambda n: props.get(n)):
            return self.client.post("/api/cmd", json={"action": "pause"})

    def test_pausing_draws_a_persistent_overlay(self):
        self.assertEqual(self.pause(True).status_code, 200)
        drawn = [c for c in self.sent if c[0] == "osd-overlay"]
        self.assertEqual(len(drawn), 1)
        self.assertEqual(drawn[0][1:3], [2, "ass-events"])
        self.assertIn("Paused", drawn[0][3])
        self.assertIn("chan: a stream", drawn[0][3])

    def test_resuming_clears_the_overlay(self):
        self.pause(False)
        self.assertIn(["osd-overlay", 2, "none"], self.sent)

    def test_the_overlay_text_cannot_inject_ass_tags(self):
        props = {"pause": True, "media-title": r"bad {\p1}title \Nmore"}
        with mock.patch.object(server, "mpv", lambda c, timeout=1.0: self.sent.append(c) or {"error": "success"}), \
                mock.patch.object(server, "prop", lambda n: props.get(n)):
            self.client.post("/api/cmd", json={"action": "pause"})
        text = [c for c in self.sent if c[0] == "osd-overlay"][0][3]
        self.assertNotIn(r"{\p1}", text.split("title")[0].split("bad")[-1])  # the title's own braces and backslashes are stripped
        self.assertIn("bad p1title Nmore", text)


class FakeHome:
    def __init__(self):
        self.shown, self.started, self.stopped, self.vo, self.error, self._running = [], 0, 0, "wlshm", None, True

    def show(self, kind, detail=""):
        self.shown.append((kind, detail))
        return True

    def running(self):
        return self._running

    def start(self):
        self.started += 1
        self._running = True
        return True

    def stop(self):
        self.stopped += 1
        self._running = False


class ShellFlow(unittest.TestCase):
    """The TV's home screen follows what the player does."""

    def setUp(self):
        self.client = server.app.test_client()
        self.home = FakeHome()
        tmp = tempfile.mkdtemp()
        for p in (mock.patch.object(server, "HOME", self.home),
                  mock.patch.object(server, "SETTINGS_FILE", os.path.join(tmp, "settings.json")),
                  mock.patch.object(server, "LOG_FILE", os.path.join(tmp, "player.log")),
                  mock.patch.dict(server.settings, {"shell": True}),
                  mock.patch.dict(server.state, {"proc": None, "url": "https://kick.com/chan"})):
            p.start()
            self.addCleanup(p.stop)

    def watch(self, returncode, error, replaced=False):
        proc = mock.Mock()
        proc.poll.return_value = returncode
        proc.returncode = returncode
        server.state["proc"] = mock.Mock() if replaced else proc
        with mock.patch.object(server, "last_error", return_value=error):
            server._watch(proc)

    def test_a_new_stream_shows_loading_before_the_old_one_stops(self):
        order = []
        self.home.show = lambda kind, detail="": order.append("screen:" + kind) or True
        with mock.patch.object(server, "stop_player", side_effect=lambda: order.append("stop")),                 mock.patch.object(server.subprocess, "Popen"), mock.patch.object(server, "_watch"),                 mock.patch.object(server, "chat_sync"), mock.patch.object(server, "switch_tv_to_pi"):
            server.start_player("https://kick.com/chan")
        self.assertEqual(order[:2], ["screen:loading", "stop"])

    def test_stopping_shows_the_idle_screen(self):
        with mock.patch.object(server, "stop_player"):
            self.client.post("/api/cmd", json={"action": "stop"})
        self.assertEqual(self.home.shown[-1], ("idle", ""))

    def test_a_player_that_dies_shows_why(self):
        self.watch(1, "HTTP error 404 Not Found")
        self.assertEqual(self.home.shown[-1], ("error", "That link wasn't found (404)"))

    def test_a_channel_that_is_not_live_says_so(self):
        server.state["url"] = "https://kick.com/capped"
        self.watch(1, "error: No playable streams found on this URL: https://kick.com/capped")
        self.assertEqual(self.home.shown[-1], ("offline", "capped"))

    def test_a_stream_that_finishes_shows_ended(self):
        self.watch(0, None)
        self.assertEqual(self.home.shown[-1], ("ended", "chan"))

    def test_a_replaced_player_shows_nothing(self):
        self.watch(1, "boom", replaced=True)
        self.assertEqual(self.home.shown, [])

    def test_playing_hides_the_loading_screen(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        server.state["proc"] = mock.Mock()  # replaced as soon as it ends, so only "playing" is shown
        with mock.patch.object(server, "prop", return_value=1234):
            server._watch(proc)
        self.assertEqual(self.home.shown, [("playing", "")])

    def test_error_text_is_friendly(self):
        d = server.describe_error
        self.assertEqual(d("error: No playable streams found on this URL: x"), "Isn't live right now")
        self.assertEqual(d("HTTP error 404 Not Found"), "That link wasn't found (404)")
        self.assertEqual(d("The web client only works when logged-in"), "That needs a login, which isn't supported")
        self.assertEqual(d("something odd"), "something odd")
        self.assertIsNone(d(None))

    def test_the_switch_persists_and_starts_or_stops_the_screen(self):
        r = self.client.post("/api/shell", json={"enabled": False})
        self.assertEqual((r.status_code, r.get_json()["enabled"]), (200, False))
        self.assertEqual(self.home.stopped, 1)
        self.assertFalse(server.load_settings()["shell"])
        r = self.client.post("/api/shell", json={"enabled": True})
        self.assertTrue(r.get_json()["enabled"])
        self.assertEqual(self.home.started, 1)
        self.assertTrue(self.client.get("/api/shell").get_json()["running"])

    def test_with_the_screen_off_nothing_is_drawn(self):
        server.settings["shell"] = False
        server.screen("loading", "x")
        self.assertEqual(self.home.shown, [])


class InputSwitch(unittest.TestCase):
    """Playing must not re-select an input the TV is already showing (it relaunches the input and flashes the screen)."""

    def setUp(self):
        for p in (mock.patch.dict(server.ROKU_IPS, {"living": "203.0.113.1"}), mock.patch.dict(server.INPUTS, {"living": "HDMI1"}),
                  mock.patch.object(server.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def run_switch(self, power, app_id):
        with mock.patch.object(server.roku, "info", return_value={"power": power, "app_id": app_id}),                 mock.patch.object(server.roku, "key") as key:
            server.switch_input("living")
        return [c.args[1] for c in key.call_args_list]

    def test_no_switch_when_the_tv_is_already_on_the_input(self):
        self.assertEqual(self.run_switch("PowerOn", "tvinput.hdmi1"), [])

    def test_switches_when_the_tv_is_on_another_input(self):
        self.assertEqual(self.run_switch("PowerOn", "tvinput.hdmi2"), ["InputHDMI1"])

    def test_wakes_a_tv_that_is_off_then_switches(self):
        self.assertEqual(self.run_switch("Ready", "tvinput.hdmi1"), ["PowerOn", "InputHDMI1"])

    def test_cec_is_skipped_when_the_tv_is_already_on_the_input(self):
        with mock.patch.object(server.roku, "info", return_value={"power": "PowerOn", "app_id": "tvinput.hdmi1"}),                 mock.patch.object(server, "cec") as cec:
            server.switch_tv_to_pi()
        cec.assert_not_called()

    def test_cec_is_sent_when_the_tv_is_on_something_else(self):
        with mock.patch.object(server.roku, "info", return_value={"power": "PowerOn", "app_id": "tvinput.hdmi3"}),                 mock.patch.object(server, "cec") as cec:
            server.switch_tv_to_pi()
        self.assertTrue(cec.called)


if __name__ == "__main__":
    unittest.main()
