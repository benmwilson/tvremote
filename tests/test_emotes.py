import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import emotes  # noqa: E402

REPLIES = {
    "7tv.io/v3/users/twitch/1": {"emote_set": {"emotes": [{"name": "GAMBA", "data": {"animated": True, "host": {"url": "//cdn.7tv.app/emote/AAA/"}}}]}},
    "7tv.io/v3/emote-sets/global": {"emotes": [{"name": "RainTime", "data": {"host": {"url": "//cdn.7tv.app/emote/BBB"}}}]},
    "betterttv.net/3/cached/users/twitch/1": {"channelEmotes": [{"id": "c1", "code": "Chan", "imageType": "png"}], "sharedEmotes": [{"id": "s1", "code": "Shared", "imageType": "gif"}]},
    "betterttv.net/3/cached/emotes/global": [{"id": "g1", "code": "OMEGALUL", "imageType": "png"}],
    "frankerfacez.com/v1/room/id/1": {"room": {"set": 7}, "sets": {"7": {"emoticons": [{"name": "WideHard", "urls": {"1": "//cdn.frankerfacez.com/emote/9/1", "2": "//cdn.frankerfacez.com/emote/9/2"}}]}}},
    "frankerfacez.com/v1/set/global": {"default_sets": [3], "sets": {"3": {"emoticons": [{"name": "ZrehplaR", "urls": {"1": "//cdn.frankerfacez.com/emote/1/1"}}]}}},
}


def fake_get(url, timeout=8):
    for k, v in REPLIES.items():
        if k in url:
            return v
    raise OSError("no such url " + url)


class EmotesTest(unittest.TestCase):
    def setUp(self):
        emotes._cache.clear()
        self.addCleanup(emotes._cache.clear)

    def test_every_provider_channel_then_global(self):
        helix = lambda path, params=(), **kw: {"data": [{"id": "t1", "name": "xqcP"}]}
        with mock.patch.object(emotes, "_get", fake_get), mock.patch.object(emotes.twitch, "enabled", lambda: True), \
                mock.patch.object(emotes.twitch, "users", lambda l: {"xqc": {"id": "1"}}), mock.patch.object(emotes.twitch, "_helix", helix):
            sets = emotes.for_channel("xqc")
        by = {(s["provider"], s["scope"]): s["emotes"] for s in sets}
        self.assertEqual(sets[0]["scope"], "channel")  # the channel's own come before the global ones
        self.assertEqual(by[("7tv", "channel")][0], {"n": "GAMBA", "u": "https://cdn.7tv.app/emote/AAA/2x.webp", "a": True})
        self.assertEqual(by[("7tv", "global")][0]["u"], "https://cdn.7tv.app/emote/BBB/2x.webp")
        self.assertEqual([e["n"] for e in by[("bttv", "channel")]], ["Chan", "Shared"])
        self.assertTrue(by[("bttv", "channel")][1]["a"] and not by[("bttv", "channel")][0]["a"])
        self.assertEqual(by[("ffz", "channel")][0]["u"], "https://cdn.frankerfacez.com/emote/9/2")
        self.assertEqual(by[("ffz", "global")][0]["u"], "https://cdn.frankerfacez.com/emote/1/1")  # falls back to the size that exists
        self.assertIn("jtvnw.net/emoticons/v2/t1/", by[("twitch", "channel")][0]["u"])

    def test_without_twitch_credentials_only_the_global_third_party_sets(self):
        with mock.patch.object(emotes, "_get", fake_get), mock.patch.object(emotes.twitch, "enabled", lambda: False):
            sets = emotes.for_channel("xqc")
        self.assertEqual({(s["provider"], s["scope"]) for s in sets}, {("7tv", "global"), ("bttv", "global"), ("ffz", "global")})

    def test_a_dead_provider_is_left_out_and_remembered_briefly(self):
        calls = []

        def flaky(url, timeout=8):
            calls.append(url)
            if "7tv.io" in url:
                raise OSError("down")
            return fake_get(url)
        with mock.patch.object(emotes, "_get", flaky), mock.patch.object(emotes.twitch, "enabled", lambda: False):
            first = emotes.for_channel("xqc")
            n = len(calls)
            emotes.for_channel("xqc")
        self.assertNotIn("7tv", {s["provider"] for s in first})
        self.assertEqual(len(calls), n)  # the second time nothing was fetched again

    def test_big_sets_are_capped(self):
        big = {"emotes": [{"name": f"e{i}", "data": {"host": {"url": "//c/x"}}} for i in range(emotes.MAX_PER_SET + 50)]}
        with mock.patch.object(emotes, "_get", lambda url, timeout=8: big):
            self.assertEqual(len(emotes._set("7tv", "global", None)), emotes.MAX_PER_SET)


if __name__ == "__main__":
    unittest.main()
