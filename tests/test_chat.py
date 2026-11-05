import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import chat  # noqa: E402
import app as server  # noqa: E402
import accounts  # noqa: E402


def setUpModule():
    """These tests exercise the chat feed, not login: the app counts as set up and every request is the signed-in owner stand-in."""
    global _patches
    _patches = [mock.patch.object(accounts, "initialised", lambda: True), mock.patch.object(server, "session_user", lambda tok: accounts.OWNER)]
    for p in _patches:
        p.start()


def tearDownModule():
    for p in _patches:
        p.stop()

LINE = "@color=#FF8000;display-name=Foo;emotes=25:0-4,12-16/1902:6-10 :foo!foo@foo.tmi.twitch.tv PRIVMSG #xqc :Kappa hi Keepo Kappa"


class Parse(unittest.TestCase):
    def test_emote_positions_are_read_from_the_tags(self):
        m = chat.parse_full(LINE)
        self.assertEqual((m["name"], m["color"]), ("Foo", "#FF8000"))
        self.assertEqual([(e["id"], e["s"], e["e"]) for e in m["emotes"]], [("25", 0, 4), ("1902", 6, 10), ("25", 12, 16)])

    def test_bad_emote_tags_are_dropped_not_trusted(self):
        m = chat.parse_full("@emotes=<b>:0-4/25:a-b/26:1-2 :a!a@a PRIVMSG #c :hello")
        self.assertEqual([e["id"] for e in m["emotes"]], ["26"])

    def test_the_tv_overlay_parser_is_unchanged(self):
        self.assertEqual(chat.parse(LINE)[0:2], ("Foo", "#FF8000"))
        self.assertIsNone(chat.parse(":tmi.twitch.tv 001 justinfan1 :Welcome"))


class Feed(unittest.TestCase):
    def test_messages_get_ids_and_since_returns_only_new_ones(self):
        f = chat.ChatFeed("xqc", autostart=False)
        for _ in range(3):
            f.on_line(LINE)
        self.assertEqual([m["id"] for m in f.since(0)], [1, 2, 3])
        self.assertEqual([m["id"] for m in f.since(2)], [3])

    def test_it_keeps_only_the_latest_messages(self):
        f = chat.ChatFeed("xqc", autostart=False)
        for _ in range(chat.ChatFeed.KEEP + 20):
            f.on_line(LINE)
        self.assertEqual(len(f.msgs), chat.ChatFeed.KEEP)
        self.assertEqual(f.n, chat.ChatFeed.KEEP + 20)


class Endpoint(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        server._feeds.clear()

    def test_nothing_playing_or_not_twitch_gives_an_empty_answer(self):
        with mock.patch.dict(server.state, {"url": "https://kick.com/xqc", "source": "Kick"}), mock.patch.object(server, "pi_playing", return_value=True):
            r = self.client.get("/api/chat/feed").get_json()
        self.assertEqual((r["ok"], r["msgs"]), (False, []))

    def test_a_twitch_channel_starts_one_feed_and_a_stale_since_resets(self):
        made = []

        class F(chat.ChatFeed):
            def __init__(s, ch, autostart=True):
                super().__init__(ch, autostart=False)
                made.append(ch)
        with mock.patch.dict(server.state, {"url": "https://www.twitch.tv/xqc", "source": "Twitch"}), mock.patch.object(server, "pi_playing", return_value=True), \
                mock.patch.object(server.chat, "ChatFeed", F):
            r = self.client.get("/api/chat/feed").get_json()
            server._feeds["xqc"].on_line(LINE)
            r2 = self.client.get("/api/chat/feed?since=0").get_json()
            r3 = self.client.get("/api/chat/feed?since=999").get_json()  # the phone is ahead of a restarted feed
        self.assertTrue(r["ok"])
        self.assertEqual(r["channel"], "xqc")
        self.assertEqual(len(r2["msgs"]), 1)
        self.assertTrue(r3["reset"])
        self.assertEqual(made, ["xqc"])  # one feed, reused

    def test_each_stream_on_the_multi_view_screen_has_its_own_feed(self):
        made = []

        class F(chat.ChatFeed):
            def __init__(s, ch, autostart=True):
                super().__init__(ch, autostart=False)
                made.append(ch)
        server.wall.extras = [{"url": "https://www.twitch.tv/other", "proc": None}]
        try:
            with mock.patch.dict(server.state, {"url": "https://www.twitch.tv/xqc", "source": "Twitch"}), mock.patch.object(server, "pi_playing", return_value=True),                     mock.patch.object(server.chat, "ChatFeed", F):
                a = self.client.get("/api/chat/feed?i=0").get_json()
                b = self.client.get("/api/chat/feed?i=1").get_json()
                none = self.client.get("/api/chat/feed?i=2").get_json()
        finally:
            server.wall.extras = []
        self.assertEqual((a["channel"], b["channel"], none["ok"]), ("xqc", "other", False))
        self.assertEqual(made, ["xqc", "other"])


class Detect(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        server._detected.clear()

    def ask(self, kick, twitch, name="omie"):
        answers = {"https://kick.com/" + name: kick, "https://www.twitch.tv/" + name: twitch}
        with mock.patch.object(server, "check_live", lambda url: answers[url]):
            return self.client.get("/api/detect?name=" + name).get_json()

    def test_live_on_one_site_picks_it(self):
        r = self.ask({"live": False}, {"live": True, "title": "hi"})
        self.assertEqual(r["pick"], "https://www.twitch.tv/omie")

    def test_known_on_only_one_site_picks_it_even_when_offline(self):
        self.assertEqual(self.ask(None, {"live": False})["pick"], "https://www.twitch.tv/omie")

    def test_live_on_both_or_offline_on_both_asks(self):
        self.assertIsNone(self.ask({"live": True}, {"live": True}, "a1")["pick"])
        self.assertIsNone(self.ask({"live": False}, {"live": False}, "a2")["pick"])

    def test_unknown_everywhere_and_bad_names(self):
        self.assertEqual(self.ask(None, None, "nobody")["matches"], [])
        self.assertEqual(self.client.get("/api/detect?name=a%20b;rm").status_code, 400)


if __name__ == "__main__":
    unittest.main()
