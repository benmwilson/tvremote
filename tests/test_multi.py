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

    def test_a_picked_quality_changes_the_stream_and_goes_with_it_on_a_swap(self):
        self.assertEqual(multi.quality_for("streamlink", "480"), "480p,360p,160p,worst")
        self.assertEqual(multi.quality_for("ytdlp", "720"), "720")
        self.assertEqual((multi.quality_for("ytdlp", "1080"), multi.quality_for("streamlink", "1080")[:11]), ("1080", "1080p60,108"))
        self.assertIsNone(multi.quality_for("streamlink", None))
        self.assertIsNone(multi.quality_for("streamlink", "1080; rm -rf"))
        w = wall()
        w.add("https://kick.com/b", "https://kick.com/a")
        w.extras[0]["quality"], w.main_quality = "720", "360"
        w.swap(1, "https://kick.com/a")
        self.assertEqual((w.extras[0]["quality"], w.main_quality), ("360", "720"))
        self.assertEqual(w.status()["tiles"][0]["quality"], "360")

    def test_the_sound_follows_its_stream_through_a_swap_and_never_plays_from_two_windows(self):
        w = wall()
        w.add("https://kick.com/b", "https://kick.com/a")
        w.audio = 1  # the sound was on the small tile (stream b)
        self.assertIn("--mute=yes", w.main_args()[0])  # a restarted main window must not add its own sound
        w.swap(1, "https://kick.com/a")  # b becomes the main stream
        self.assertEqual(w.audio, 0)  # the sound stays with b
        self.assertNotIn("--mute=yes", w.main_args()[0])
        w.swap(1, "https://kick.com/b")  # and back: the sound goes with b to the small tile
        self.assertEqual(w.audio, 1)
        w.audio = 2
        w.add("https://kick.com/c", "https://kick.com/b")
        self.assertEqual(w.swap(1, "https://kick.com/b") and w.audio, 2)  # a third tile keeps it

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


class OnlyThisTest(unittest.TestCase):
    def test_keeping_only_an_added_stream_ends_the_wall_and_starts_that_stream_alone(self):
        import app as server
        server.wall.extras = [{"url": "https://kick.com/b", "proc": None}]
        server.wall.layout = "grid"
        calls = []
        try:
            with mock.patch.object(server, "wall_later", lambda fn, *a: calls.append((fn, a))),                     mock.patch.dict(server.state, {"url": "https://kick.com/a"}), mock.patch.object(server, "tgt", return_value=None):
                with server.app.test_request_context("/api/wall", method="POST", json={"action": "only", "i": 1}):
                    r = server.api_wall()  # called directly: the login guard is not what is being tested
        finally:
            server.wall.clear()
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(calls, [(server.wall_relayout, ("https://kick.com/b",))])
        self.assertFalse(server.wall.active())


class StaleWallTest(unittest.TestCase):
    def test_turning_the_tv_off_and_starting_something_new_does_not_bring_the_old_wall_back(self):
        import app as server
        server.wall.extras = [{"url": "https://kick.com/b", "proc": None}]
        server.wall.layout = "grid"
        try:
            with mock.patch.object(server, "pi_playing", return_value=False):
                server.on_tv_off("living")
            self.assertFalse(server.wall.active())
            self.assertIsNone(server.wall.layout)
            server.wall.extras, server.wall.layout = [{"url": "https://kick.com/b", "proc": None}], "focus"
            with mock.patch.object(server, "remember"), mock.patch.object(server, "resolve", return_value=("Kick", "streamlink")),                     mock.patch.object(server, "start_player"), mock.patch.object(server, "switch_input"):
                server.play_on(None, "https://kick.com/omie", None)
            self.assertFalse(server.wall.active())  # a fresh play is one stream
        finally:
            server.wall.clear()


if __name__ == "__main__":
    unittest.main()
