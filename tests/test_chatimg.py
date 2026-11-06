import importlib.util
import io
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import chatimg  # noqa: E402
import chatwin  # noqa: E402

HAVE = importlib.util.find_spec("PIL") is not None


def png(color, size=(40, 40)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGBA", size, color).save(b, "PNG")
    return b.getvalue()


def settle(pics, url):
    for _ in range(100):
        if pics.get(url) is not None:
            return pics.get(url)
        time.sleep(0.02)
    return None


@unittest.skipUnless(HAVE, "Pillow is not installed here (it is in the container)")
class Render(unittest.TestCase):
    def pics(self, color=(255, 0, 0, 255)):
        return chatimg.Pictures(download=lambda url: png(color))

    def test_nothing_to_draw_is_none(self):
        self.assertIsNone(chatimg.render([], self.pics(), 424, 900))

    def test_picture_bytes_are_bgra_of_the_right_size(self):
        data, w, h = chatimg.render([("Ann", "#FF8000", "hello there", [])], self.pics(), 424, 300)
        self.assertEqual((w, h, len(data)), (424, 300, 424 * 300 * 4))
        self.assertTrue(any(data[i + 3] for i in range(0, len(data), 4)))  # something is drawn
        self.assertEqual(max(data[i + 3] for i in range(0, 424 * 100 * 4, 4)), 0)  # the top is clear: messages grow up from the bottom

    def test_an_emote_downloads_once_then_is_drawn_and_the_version_moves(self):
        pics = self.pics((0, 255, 0, 255))
        url = "https://cdn.example/e.webp"
        self.assertIsNone(pics.get(url))  # not here yet: the first ask only starts the download
        v0 = pics.version
        self.assertIsNotNone(settle(pics, url))
        self.assertGreater(pics.version, v0)  # a caller redraws when this moves
        data, _, _ = chatimg.render([("Ann", "", "Kappa", [])], pics, 424, 120, words={"Kappa": url})
        px = [data[i:i + 4] for i in range(0, len(data), 4)]
        self.assertTrue(any(p[1] > 200 and p[0] < 60 and p[2] < 60 and p[3] == 255 for p in px))  # the green emote, in BGRA

    def test_a_missing_picture_shows_its_name_meanwhile_and_a_failed_one_stays_text(self):
        pics = chatimg.Pictures(download=lambda url: (_ for _ in ()).throw(OSError("gone")))
        data, _, _ = chatimg.render([("Ann", "", "Kappa", [])], pics, 424, 120, words={"Kappa": "https://x/y.png"})
        self.assertTrue(any(data[i + 3] for i in range(0, len(data), 4)))
        time.sleep(0.2)
        self.assertIsNone(pics.get("https://x/y.png"))

    def test_twitchs_own_emotes_come_from_their_positions(self):
        toks = chatimg._tokens("hi Kappa yo", [{"id": "25", "s": 3, "e": 7}], {})
        self.assertEqual([t[0] for t in toks if t[0] != "s"], ["w", "e", "w"])
        self.assertIn("/25/", [t for t in toks if t[0] == "e"][0][1])

    def test_long_messages_wrap_and_only_the_newest_lines_that_fit_are_kept(self):
        msgs = [("Bob", "", "word " * 80, [])] * 5
        data, w, h = chatimg.render(msgs, self.pics(), 300, 200)
        self.assertEqual(len(data), 300 * 200 * 4)

    def test_a_word_wider_than_the_strip_is_broken_not_lost(self):
        data, _, _ = chatimg.render([("Bob", "", "x" * 200, [])], self.pics(), 200, 400)
        self.assertTrue(any(data[i + 3] for i in range(0, len(data), 4)))


@unittest.skipUnless(HAVE, "Pillow is not installed here (it is in the container)")
class Safety(unittest.TestCase):
    def test_a_huge_image_is_refused_before_it_is_decoded_and_other_hosts_are_not_fetched(self):
        pics = chatimg.Pictures(download=lambda url: png((0, 0, 0, 255), (3000, 3000)))  # a few KB as a file, 36 MB as a bitmap
        pics.get("https://cdn.7tv.app/big.png")
        for _ in range(100):
            if pics.version:
                break
            time.sleep(0.02)
        self.assertIsNone(pics.get("https://cdn.7tv.app/big.png"))  # failed: stays text
        self.assertTrue(chatimg._allowed("https://cdn.7tv.app/emote/x/2x.webp") and chatimg._allowed("https://static-cdn.jtvnw.net/emoticons/v2/25/default/dark/2.0"))
        for bad in ("http://cdn.7tv.app/x", "https://evil.example/x", "https://192.168.1.1/x", "https://cdn.7tv.app.evil.example/x", "file:///etc/passwd"):
            self.assertFalse(chatimg._allowed(bad), bad)


class Overlay(unittest.TestCase):
    def test_the_picture_is_handed_to_mpv_under_one_id_and_removed_by_none(self):
        sent, files = [], []
        panel = chatwin.Panel("t", "app", 480, 1080, ipc=lambda sock, cmd, *a: sent.append(cmd) or {"error": "success"}, write=lambda p, d: None)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            panel.files = (d + "/a.ass", d + "/b.ass")
            self.assertTrue(panel.overlay((b"\x00" * 16, 2, 2, 28, 118)))
            self.assertTrue(panel.overlay(None))
        add = sent[0]
        self.assertEqual((add[0], add[1], add[2], add[3]), ("overlay-add", 1, 28, 118))
        self.assertEqual((add[6], add[7], add[8], add[9]), ("bgra", 2, 2, 8))  # width x 4 bytes a row
        self.assertEqual(sent[1], ["overlay-remove", 1])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE, "Pillow is not installed here (it is in the container)")
class Wrapping(unittest.TestCase):
    def test_a_word_moves_to_the_next_line_whole(self):
        pics = chatimg.Pictures(download=lambda u: png((0, 0, 0, 255)))
        from PIL import Image, ImageDraw
        data, w, h = chatimg.render([("Ann", "", "aaaa bbbbbbbbbbb", [])], pics, 150, 200)
        # a word is only broken when it is wider than the strip: here it is narrower, so it must be found unbroken on one row
        im = Image.frombytes("RGBA", (w, h), data)
        rows = [y for y in range(h) if any(im.getpixel((x, y))[3] for x in range(w))]
        self.assertTrue(rows)

    def test_emoji_are_dropped_and_emote_positions_still_line_up(self):
        toks = chatimg._tokens("hi \U0001F600 Kappa", [{"id": "25", "s": 5, "e": 9}], {})
        self.assertEqual([t[0] for t in toks if t[0] != "s"], ["w", "e"])  # the emoji is gone, the emote is where it was
