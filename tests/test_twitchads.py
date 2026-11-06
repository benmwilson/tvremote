import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
HAVE = bool(importlib.util.find_spec("streamlink") and importlib.util.find_spec("yt_dlp"))  # they live in the Pi's container, not on every dev machine


def seg(n, title="live", sec=0):
    return f"#EXT-X-PROGRAM-DATE-TIME:2026-11-06T12:00:{sec:02d}.000Z\n#EXTINF:2.000,{title}\nhttps://cdn.example/{n}.ts\n"


def playlist(first_seq, segs):
    return ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:%d\n" % first_seq + "".join(segs)).encode()


@unittest.skipUnless(HAVE, "streamlink is not installed here (it is in the container)")
class TwitchAdsTest(unittest.TestCase):
    def test_ads_are_dropped_and_the_jump_is_marked(self):
        import twitchads
        replies = [playlist(1, [seg("a1", sec=0), seg("a2", sec=2), seg("ad1", "Amazon|1", 4), seg("ad2", "Amazon|1", 6)]),
                   playlist(3, [seg("ad1", "Amazon|1", 4), seg("ad2", "Amazon|1", 6), seg("b1", sec=8), seg("b2", sec=10)]),
                   playlist(3, [seg("ad2", "Amazon|1", 6), seg("b1", sec=8), seg("b2", sec=10), seg("b3", sec=12)])]
        r = twitchads.Relay("https://usher.example/p.m3u8", lambda u: replies.pop(0))
        first = r.playlist()
        self.assertIn("a1.ts", first) and self.assertIn("a2.ts", first)
        self.assertNotIn("ad1", first)
        second = r.playlist()
        self.assertNotIn("ad", second.replace("EXT-X-DISCONTINUITY", "").replace("#EXTINF", "").replace("MEDIA-SEQUENCE", ""))  # none of the ad segments
        lines = second.splitlines()
        self.assertEqual(lines[lines.index("#EXT-X-DISCONTINUITY") + 2], "https://cdn.example/b1.ts")  # the picture jumps right before the first one after the ad
        third = r.playlist()
        self.assertEqual(third.count("b1.ts"), 1)  # a segment seen again is not added twice
        self.assertIn("b3.ts", third)
        seqs = [int(x.split(":")[1]) for x in third.splitlines() if x.startswith("#EXT-X-MEDIA-SEQUENCE")]
        self.assertEqual(seqs, [0])  # numbered by us, from 0, so removing ads never confuses the player

    def test_expired_address_is_asked_for_again_once(self):
        import twitchads
        calls = []

        def fetch(u):
            calls.append(u)
            if "old" in u:
                raise OSError("403")
            return playlist(1, [seg("a1")])
        r = twitchads.Relay("https://x/old.m3u8", fetch, resolve=lambda: "https://x/new.m3u8")
        self.assertIn("a1.ts", r.playlist())
        self.assertEqual(calls, ["https://x/old.m3u8", "https://x/new.m3u8"])

    def test_sessions_are_capped(self):
        import twitchads
        tokens = [twitchads.create("u", lambda u: b"") for _ in range(twitchads.MAX_SESSIONS + 3)]
        self.assertIsNone(twitchads.get(tokens[0]))
        self.assertIsNotNone(twitchads.get(tokens[-1]))


if __name__ == "__main__":
    unittest.main()
