import sys
import unittest
from datetime import datetime, timezone
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

    def test_sound_is_switched_per_stream_and_only_the_main_one_has_it_to_start_with(self):
        w = wall()
        w.add("https://kick.com/b", "https://kick.com/a")
        self.assertEqual(w.sound, {0})  # not on by default
        with mock.patch.object(multi.mpvipc, "send") as send:
            w.set_sound(1, True, "/tmp/main.sock")  # more than one can be heard at once
            send.assert_called_once_with(multi._sock(1), ["set_property", "mute", False])
            self.assertEqual(w.sound, {0, 1})
            w.set_sound(0, False, "/tmp/main.sock")
        send.assert_called_with("/tmp/main.sock", ["set_property", "mute", True])
        self.assertEqual(w.sound, {1})

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
        w.sound = {1}  # the sound was on the small tile (stream b), not on the main one
        self.assertIn("--mute=yes", w.main_args()[0])  # a restarted main window must not add its own sound
        w.swap(1, "https://kick.com/a")  # b becomes the main stream
        self.assertEqual(w.sound, {0})  # the sound stays with b
        self.assertNotIn("--mute=yes", w.main_args()[0])
        w.swap(1, "https://kick.com/b")  # and back: the sound goes with b to the small tile
        self.assertEqual(w.sound, {1})
        w.sound = {0, 1}
        w.swap(1, "https://kick.com/b")  # both are on: both stay on
        self.assertEqual(w.sound, {0, 1})
        w.add("https://kick.com/c", "https://kick.com/b")
        w.sound = {2}
        w.remove(1)  # the tile after the removed one moves up, and keeps its sound
        self.assertEqual(w.sound, {1})

    def test_all_windows_start_together_by_default(self):
        self.assertEqual(multi.STAGGER, 0)


if __name__ == "__main__":
    unittest.main()


class WallScreenTest(unittest.TestCase):
    def test_background_has_a_name_strip_per_stream_and_no_big_logo(self):
        import shell
        ev = shell.wall_screen("grid", [("alpha", "Kick", False), ("beta", "Twitch", False), ("gamma", "Other", False)])
        self.assertEqual(len(ev), 2 + 3 * 3)  # two glows, and for each stream a panel, its words and its name
        self.assertTrue(all(f"{n}" in "".join(ev) for n in ("alpha", "beta", "gamma")))
        self.assertNotIn("fscx260", shell.ass_file([(0, 1, "x")], logo=False))  # no big logo behind the windows
        self.assertIn("fscx260", shell.ass_file([(0, 1, "x")]))


class AddIsRecentTest(unittest.TestCase):
    def test_a_stream_added_to_the_screen_is_remembered(self):
        import app as server
        try:
            with mock.patch.object(server, "remember") as remember, mock.patch.object(server, "wall_later"), mock.patch.object(server, "tgt", return_value=None),                     mock.patch.object(server, "pi_status", return_value={"running": True}), mock.patch.dict(server.state, {"url": "https://kick.com/a"}):
                with server.app.test_request_context("/api/wall", method="POST", json={"action": "add", "url": "https://kick.com/b"}):
                    r = server.api_wall()
        finally:
            server.wall.clear()
        self.assertTrue(r.get_json()["ok"])
        remember.assert_called_once_with("https://kick.com/b")


class QuickSecondAddTest(unittest.TestCase):
    def test_a_second_stream_added_while_the_first_one_is_still_loading_is_not_refused(self):
        import app as server
        def add(url):
            with server.app.test_request_context("/api/wall", method="POST", json={"action": "add", "url": url}):
                return server.api_wall()
        try:
            with mock.patch.object(server, "remember"), mock.patch.object(server, "wall_later"), mock.patch.object(server, "tgt", return_value=None),                     mock.patch.dict(server.state, {"url": "https://kick.com/a"}):
                with mock.patch.object(server, "pi_status", return_value={"running": True}):
                    first = add("https://kick.com/b")
                # the first add restarts the main window: for a few seconds the player says it is not running
                with mock.patch.object(server, "pi_status", return_value={"running": False}):
                    second = add("https://kick.com/c")
                    refused = add("https://kick.com/b")  # a stream already on the screen is still refused
                urls = [t["url"] for t in server.wall.extras]
        finally:
            server.wall.clear()
        self.assertTrue(first.get_json()["ok"])
        self.assertTrue(second.get_json()["ok"])
        self.assertEqual(urls, ["https://kick.com/b", "https://kick.com/c"])
        self.assertEqual(refused[1], 400)

    def test_nothing_is_added_when_nothing_is_playing_and_there_is_no_wall(self):
        import app as server
        try:
            with mock.patch.object(server, "tgt", return_value=None), mock.patch.object(server, "pi_status", return_value={"running": False}),                     mock.patch.dict(server.state, {"url": None}):
                with server.app.test_request_context("/api/wall", method="POST", json={"action": "add", "url": "https://kick.com/b"}):
                    r = server.api_wall()
        finally:
            server.wall.clear()
        self.assertEqual(r[1], 400)


class QualityOsdTest(unittest.TestCase):
    def test_a_wheel_and_text_show_while_the_quality_changes_then_the_result(self):
        import app as server
        sent, polls = [], [0]
        def ready():
            polls[0] += 1
            return polls[0] >= 2  # the new picture is playing after a moment
        with mock.patch.object(server.mpvipc, "send", side_effect=lambda sock, cmd, *a: sent.append(cmd) or {"error": "success"}), \
                mock.patch.object(server.time, "sleep"), mock.patch.object(server.time, "time", side_effect=[0.0, 0.1] + [1.0] * 40):
            server.osd_swap("720p", ready=ready).join(5)
        texts = [c[1] for c in sent if c[0] == "show-text"]
        self.assertTrue(texts[0].endswith("Switching to 720p"))
        self.assertEqual(texts[-1], "Quality: 720p")
        self.assertGreaterEqual(len({t[0] for t in texts[:-1]}), 1)  # a wheel frame in front of the words

    def test_it_stops_quietly_when_there_is_no_window_to_draw_on(self):
        import app as server
        with mock.patch.object(server.mpvipc, "send", return_value=None) as send:
            server.osd_swap("720p").join(5)
        self.assertEqual(send.call_count, 1)  # tried once, then gave up: no "Quality" message to nowhere


class WarmAddressesTest(unittest.TestCase):
    def test_the_full_screen_address_of_every_stream_is_ready_before_the_wall_shrinks(self):
        import app as server
        asked = []
        server.wall.extras = [{"url": "https://kick.com/b", "proc": None}]
        server.wall.layout = "grid"
        server.wall.main_url = "https://kick.com/a"
        server.wall.saved_quality = None
        try:
            with mock.patch.object(server, "resolve", return_value=("Kick", "streamlink")), \
                    mock.patch.object(server, "stream_url", side_effect=lambda u, q, low=False: asked.append((u, q))):
                server.warm_addresses()
        finally:
            server.wall.clear()
        # what removing a stream loads: the one that is left, at its full quality (the single-stream default)
        self.assertIn(("https://kick.com/a", "1080p60,best"), asked)
        self.assertIn(("https://kick.com/b", "1080p60,best"), asked)


class PlayFromAWallTest(unittest.TestCase):
    """Starting something new while several streams are on the screen: the main window goes full screen and loads it in place."""

    def run_play(self, wall_up):
        import app as server
        server.wall.extras = [{"url": "https://kick.com/b", "proc": None}] if wall_up else []
        server.wall.layout = "grid" if wall_up else None
        server.wall.saved_quality = "720"
        try:
            with mock.patch.object(server, "remember"), mock.patch.object(server, "switch_input"), mock.patch.object(server, "resolve", return_value=("Kick", "streamlink")),                     mock.patch.object(server, "wall_relayout") as relayout, mock.patch.object(server, "start_player") as start, mock.patch.object(server.wall, "stop_windows"),                     mock.patch.dict(server.state, {"url": "https://kick.com/a", "started": 0}):
                server.play_on(None, "https://kick.com/new", None)
            return relayout, start, server.wall.saved_quality, server.wall.active()
        finally:
            server.wall.clear()

    def test_a_new_stream_out_of_a_multi_view_loads_in_place_at_its_own_quality(self):
        relayout, start, saved, active = self.run_play(True)
        relayout.assert_called_once_with("https://kick.com/new")
        start.assert_not_called()  # no new window
        self.assertEqual((saved, active), (None, False))

    def test_a_new_stream_on_a_single_stream_screen_is_unchanged(self):
        relayout, start, _, _ = self.run_play(False)
        start.assert_called_once_with("https://kick.com/new", None)
        relayout.assert_not_called()


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


class ReconcileTest(unittest.TestCase):
    """The wall tracks the main stream itself, so quick repeated swaps can never copy one stream into two tiles, and the windows catch up."""

    def setUp(self):
        import app as server
        self.server = server
        server.wall.clear()
        server.wall.layout = "focus"
        server.wall.main_url = "https://kick.com/a"
        live = mock.MagicMock()
        live.poll.return_value = None
        server.wall.extras = [{"url": "https://kick.com/b", "proc": live, "hot": True, "wid": "tvr-focus3-2", "started": "https://kick.com/b"},
                              {"url": "https://kick.com/c", "proc": live, "hot": True, "wid": "tvr-focus3-3", "started": "https://kick.com/c"}]
        self.state = mock.patch.dict(server.state, {"url": "https://kick.com/a", "proc": live, "hot": True, "wid": "tvr-focus3-1"})
        self.state.start()

    def tearDown(self):
        self.state.stop()
        self.server.wall.extras = []  # fake processes: nothing to kill
        self.server.wall.clear()

    def streams(self):
        w = self.server.wall
        return [w.main_url] + [t["url"] for t in w.extras]

    def test_quick_swaps_only_ever_permute_the_streams(self):
        w = self.server.wall
        for i in (1, 2, 1, 2, 2, 1):  # taps that land before the windows have caught up
            w.swap(i)
            self.assertCountEqual(self.streams(), ["https://kick.com/a", "https://kick.com/b", "https://kick.com/c"])

    def test_the_windows_are_told_to_load_the_new_streams_in_place_all_at_once(self):
        server, w = self.server, self.server.wall
        w.swap(1)
        w.swap(2)  # a second tap before anything has loaded
        sent = []
        with mock.patch.object(server, "stream_url", lambda url, q: "addr:" + url), mock.patch.object(server, "screen"),                 mock.patch.object(server.mpvipc, "send", lambda sock, cmd, timeout=1.0: sent.append((sock, cmd)) or {"error": "success"}):
            server.reconcile_job()
        loads = {s: c[1] for s, c in sent if c[0] == "loadfile"}
        self.assertEqual(len(loads), 3)  # every window changed stream, none restarted
        self.assertEqual(loads[server.SOCK], "addr:" + w.main_url)
        self.assertEqual(server.state["url"], w.main_url)
        self.assertTrue(all(t["started"] == t["url"] for t in w.extras))
        before = len(sent)
        with mock.patch.object(server, "stream_url", lambda url, q: "addr:" + url), mock.patch.object(server, "screen"),                 mock.patch.object(server.mpvipc, "send", lambda sock, cmd, timeout=1.0: sent.append((sock, cmd)) or {"error": "success"}):
            server.reconcile_job()  # a queued second run finds nothing left to do
        self.assertFalse([c for s, c in sent[before:] if c[0] == "loadfile"])


class TitlesTest(unittest.TestCase):
    def test_every_stream_on_the_screen_gets_a_title_and_a_lookup(self):
        import app as server
        server.wall.clear()
        server.wall.layout, server.wall.main_url = "grid", "https://kick.com/a"
        server.wall.extras = [{"url": "https://kick.com/b", "proc": None}, {"url": "https://www.twitch.tv/c", "proc": None}]
        facts = {"https://kick.com/a": {"title": "A's stream"}, "https://kick.com/b": {"title": "B's stream"}}
        warmed = []
        try:
            with mock.patch.object(server, "warm_title", warmed.append), mock.patch.object(server.livewatch, "snapshot", lambda: facts),                     mock.patch.dict(server.state, {"url": "https://kick.com/a", "source": "Kick"}):
                w = server.wall_status()
        finally:
            server.wall.clear()
        self.assertEqual([t["title"] for t in w["tiles"]], ["B's stream", None])  # the one nothing is known about stays empty until the lookup finishes
        self.assertEqual(w["main"]["title"], "A's stream")
        self.assertCountEqual(warmed, ["https://kick.com/a", "https://kick.com/b", "https://www.twitch.tv/c"])


class ControlledStreamTest(unittest.TestCase):
    """The volume slider and mute button belong to the stream the card controls, not to the first window."""

    def setUp(self):
        import app as server
        self.server = server
        server.wall.clear()
        server.wall.layout, server.wall.main_url = "grid", "https://kick.com/a"
        server.wall.extras = [{"url": "https://kick.com/b", "proc": None}]
        server.wall.selected = 1

    def tearDown(self):
        self.server.wall.clear()

    def post(self, body):
        with self.server.app.test_request_context("/api/cmd", method="POST", json=body):
            return self.server.api_cmd()

    def test_volume_goes_to_the_controlled_stream_only(self):
        sent = []
        with mock.patch.object(self.server, "tgt", return_value=None),                 mock.patch.object(self.server.mpvipc, "send", lambda sock, cmd, timeout=1.0: sent.append((sock, cmd)) or {"error": "success"}),                 mock.patch.object(self.server, "osd_volume"):
            self.post({"action": "volume", "value": 40})
        self.assertEqual([s for s, c in sent if c[0] == "set_property"], [multi._sock(1)])

    def test_the_volume_bar_is_drawn_in_the_controlled_streams_window(self):
        sent = []
        with mock.patch.object(self.server, "tgt", return_value=None),                 mock.patch.object(self.server.mpvipc, "send", lambda sock, cmd, timeout=1.0: sent.append((sock, cmd)) or {"error": "success", "data": 40}):
            self.post({"action": "volume", "value": 40})
        shown = [s for s, c in sent if c[0] == "show-text"]
        self.assertEqual(shown, [multi._sock(1)])  # not the first window's

    def test_mute_switches_the_controlled_streams_sound(self):
        with mock.patch.object(self.server, "tgt", return_value=None), mock.patch.object(multi.mpvipc, "send") as send:
            self.post({"action": "mute"})  # it had no sound: now it has
            self.assertEqual(self.server.wall.sound, {0, 1})
            self.post({"action": "mute"})
            self.assertEqual(self.server.wall.sound, {0})
        send.assert_any_call(multi._sock(1), ["set_property", "mute", False])


class RemoveFirstTest(unittest.TestCase):
    def test_removing_the_first_stream_promotes_the_next_and_keeps_sound_and_selection_with_their_streams(self):
        w = wall()
        w.add("https://kick.com/b", "https://kick.com/a")
        w.add("https://kick.com/c", "https://kick.com/a")
        w.sound, w.selected = {0, 2}, 2  # a and c have sound; c is controlled
        self.assertEqual(w.drop_main(), "https://kick.com/b")
        self.assertEqual([t["url"] for t in w.extras], ["https://kick.com/c"])
        self.assertEqual((w.sound, w.selected), ({1}, 1))  # c moved up one and kept both
        self.assertTrue(w.active())

    def test_the_last_one_left_is_alone_and_has_sound(self):
        w = wall()
        w.add("https://kick.com/b", "https://kick.com/a")
        w.sound = {0}  # only the one being removed had sound
        self.assertEqual(w.drop_main(), "https://kick.com/b")
        self.assertFalse(w.active())
        self.assertEqual(w.sound, {0})
        self.assertIsNone(w.drop_main())  # nothing left to promote


PLAYLIST = "#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:{pdt}\n#EXTINF:2.0,\nseg1.ts\n#EXTINF:2.0,\nseg2.ts\n"
BARE = b"#EXTM3U\n#EXTINF:2.0,\nseg.ts\n"


class ReplaceAndSyncTest(unittest.TestCase):
    def setUp(self):
        import app as server
        self.server = server
        server.wall.clear()
        server.wall.layout, server.wall.main_url = "grid", "https://kick.com/a"
        server.wall.extras = [{"url": "https://kick.com/b", "proc": None}]

    def tearDown(self):
        self.server.wall.clear()

    def test_replace_puts_a_new_stream_in_a_window_and_refuses_one_already_there(self):
        w = self.server.wall
        self.assertFalse(w.replace(1, "https://kick.com/a"))
        self.assertTrue(w.replace(1, "https://kick.com/c"))
        self.assertEqual(w.extras[0]["url"], "https://kick.com/c")
        self.assertTrue(w.replace(0, "https://kick.com/d"))
        self.assertEqual(w.main_url, "https://kick.com/d")
        self.assertFalse(w.replace(5, "https://kick.com/e"))

    def test_the_live_edge_comes_from_the_playlists_time_tags(self):
        edge = self.server.hls_edge(PLAYLIST.format(pdt="2026-01-01T00:00:00.000Z"))
        self.assertEqual(edge, datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() + 4.0)
        self.assertIsNone(self.server.hls_edge(BARE.decode()))  # no tags: no answer

    def test_sync_winds_back_only_the_leading_window_by_how_far_ahead_it_is(self):
        server = self.server
        pdt = {"https://kick.com/a": "2026-01-01T00:00:00.000Z", "https://kick.com/b": "2026-01-01T00:00:03.000Z"}  # b is 3 s ahead
        sent = []
        with mock.patch.object(server, "stream_url", lambda url, q: url), \
                mock.patch.object(server.hlsproxy, "_get", lambda url: PLAYLIST.format(pdt=pdt[url]).encode()), \
                mock.patch.object(server, "sprop", lambda sock, name: {"time-pos": 100.0, "demuxer-cache-time": 104.0}[name]), \
                mock.patch.object(server.mpvipc, "send", lambda sock, cmd, timeout=1.0: sent.append((sock, cmd)) or {"error": "success"}):
            r = server.wall_sync()
        self.assertEqual(sent, [(multi._sock(1), ["seek", -3.0, "relative+exact"])])
        self.assertEqual((r["aligned"], r["spread"]), (2, 3.0))
        self.assertIn("3.0 s apart", r["note"])

    def test_sync_says_so_when_the_streams_carry_no_timing(self):
        server = self.server
        with mock.patch.object(server, "stream_url", lambda url, q: url), \
                mock.patch.object(server.hlsproxy, "_get", lambda url: BARE), \
                mock.patch.object(server, "sprop", lambda sock, name: {"time-pos": 100.0, "demuxer-cache-time": 104.0}[name]), \
                mock.patch.object(server.mpvipc, "send", lambda sock, cmd, timeout=1.0: {"error": "success"}):
            r = server.wall_sync()
        self.assertEqual(r["aligned"], 0)
        self.assertIn("no timing", r["note"])


class HotLoadTest(unittest.TestCase):
    def setUp(self):
        import app as server
        self.server = server
        server.wall.clear()
        live = mock.MagicMock()
        live.poll.return_value = None
        self.state = mock.patch.dict(server.state, {"url": "https://kick.com/a", "proc": live, "hot": True, "wid": None})
        self.state.start()

    def tearDown(self):
        self.state.stop()

    def test_a_new_stream_loads_into_the_running_window(self):
        server, sent = self.server, []
        with mock.patch.object(server, "stream_url", lambda url, q: "addr:" + url), mock.patch.object(server, "is_channel", return_value=False), \
                mock.patch.object(server.threading, "Thread"), \
                mock.patch.object(server.mpvipc, "send", lambda sock, cmd, timeout=1.0: sent.append(cmd) or {"error": "success"}):
            done = server.hot_load("https://kick.com/b", None)
        self.assertTrue(done)
        self.assertEqual(sent, [["loadfile", "addr:https://kick.com/b", "replace"]])
        self.assertEqual(server.state["url"], "https://kick.com/b")

    def test_twitch_or_a_window_that_cannot_do_it_is_started_the_usual_way(self):
        server = self.server
        with mock.patch.object(server.mpvipc, "send") as send:
            self.assertFalse(server.hot_load("https://www.twitch.tv/x", None))  # Twitch's ad filter lives in streamlink's own pipe
            server.state["hot"] = False
            self.assertFalse(server.hot_load("https://kick.com/b", None))
        send.assert_not_called()


class AddressCacheTest(unittest.TestCase):
    def test_a_stream_address_is_remembered_so_a_swap_does_not_wait_for_streamlink(self):
        import app as server
        server._addr_cache.clear()
        ran = []

        def fake(cmd, **kw):
            ran.append(cmd)
            return mock.Mock(stdout="https://example.com/live.m3u8\n", stderr="", returncode=0)
        with mock.patch.object(server.subprocess, "run", fake):
            a = server.stream_url("https://kick.com/a", "480p,worst")
            b = server.stream_url("https://kick.com/a", "480p,worst")  # remembered
            server.stream_url("https://kick.com/a", "720p,worst")  # another quality is another address
        self.assertEqual((a, b), ("https://example.com/live.m3u8",) * 2)
        self.assertEqual(len(ran), 2)
        server._addr_cache.clear()


class OfflineTest(unittest.TestCase):
    def test_a_window_that_ends_says_why_in_plain_words_and_can_be_retried(self):
        self.assertEqual(multi.friendly("[info] Closing currently open stream..."), "Went offline")
        self.assertEqual(multi.friendly(None), "Went offline")
        self.assertEqual(multi.friendly("Failed to open something odd"), "Failed to open something odd")
        w = wall()
        w.add("https://kick.com/b", "https://kick.com/a")
        w.extras[0].update(error="Went offline", proc=object())
        w.retry(1)
        self.assertIsNone(w.extras[0]["error"])
        self.assertIsNone(w.extras[0]["proc"])

    def test_the_background_marks_an_ended_streams_slot(self):
        import shell
        ev = shell.wall_screen("grid", [("a", "Kick", False), ("b", "Kick", True)])
        self.assertEqual(len(ev), 2 + 2 * 3)
        self.assertIn("Loading", "".join(ev))  # the one that is still starting says so
        self.assertIn("Went offline", "".join(ev))


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
