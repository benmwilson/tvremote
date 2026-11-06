"""The TV chat's messages as a picture, so emotes can sit inside the lines. The chat window shows text as subtitles (libass), which can draw text but
not pictures, so the messages are drawn here (Pillow) and laid over the window as an image; the header and rule stay subtitles.
Emote pictures (Twitch's own, plus 7TV / BetterTTV / FrankerFaceZ by name) are downloaded once, kept at line height (the first frame of an animated
one) and remembered. Until one has arrived its name is written instead, and the chat redraws when it lands. Without Pillow, available() is False
and the chat stays plain text."""
import io
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # the Pi's image has it; a dev machine may not
    Image = None

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SIZE, LINE, GAP = 26, 36, 10  # text size, line height (an emote is a little taller than the text), air between messages
EMOTE_H, EMOTE_MAX_W = 32, 96
TEXT = (238, 241, 246, 255)  # the chat's text colour (EEF1F6)
OUTLINE = (0, 0, 0, 255)


_NOGLYPH = re.compile("[𐀀-􏿿‍️☀-➿]")  # emoji and symbols the font draws as empty boxes


def available():
    return Image is not None


def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size)  # no DejaVu (a dev machine): Pillow's own


HOSTS = ("static-cdn.jtvnw.net", "cdn.7tv.app", "cdn.betterttv.net", "cdn.frankerfacez.com")  # where emote pictures come from
MAX_PIXELS = 1_000_000  # an emote is tiny; a small file can still decode to a huge bitmap, so anything bigger is refused before it is decoded


def _allowed(url):
    p = urlparse(url)
    return p.scheme == "https" and (p.hostname or "") in HOSTS


def _download(url, timeout=4):  # one place to stub in tests
    if not _allowed(url):
        raise ValueError("not an emote host")  # the addresses come from third-party lists: only their own CDNs are fetched
    return urlopen(Request(url, headers={"User-Agent": "tvremote/1.0"}), timeout=timeout).read(2_000_000)


class Pictures:
    """Emote pictures by address: get() answers at once with the picture or None (and starts the download); `version` goes up whenever
    one arrives or fails, so a caller knows to draw again."""

    def __init__(self, download=_download, workers=4, keep=400):
        self.download, self.keep, self.version = download, keep, 0
        self._pics, self._lock = {}, threading.Lock()  # url -> Image, or False for one that can't be had
        self._pool = ThreadPoolExecutor(max_workers=workers)

    def get(self, url):
        with self._lock:
            if url in self._pics:
                return self._pics[url] or None
            self._pics[url] = False  # asked for: not fetched twice
            while len(self._pics) > self.keep:
                self._pics.pop(next(iter(self._pics)))
        self._pool.submit(self._fetch, url)
        return None

    def _fetch(self, url):
        pic = False
        try:
            im = Image.open(io.BytesIO(self.download(url)))
            if im.width * im.height > MAX_PIXELS:
                raise ValueError("too big for an emote")
            im.seek(0)  # an animated emote: its first frame
            im = im.convert("RGBA")
            w = min(EMOTE_MAX_W, max(1, round(im.width * EMOTE_H / im.height)))
            pic = im.resize((w, EMOTE_H), Image.LANCZOS)
        except Exception:
            pass
        with self._lock:
            self._pics[url] = pic
            self.version += 1


def _tokens(text, native, words):
    """The pieces of a message: ('w', word) / ('e', url, name). native: Twitch's own emotes as [{id, s, e}] (character positions in `text`);
    words: {emote name: picture address} for the third-party sets."""
    out, at, chars = [], 0, list(text)

    def plain(seg):
        for part in re.findall(r"\S+|\s+", seg):
            if part.isspace():
                out.append(("s", part))
            elif part in words:
                out.append(("e", words[part], part))
            elif (part := _NOGLYPH.sub("", part)):  # (stripped per word, so Twitch's emote positions in the text still line up)
                out.append(("w", part))
    for e in native or []:
        if e["s"] < at or e["e"] >= len(chars):
            continue
        plain("".join(chars[at:e["s"]]))
        out.append(("e", f"https://static-cdn.jtvnw.net/emoticons/v2/{e['id']}/default/dark/2.0", "".join(chars[e["s"]:e["e"] + 1])))
        at = e["e"] + 1
    plain("".join(chars[at:]))
    return out


def render(msgs, pics, width, height, words=None, fonts=None):
    """Messages [(name, colour, text, native emotes)] as a transparent picture `width` x `height` with the newest at the bottom.
    Returns (BGRA bytes with premultiplied alpha, width, height) ready for mpv's overlay-add, or None when there is nothing to draw."""
    if not msgs:
        return None
    regular, bold = fonts or (_font(FONT, SIZE), _font(FONT_BOLD, SIZE))
    words = words or {}
    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    rows = []  # one list of drawable items per line: ('t', text, font, colour, width) or ('i', picture, width); None = a gap between messages

    for name, colour, text, native in msgs:
        name = _NOGLYPH.sub("", name) or "?"
        rgb = tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5)) + (255,) if re.fullmatch(r"#[0-9A-Fa-f]{6}", colour or "") else TEXT
        line, x = [], 0

        def put(item, w):
            nonlocal line, x
            if x and x + w > width:
                rows.append(line)
                line, x = [], 0
            line.append(item + (w,))
            x += w
        put(("t", name, bold, rgb), scratch.textlength(name, font=bold))
        put(("t", ": ", regular, TEXT), scratch.textlength(": ", font=regular))
        for tok in _tokens(text, native, words):
            if tok[0] == "s":
                if x:
                    sp = scratch.textlength(" ", font=regular)
                    if x + sp <= width:
                        line.append(("t", " ", regular, TEXT, sp))
                        x += sp
                continue
            pic = pics.get(tok[1]) if tok[0] == "e" else None
            if tok[0] == "e" and pic is not None:
                put(("i", pic), pic.width + 2)
                continue
            word = tok[2] if tok[0] == "e" else tok[1]  # a picture not here yet: its name for now
            whole = scratch.textlength(word, font=regular)
            if x and x + whole > width and whole <= width:  # a word that fits on a line of its own goes to the next one whole
                rows.append(line)
                line, x = [], 0
            while word:  # (only a word wider than the strip is broken, letter by letter)
                fit = len(word)
                while fit > 1 and scratch.textlength(word[:fit], font=regular) > width - x:
                    fit -= 1
                w = scratch.textlength(word[:fit], font=regular)
                line.append(("t", word[:fit], regular, TEXT, w))
                x += w
                word = word[fit:]
                if word:
                    rows.append(line)
                    line, x = [], 0
        rows.append(line)
        rows.append(None)
    if rows and rows[-1] is None:
        rows.pop()

    pitch = lambda r: GAP if r is None else LINE
    total = sum(pitch(r) for r in rows)
    keep, used = [], 0
    for r in reversed(rows):  # the newest lines that fit, whole
        if used + pitch(r) > height:
            break
        keep.append(r)
        used += pitch(r)
    keep.reverse()
    im = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    y = height - used
    for r in keep:
        if r is not None:
            x = 0
            for item in r:
                if item[0] == "t":
                    d.text((x, y + (LINE - SIZE) // 2 - 2), item[1], font=item[2], fill=item[3], stroke_width=2, stroke_fill=OUTLINE)
                else:
                    im.alpha_composite(item[1], (int(x), y + (LINE - item[1].height) // 2))
                x += item[-1]
        y += pitch(r)
    r, g, b, a = im.convert("RGBa").split()
    return Image.merge("RGBA", (b, g, r, a)).tobytes(), width, height
