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


if __name__ == "__main__":
    unittest.main()
