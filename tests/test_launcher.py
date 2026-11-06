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

    def test_groups_are_their_own_row_of_group_tiles(self):
        rows = launcher.build_rows([], [], [{"id": "g1", "name": "omie, xqc", "members": []}])
        self.assertEqual(rows[0]["title"], "Groups")
        self.assertEqual(rows[0]["tiles"][0]["kind"], "group")

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
