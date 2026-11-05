import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import multi  # noqa: E402


def wall(**kw):
    return multi.Wall(lambda url, q, sock, extra: (["sleep", "30"], q), lambda url: ("Kick", "streamlink"), lambda url: url, **kw)


class LayoutTest(unittest.TestCase):
    def test_every_tile_fits_the_screen_with_room_for_its_name_strip_and_none_overlap(self):
        for layout in multi.LAYOUTS:
            for n in range(2, multi.MAX_TILES + 1):
                rects = multi.tiles(layout, n)
                self.assertEqual(len(rects), n)
                for x, y, w, h in rects:
                    self.assertGreaterEqual(x, multi.M)
                    self.assertGreaterEqual(y, multi.M)
                    self.assertLessEqual(x + w, multi.W - multi.M)
                    self.assertLessEqual(y + h + multi.S, multi.H - multi.M)
                for a in range(n):
                    for b in range(a + 1, n):
                        (x1, y1, w1, h1), (x2, y2, w2, h2) = rects[a], rects[b]
                        apart = x1 + w1 <= x2 or x2 + w2 <= x1 or y1 + h1 + multi.S <= y2 or y2 + h2 + multi.S <= y1
                        self.assertTrue(apart, (layout, n, a, b))

    def test_four_in_a_grid_is_the_size_that_was_tested_on_the_pi(self):
        self.assertEqual(multi.tiles("grid", 4), [(112, 24, 840, 472), (968, 24, 840, 472), (112, 548, 840, 472), (968, 548, 840, 472)])

    def test_the_big_focus_tile_gets_a_720_stream_and_small_ones_480(self):
        self.assertTrue(multi.cap("streamlink", multi.tiles("focus", 3)[0][2]).startswith("720p60"))
        self.assertTrue(multi.cap("streamlink", multi.tiles("grid", 4)[0][2]).startswith("480p"))
        self.assertEqual((multi.cap("ytdlp", 1198), multi.cap("ytdlp", 840)), ("720", "480"))

    def test_there_is_a_desktop_rule_for_every_window_name(self):
        xml = multi.rules_xml()
        for layout in multi.LAYOUTS:
            for n in range(2, multi.MAX_TILES + 1):
                for i in range(n):
                    self.assertIn(f'identifier="{multi.window_id(layout, n, i)}"', xml)


class WallTest(unittest.TestCase):
    def test_add_remove_and_the_limits(self):
        w = wall()
        self.assertFalse(w.active())
        self.assertTrue(w.add("https://kick.com/b", "https://kick.com/a"))
        self.assertTrue(w.active() and w.layout == "grid" and w.count() == 2)
        self.assertFalse(w.add("https://kick.com/b", "https://kick.com/a"))  # already there
        self.assertFalse(w.add("https://kick.com/a", "https://kick.com/a"))  # the main stream
        self.assertTrue(w.add("https://kick.com/c", "https://kick.com/a"))
        self.assertTrue(w.add("https://kick.com/d", "https://kick.com/a"))
        self.assertFalse(w.add("https://kick.com/e", "https://kick.com/a"))  # full
        w.remove(2)
        self.assertEqual([t["url"] for t in w.extras], ["https://kick.com/b", "https://kick.com/d"])
        w.remove(1); w.remove(1)
        self.assertFalse(w.active())
        self.assertIsNone(w.layout)

    def test_the_main_player_gets_its_window_only_while_the_wall_is_up(self):
        w = wall()
        self.assertEqual(w.main_args(), ([], None))
        w.add("https://kick.com/b", "https://kick.com/a")
        args, width = w.main_args()
        self.assertIn("--wayland-app-id=tvr-grid2-1", args)
        self.assertEqual(width, 928)

    def test_swap_puts_the_other_stream_on_the_big_tile(self):
        w = wall()
        w.add("https://kick.com/b", "https://kick.com/a")
        self.assertEqual(w.swap(1, "https://kick.com/a"), "https://kick.com/b")
        self.assertEqual(w.extras[0]["url"], "https://kick.com/a")
        self.assertIsNone(w.swap(5, "https://kick.com/a"))

    def test_audio_mutes_everything_else(self):
        w = wall()
        w.add("https://kick.com/b", "https://kick.com/a")
        with mock.patch.object(multi.mpvipc, "send") as send:
            w.set_audio(1, "/tmp/main.sock")
        send.assert_any_call("/tmp/main.sock", ["set_property", "mute", True])
        send.assert_any_call(multi._sock(1), ["set_property", "mute", False])
        self.assertEqual(w.audio, 1)

    def test_all_windows_start_together_by_default(self):
        self.assertEqual(multi.STAGGER, 0)


if __name__ == "__main__":
    unittest.main()


class WallScreenTest(unittest.TestCase):
    def test_background_has_a_name_strip_per_stream_and_no_big_logo(self):
        import shell
        ev = shell.wall_screen("grid", [("alpha", "Kick"), ("beta", "Twitch"), ("gamma", "Other")])
        self.assertEqual(len(ev), 5)  # two glows and three names
        self.assertTrue(all(f"{n}" in "".join(ev) for n in ("alpha", "beta", "gamma")))
        self.assertNotIn("fscx260", shell.ass_file([(0, 1, "x")], logo=False))  # no big logo behind the windows
        self.assertIn("fscx260", shell.ass_file([(0, 1, "x")]))
