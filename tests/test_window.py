import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import app as server  # noqa: E402


class FullWindow(unittest.TestCase):
    def setUp(self):
        server._fs["at"] = 0.0

    def test_a_small_window_gets_fullscreen_asked_for_again(self):
        calls = []
        with mock.patch.object(server, "prop", return_value={"w": 1280, "h": 720}), mock.patch.object(server, "mpv", side_effect=lambda c, *a: calls.append(c)), \
                mock.patch.object(server.time, "sleep"):
            self.assertTrue(server.ensure_full_window(100.0))
        self.assertEqual(calls, [["set_property", "fullscreen", False], ["set_property", "fullscreen", True]])

    def test_a_right_sized_window_or_a_silent_player_is_left_alone(self):
        for answer in ({"w": 1920, "h": 1080}, None, {}):
            server._fs["at"] = 0.0
            with mock.patch.object(server, "prop", return_value=answer), mock.patch.object(server, "mpv") as m:
                self.assertFalse(server.ensure_full_window(100.0))
            m.assert_not_called()

    def test_it_checks_at_most_every_five_seconds(self):
        with mock.patch.object(server, "prop", return_value={"w": 1280, "h": 720}) as p, mock.patch.object(server, "mpv"), mock.patch.object(server.time, "sleep"):
            server.ensure_full_window(100.0)
            self.assertFalse(server.ensure_full_window(102.0))
        self.assertEqual(p.call_count, 1)


class HeatWarning(unittest.TestCase):
    def setUp(self):
        server._warned["at"] = 0.0

    def test_off_by_default_and_only_when_warm_and_a_picture_is_playing(self):
        with mock.patch.object(server, "mpv") as m, mock.patch.object(server, "prop", return_value=123):
            with mock.patch.dict(server.settings, {"heat_tv": False}):
                self.assertFalse(server.heat_warn(85.0, 100.0))
            with mock.patch.dict(server.settings, {"heat_tv": True}):
                self.assertFalse(server.heat_warn(70.0, 100.0))  # cool
                self.assertFalse(server.heat_warn(None, 100.0))  # unknown
                self.assertTrue(server.heat_warn(80.0, 100.0))
        m.assert_called_once()

    def test_at_most_once_a_minute_and_never_without_a_player(self):
        with mock.patch.dict(server.settings, {"heat_tv": True}), mock.patch.object(server, "mpv") as m:
            with mock.patch.object(server, "prop", return_value=None):
                self.assertFalse(server.heat_warn(85.0, 100.0))  # no player: nothing to put text on
            with mock.patch.object(server, "prop", return_value=123):
                self.assertTrue(server.heat_warn(85.0, 200.0))
                self.assertFalse(server.heat_warn(85.0, 230.0))
                self.assertTrue(server.heat_warn(85.0, 262.0))
        self.assertEqual(m.call_count, 2)


if __name__ == "__main__":
    unittest.main()
