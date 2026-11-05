import sys
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


if __name__ == "__main__":
    unittest.main()
