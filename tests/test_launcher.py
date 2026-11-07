import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import launcher  # noqa: E402


def item(name, live=False, url=None, source="Kick"):
    return {"name": name, "url": url or f"https://kick.com/{name}", "live": live, "source": source}


class RowsTests(unittest.TestCase):
    def test_live_streamers_come_first_once_and_are_not_repeated_below(self):
        favs = [item("omie", True), item("capped")]
        recents = [item("omie", True), item("xqc", True), item("old")]
        rows = launcher.build_rows(favs, recents, [])
        self.assertEqual([r["title"] for r in rows], ["Live now", "Favourites", "Recent"])
        self.assertEqual([t["name"] for t in rows[0]["tiles"]], ["omie", "xqc"])
        self.assertEqual([t["name"] for t in rows[1]["tiles"]], ["capped"])
        self.assertEqual([t["name"] for t in rows[2]["tiles"]], ["old"])

    def test_one_streamer_is_one_tile_even_when_the_saved_name_has_a_title_in_it(self):
        favs = [{"name": "xqc: LIVE HERE", "url": "https://kick.com/xqc", "live": True, "source": "Kick"}]
        recents = [{"name": "xqc", "url": "https://kick.com/xqc", "live": True, "source": "Kick"},
                   {"name": "xqc", "url": "https://www.twitch.tv/xqc", "live": True, "source": "Twitch"}]
        rows = launcher.build_rows(favs, recents, [])
        self.assertEqual([t["name"] for r in rows for t in r["tiles"]], ["xqc: LIVE HERE"])

    def test_groups_are_their_own_row_of_group_tiles(self):
        rows = launcher.build_rows([], [], [{"id": "g1", "name": "omie, xqc", "members": []}])
        self.assertEqual(rows[0]["title"], "Groups")
        self.assertEqual(rows[0]["tiles"][0]["kind"], "group")

    def test_tiles_carry_the_picture_and_viewer_facts(self):
        x = {**item("omie", True), "thumb": "http://t/1.jpg", "viewers": 42, "avatar": "http://a/1.jpg", "title": "hi", "game": "Chess"}
        t = launcher.build_rows([x], [], [])[0]["tiles"][0]
        self.assertEqual((t["thumb"], t["viewers"], t["avatar"], t["title"], t["game"]), ("http://t/1.jpg", 42, "http://a/1.jpg", "hi", "Chess"))

    def test_empty_rows_are_left_out(self):
        self.assertEqual(launcher.build_rows([], [], []), [])


class MoveTests(unittest.TestCase):
    def setUp(self):
        self.pad = launcher.Launcher(launcher.build_rows(
            [item("a", True), item("b", True), item("c", True), item("d"), item("e")], [item("r1"), item("r2")], []))

    def test_sideways_moves_stop_at_the_ends(self):
        self.assertIsNone(self.pad.key("left"))
        self.assertEqual(self.pad.key("right"), {"redraw": True})
        self.assertEqual((self.pad.row, self.pad.col), (0, 1))
        for _ in range(5):
            self.pad.key("right")
        self.assertEqual(self.pad.col, 2)  # three live tiles: stops on the last

    def test_down_keeps_the_column_as_far_as_the_next_row_allows(self):
        self.pad.key("right"); self.pad.key("right")
        self.pad.key("down")
        self.assertEqual((self.pad.row, self.pad.col), (1, 1))  # the favourites row has two tiles
        self.pad.key("down")
        self.assertEqual((self.pad.row, self.pad.col), (2, 1))

    def test_going_back_up_returns_to_where_you_were(self):
        self.pad.key("right"); self.pad.key("right")
        self.pad.key("down"); self.pad.key("up")
        self.assertEqual((self.pad.row, self.pad.col), (0, 2))

    def test_select_plays_the_focused_stream_and_a_group_is_a_group(self):
        self.pad.key("right")
        self.assertEqual(self.pad.key("select"), {"play": "https://kick.com/b"})
        g = launcher.Launcher(launcher.build_rows([], [], [{"id": "g1", "name": "x", "members": []}]))
        self.assertEqual(g.key("select"), {"group": "g1"})

    def test_keys_arrive_under_many_names(self):
        self.assertEqual(self.pad.key("KEY_RIGHT"), {"redraw": True})
        self.assertEqual(self.pad.key("Down"), {"redraw": True})
        self.assertEqual(self.pad.key("KEY_BACK"), {"back": True})
        self.assertIsNone(self.pad.key("KEY_F13"))  # a key the launcher has no use for

    def test_home_goes_to_the_first_tile(self):
        self.pad.key("right"); self.pad.key("down")
        self.assertEqual(self.pad.key("home"), {"redraw": True})
        self.assertEqual((self.pad.row, self.pad.col), (0, 0))
        self.assertEqual(self.pad.key("home"), {"home": True})  # already there

    def test_new_contents_keep_the_focus_on_the_same_tile(self):
        self.pad.key("right")
        rows = launcher.build_rows([item("z", True), item("a", True), item("b", True)], [], [])
        self.pad.set_rows(rows)
        self.assertEqual(self.pad.focused()["name"], "b")

    def test_an_empty_launcher_ignores_moves_but_still_says_back(self):
        pad = launcher.Launcher([])
        self.assertIsNone(pad.key("down"))
        self.assertEqual(pad.key("back"), {"back": True})
        self.assertIsNone(pad.focused())


if __name__ == "__main__":
    unittest.main()


class ScreenTests(unittest.TestCase):
    def test_the_focused_tile_gets_a_ring_and_every_name_is_drawn(self):
        import shell
        rows = launcher.build_rows([item("omie", True), item("capped")], [item("old")], [])
        pieces = shell.launcher_pieces(rows, 0, 0)
        text = "\n".join(pieces)
        for name in ("omie", "capped", "old", "Live now", "Favourites", "Recent", "LIVE"):
            self.assertIn(name, text)
        self.assertEqual(sum(1 for p in pieces if r"\bord7" in p), 1)  # one ring, on the focused tile

    def test_the_window_scrolls_to_keep_the_focus_in_view(self):
        import shell
        rows = launcher.build_rows([item(f"s{i}", True) for i in range(9)], [], [])
        text = "\n".join(shell.launcher_pieces(rows, 0, 8))
        self.assertIn("s8", text)
        self.assertNotIn("s0", text)  # four across: the first ones have scrolled off to the left

    def test_an_empty_launcher_says_so(self):
        import shell
        self.assertIn("Nothing here yet", "\n".join(shell.launcher_pieces([], 0, 0)))

    def test_nothing_is_drawn_outside_the_screen(self):
        import shell
        rows = launcher.build_rows([item(f"s{i}", True) for i in range(6)], [item(f"r{i}") for i in range(6)], [{"id": "g", "name": "g", "members": []}])
        seen = []
        for ri in range(len(rows)):
            for p in shell.launcher_pieces(rows, ri, 5):
                k = p.find(chr(92) + "pos(")
                if k >= 0:
                    x, y = (int(v) for v in p[k + 5:p.index(")", k)].split(","))
                    seen.append(1)
                    self.assertLessEqual(x, 1920)
                    self.assertLessEqual(y, 1080)
        self.assertTrue(seen)  # the check really looked at positions


class DataEndpointTests(unittest.TestCase):
    def test_the_launcher_data_is_refused_while_the_launcher_is_off(self):
        from unittest import mock
        import app as server
        with mock.patch.object(server, "launcher_on", return_value=False):
            r = server.app.test_client().get("/api/launcher")
        self.assertIn(r.status_code, (401, 403, 404))


class KeyEndpointTests(unittest.TestCase):
    def test_keys_only_work_while_the_launcher_is_on_the_tv(self):
        from unittest import mock
        import app as server
        with mock.patch.object(server, "launcher_on", return_value=False):
            r = server.app.test_client().post("/api/launcher/key", json={"key": "down"})
        self.assertIn(r.status_code, (401, 403, 409))

    def test_a_select_starts_the_focused_stream(self):
        from unittest import mock
        import app as server
        pad = launcher.Launcher(launcher.build_rows([item("omie", True)], [], []))
        fake_home = mock.Mock(kind="idle")
        with mock.patch.object(server, "launcher_on", return_value=True), mock.patch.object(server, "LAUNCHER", pad), \
                mock.patch.object(server, "launcher_state", return_value=None), mock.patch.object(server, "HOME", fake_home), \
                mock.patch.object(server, "tgt", return_value=None), mock.patch.object(server, "play_async") as play, \
                mock.patch.object(server, "guest_link_refused", return_value=None):
            with server.app.test_request_context("/api/launcher/key", method="POST", json={"key": "select"}):
                server.g.user = "ben"
                r = server.api_launcher_key()
        self.assertTrue(r.get_json()["ok"])
        play.assert_called_once_with(None, "https://kick.com/omie", None)

    def test_a_select_on_a_card_that_went_offline_says_so_and_starts_nothing(self):
        from unittest import mock
        import app as server
        pad = launcher.Launcher(launcher.build_rows([item("capped", True)], [], []))
        fake_live = mock.Mock()
        fake_live.snapshot.return_value = {"https://kick.com/capped": {"live": False}}
        with mock.patch.object(server, "launcher_on", return_value=True), mock.patch.object(server, "LAUNCHER", pad),                 mock.patch.object(server, "launcher_state", return_value=None), mock.patch.object(server, "HOME", mock.Mock(kind="idle")),                 mock.patch.object(server, "tgt", return_value=None), mock.patch.object(server, "play_async") as play,                 mock.patch.object(server, "livewatch", fake_live), mock.patch.object(server, "guest_link_refused", return_value=None):
            with server.app.test_request_context("/api/launcher/key", method="POST", json={"key": "select"}):
                server.g.user = "ben"
                r = server.api_launcher_key()
        fake_live.refresh_one.assert_called_once_with("https://kick.com/capped")
        play.assert_not_called()
        self.assertIn("offline", server._launch_note["text"])
