"""The TV chat's messages as a picture, so emotes can sit inside the lines. The chat window shows text as subtitles (libass), which can draw text but
not pictures, so the messages are drawn here (Pillow) and laid over the window as an image; the header and rule stay subtitles.
Emote pictures (Twitch's own, plus 7TV / BetterTTV / FrankerFaceZ by name) are downloaded once, kept at line height (the first frame of an animated
one) and remembered. Until one has arrived its name is written instead, and the chat redraws when it lands. Without Pillow, available() is False
and the chat stays plain text."""
import io
import os
import re
import threading
from collections import OrderedDict
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


HOSTS = ("static-cdn.jtvnw.net", "cdn.7tv.app", "cdn.betterttv.net", "cdn.frankerfacez.com", "files.kick.com")  # where emote pictures come from
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
    """The pieces of a message: ('w', word) / ('e', url, name). native: the site's own emotes as [{id, s, e}] or [{u, s, e}] with a picture address
    (character positions in `text`);
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
        out.append(("e", e.get("u") or f"https://static-cdn.jtvnw.net/emoticons/v2/{e['id']}/default/dark/2.0", "".join(chars[e["s"]:e["e"] + 1])))
        at = e["e"] + 1
    plain("".join(chars[at:]))
    return out


_fonts = []


def _default_fonts():
    if not _fonts:  # loaded once: the same font objects every time, which the cache of drawn messages relies on
        _fonts.extend((_font(FONT, SIZE), _font(FONT_BOLD, SIZE)))
    return tuple(_fonts)


_cache = OrderedDict()  # one drawn message each: key -> (picture, how many lines). Only a message that has just arrived is drawn; the rest are reused.
CACHE_KEEP = 60


def _measure(font, text):
    return font.getlength(text)


def _draw_message(name, colour, text, native, pics, width, words, regular, bold):
    """One message as a transparent picture `width` wide and a whole number of lines (LINE px each) tall, with its emotes in it."""
    rgb = tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5)) + (255,) if re.fullmatch(r"#[0-9A-Fa-f]{6}", colour or "") else TEXT
    rows, line, x = [], [], 0  # a line is a list of ['t', text, font, colour, width] and ['i', picture, width]

    def newline():
        nonlocal line, x
        rows.append(line)
        line, x = [], 0

    def add_text(piece, font, col):
        nonlocal x
        w = _measure(font, piece)
        if line and line[-1][0] == "t" and line[-1][2] is font and line[-1][3] == col:  # one drawing call for a run of text in one style
            line[-1][1] += piece
            line[-1][4] += w
        else:
            line.append(["t", piece, font, col, w])
        x += w

    add_text(name, bold, rgb)
    add_text(": ", regular, TEXT)
    for tok in _tokens(text, native, words):
        if tok[0] == "s":
            sp = _measure(regular, " ")
            if x and x + sp <= width:
                add_text(" ", regular, TEXT)
            continue
        pic = pics.get(tok[1]) if tok[0] == "e" else None
        if pic is not None:
            if x and x + pic.width + 2 > width:
                newline()
            line.append(["i", pic, pic.width + 2])
            x += pic.width + 2
            continue
        word = tok[2] if tok[0] == "e" else tok[1]  # a picture not here yet: its name for now
        whole = _measure(regular, word)
        if x and x + whole > width and whole <= width:  # a word that fits on a line of its own goes to the next one whole
            newline()
        while word:  # (only a word wider than the strip is broken, letter by letter)
            fit = len(word)
            while fit > 1 and _measure(regular, word[:fit]) > width - x:
                fit -= 1
            add_text(word[:fit], regular, TEXT)
            word = word[fit:]
            if word:
                newline()
    rows.append(line)
    im = Image.new("RGBA", (width, LINE * len(rows)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for n, r in enumerate(rows):
        y, x = n * LINE, 0
        for item in r:
            if item[0] == "t":
                d.text((x, y + (LINE - SIZE) // 2 - 2), item[1], font=item[2], fill=item[3])
            else:
                im.alpha_composite(item[1], (int(x), y + (LINE - item[1].height) // 2))
            x += item[-1]
    return im, len(rows)


def render(msgs, pics, width, height, words=None, fonts=None):
    """Messages [(name, colour, text, native emotes)] as a transparent picture `width` x `height` with the newest at the bottom.
    Returns (BGRA bytes with premultiplied alpha, width, height) ready for mpv's overlay-add, or None when there is nothing to draw.
    A message is drawn once and kept: a redraw only draws what has arrived since (the Pi spent most of a second drawing them all every time)."""
    if not msgs:
        return None
    regular, bold = fonts or _default_fonts()
    words = words or {}
    pieces, used = [], 0  # newest first
    for name, colour, text, native in reversed(msgs):
        name = _NOGLYPH.sub("", name) or "?"
        has_emote = bool(native) or any(w in words for w in text.split())
        key = (name, colour, text, tuple(tuple(sorted(e.items())) for e in native or ()), width, pics.version if has_emote else 0, id(regular))
        hit = _cache.get(key)
        if hit is None:
            hit = _cache[key] = _draw_message(name, colour, text, native, pics, width, words, regular, bold)
            while len(_cache) > CACHE_KEEP:
                _cache.popitem(last=False)
        else:
            _cache.move_to_end(key)
        im, lines = hit
        room = height - used - (GAP if pieces else 0)
        if room < LINE:
            break
        keep = min(lines, room // LINE)  # a message that does not fit whole shows its last lines
        pieces.append((im if keep == lines else im.crop((0, (lines - keep) * LINE, width, lines * LINE)), keep))
        used += keep * LINE + (GAP if len(pieces) > 1 else 0)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    y = height
    for i, (im, keep) in enumerate(pieces):
        y -= keep * LINE
        canvas.alpha_composite(im, (0, y))
        y -= GAP
    r, g, b, a = canvas.convert("RGBa").split()
    return Image.merge("RGBA", (b, g, r, a)).tobytes(), width, height
