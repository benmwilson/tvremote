import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import pihealth  # noqa: E402


class PiHealthTests(unittest.TestCase):
    def test_levels(self):
        self.assertEqual([pihealth.level(t) for t in (None, 60, 78, 81.9, 82)], ["unknown", "ok", "warm", "warm", "hot"])

    def test_snapshot_never_raises(self):
        s = pihealth.snapshot()
        self.assertIn("level", s)
        self.assertIsInstance(s["players"], list)


class HeatGuardTests(unittest.TestCase):
    def test_hot_lowers_and_cooling_restores(self):
        import app as a  # noqa: E402
        w = a.wall
        for p in (mock.patch.object(a, "wall_later"), mock.patch.object(a, "mpv"), mock.patch.object(w, "active", return_value=True),
                  mock.patch.object(w, "layout", "grid"), mock.patch.object(w, "extras", [{"url": "u", "quality": "1080"}]),
                  mock.patch.object(w, "main_quality", "1080")):
            p.start()
            self.addCleanup(p.stop)
        a.heat.update(on=False, saved=None, restored=False)
        self.addCleanup(a.heat.update, on=False, saved=None, restored=False)
        a.heat_check(83)
        self.assertEqual((w.main_quality, w.extras[0]["quality"]), ("480", "480"))
        a.heat_check(80)  # not cooled enough yet
        self.assertEqual(w.main_quality, "480")
        a.heat_check(70)
        self.assertEqual((w.main_quality, w.extras[0]["quality"]), ("1080", "1080"))
        a.heat_check(83)  # a second overheat: stays lowered, picks are not remembered again
        a.heat_check(70)
        self.assertEqual(w.main_quality, "480")


if __name__ == "__main__":
    unittest.main()


class WifiTests(unittest.TestCase):
    SAMPLE = ("Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
              " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
              " wlan0: 0000   70.  -55.  -256        0      0      0      0      0        0\n").replace("\\n", "\n")

    def test_the_signal_is_read_from_the_wireless_file(self):
        with mock.patch.object(pihealth, "_read", return_value=self.SAMPLE):
            self.assertEqual(pihealth.wifi_dbm(), -55)

    def test_a_wired_pi_has_no_signal(self):
        with mock.patch.object(pihealth, "_read", return_value=None):
            self.assertIsNone(pihealth.wifi_dbm())
