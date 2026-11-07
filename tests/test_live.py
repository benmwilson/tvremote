import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import live  # noqa: E402

URL = "https://kick.com/chan"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(results, urls=(URL,), busy=False, path=None, clock=None):
    clock = clock or Clock()
    calls = []

    def check(url):
        calls.append(url)
        return results.pop(0) if results else None
    path = path or os.path.join(tempfile.mkdtemp(), "live.json")
    w = live.LiveWatch(path, urls=lambda: list(urls), check=check, busy=lambda: busy, clock=clock)
    return w, calls, clock, path


class Recording(unittest.TestCase):
    def test_a_live_channel_is_recorded_with_game_and_uptime(self):
        w, calls, clock, _ = make([{"live": True, "game": "GTA V", "title": "RP", "uptime_s": 3600}])
        w.refresh_now()
        e = w.snapshot()[URL]
        self.assertEqual((e["live"], e["game"], e["title"], e["uptime_s"]), (True, "GTA V", "RP", 3600))
        self.assertEqual((e["checked"], e["last_live"]), (clock.t, clock.t))

    def test_going_offline_keeps_when_and_what_they_last_played(self):
        w, calls, clock, _ = make([{"live": True, "game": "GTA V", "uptime_s": 60}, {"live": False}])
        w.refresh_now()
        seen = clock.t
        clock.t += 7200
        w.refresh_now()
        e = w.snapshot()[URL]
        self.assertFalse(e["live"])
        self.assertEqual((e["last_live"], e["game"]), (seen, "GTA V"))
        self.assertIsNone(w.live_for(URL))

    def test_an_unknown_result_keeps_the_history(self):
        w, calls, clock, _ = make([{"live": True, "game": "GTA V", "uptime_s": 60}, None])
        w.refresh_now()
        seen = clock.t
        clock.t += 600
        w.refresh_now()
        e = w.snapshot()[URL]
        self.assertIsNone(e["live"])
        self.assertEqual(e["last_live"], seen)

    def test_live_for_keeps_growing_between_checks(self):
        w, calls, clock, _ = make([{"live": True, "uptime_s": 3600}])
        w.refresh_now()
        clock.t += 90
        self.assertEqual(w.live_for(URL), 3690)

    def test_history_survives_a_restart(self):
        w, calls, clock, path = make([{"live": True, "game": "GTA V", "uptime_s": 60}])
        w.refresh_now()
        again, _, _, _ = make([], path=path, clock=clock)
        self.assertEqual(again.snapshot()[URL]["game"], "GTA V")

    def test_channels_that_are_no_longer_wanted_are_dropped(self):
        w, calls, clock, path = make([{"live": True}, {"live": True}], urls=(URL, "https://kick.com/gone"))
        w.refresh_now()
        w.urls = lambda: [URL]
        w.refresh_now()
        self.assertNotIn("https://kick.com/gone", w.snapshot())


class Politeness(unittest.TestCase):
    def test_a_busy_pi_is_left_alone(self):
        w, calls, clock, _ = make([{"live": True}], busy=True)
        w.refresh_now()
        self.assertEqual(calls, [])

    def test_one_channel_can_be_checked_even_when_busy(self):
        w, calls, clock, _ = make([{"live": True}], busy=True)
        w.refresh_one(URL)
        self.assertEqual(calls, [URL])

    def test_poke_only_refreshes_stale_data(self):
        w, calls, clock, _ = make([{"live": True}, {"live": True}])
        t = w.poke()
        t.join(2)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(w.poke())          # fresh: nothing to do
        clock.t += w.stale + 1
        t = w.poke()
        t.join(2)
        self.assertEqual(len(calls), 2)

    def test_poke_can_ask_for_fresher_data(self):
        w, calls, clock, _ = make([{"live": True}])
        w.poke().join(2)
        clock.t += 120
        self.assertIsNone(w.poke())              # 120 s old is fresh enough by the default (180 s)
        w.poke(stale=100).join(2)                # the launcher asks for 100 s
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
