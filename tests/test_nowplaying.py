import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import nowplaying  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_the_newest_now_playing_wins(self):
        t = '12\n[[5,["nowPlaying",{"videoId":"aaaaaaaaaaa","currentTime":"1"}]],[6,["onStateChange",{"state":"1"}]],[7,["nowPlaying",{"videoId":"bbbbbbbbbbb"}]]]'
        self.assertEqual(nowplaying.parse_events(t)["videoId"], "bbbbbbbbbbb")

    def test_nothing_useful_gives_none(self):
        self.assertIsNone(nowplaying.parse_events('[[1,["noop"]],[2,["nowPlaying",{}]]]'))
        self.assertIsNone(nowplaying.parse_events(""))

    def test_a_bad_video_id_is_never_looked_up(self):
        with mock.patch.object(nowplaying.urllib.request, "urlopen") as op:
            self.assertIsNone(nowplaying.video_details("../x"))
            op.assert_not_called()


class SyncTests(unittest.TestCase):
    def tearDown(self):
        for st in list(nowplaying._state.values()):
            st["stop"].set()
        nowplaying._state.clear(); nowplaying._info.clear()

    def test_only_youtube_starts_a_listener_and_leaving_it_stops_it(self):
        with mock.patch.object(nowplaying.threading, "Thread") as th:
            nowplaying.sync("tv1", "1.2.3.4", "Netflix")
            th.assert_not_called()
            nowplaying.sync("tv1", "1.2.3.4", "YouTube")
            nowplaying.sync("tv1", "1.2.3.4", "YouTube")  # a second look keeps the same listener
            self.assertEqual(th.call_count, 1)
            st = nowplaying._state["tv1"]
            nowplaying._info["tv1"] = {"app": "YouTube", "title": "t"}
            self.assertEqual(nowplaying.get("tv1", "YouTube")["title"], "t")
            nowplaying.sync("tv1", "1.2.3.4", None)
            self.assertTrue(st["stop"].is_set())
            self.assertIsNone(nowplaying.get("tv1", "YouTube"))

    def test_details_of_another_app_are_not_shown(self):
        nowplaying._info["tv1"] = {"app": "YouTube", "title": "t"}
        self.assertIsNone(nowplaying.get("tv1", "Netflix"))


if __name__ == "__main__":
    unittest.main()


class ViewTests(unittest.TestCase):
    def test_only_admins_see_what_is_being_watched(self):
        import app as server
        ext = {"app": "YouTube", "paused": False, "title": "t", "channel": "c", "thumb": "u", "video": "v"}
        with server.app.test_request_context("/"):
            server.g.user = "someone"
            with mock.patch.object(server.accounts, "role", return_value="user"):
                self.assertEqual(server.own_app_view(ext), {"app": "YouTube", "paused": False})
            with mock.patch.object(server.accounts, "role", return_value="admin"):
                self.assertEqual(server.own_app_view(ext)["title"], "t")
            self.assertIsNone(server.own_app_view(None))
