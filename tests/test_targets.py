import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import targets  # noqa: E402


class Health(unittest.TestCase):
    def test_health_rules(self):
        h = targets.health
        self.assertEqual(h("pi", {"error": "boom", "buffering": True, "running": True})["state"], "error")
        self.assertEqual(h("pi", {"starting": True})["state"], "starting")
        self.assertEqual(h("pi", {"running": True, "buffering": True})["state"], "buffering")
        self.assertEqual(h("pi", {"running": True})["state"], "smooth")
        self.assertEqual(h("pi", {})["state"], "idle")

    def test_health_carries_drops_for_pi_only_and_start_time(self):
        self.assertEqual(targets.health("pi", {"running": True, "drops": 3})["drops"], 3)
        self.assertIsNone(targets.health("chromecast", {"running": True, "drops": 3})["drops"])
        self.assertEqual(targets.health("chromecast", {"running": True, "start_s": 9.5})["start_s"], 9.5)


class CardCache(unittest.TestCase):
    def test_snapshot_does_not_wait_for_slow_provider(self):
        c = targets.Cards({"a": lambda: {"name": "A"}, "b": lambda: time.sleep(5) or {"name": "B"}},
                          order=["a", "b"], timeout=0.2)
        t0 = time.time()
        c.refresh_now()
        snap = c.snapshot()
        self.assertLess(time.time() - t0, 1.0)
        self.assertEqual([s["id"] for s in snap], ["a", "b"])
        self.assertEqual((snap[0]["name"], snap[0]["reachable"]), ("A", True))
        self.assertFalse(snap[1]["reachable"])

    def test_unreachable_keeps_last_good_fields(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) > 1:
                raise OSError("tv off")
            return {"name": "B", "power": "on"}
        c = targets.Cards({"b": flaky}, order=["b"], timeout=0.5)
        c.refresh_now()
        c.refresh_now()
        card = c.snapshot()[0]
        self.assertEqual((card["name"], card["power"]), ("B", "on"))
        self.assertFalse(card["reachable"])

    def test_late_result_is_kept_for_the_next_refresh(self):
        c = targets.Cards({"b": lambda: time.sleep(0.3) or {"name": "B"}}, order=["b"], timeout=0.1)
        c.refresh_now()                     # misses its deadline
        self.assertFalse(c.snapshot()[0]["reachable"])
        time.sleep(0.4)                     # the provider finishes in the background
        c.refresh_now()                     # its result is adopted, and being a little slow no longer counts as unreachable
        card = c.snapshot()[0]
        self.assertEqual(card.get("name"), "B")
        self.assertTrue(card["reachable"])

    def test_stuck_provider_is_not_stacked(self):
        calls = []
        c = targets.Cards({"b": lambda: calls.append(1) or time.sleep(1)}, order=["b"], timeout=0.1)
        for _ in range(3):
            c.refresh_now()
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
