"""The TV's home screen: a fullscreen window that is always there behind the streams, so the Pi desktop never shows
between them. It shows Ready (clock, date, the remote's address), Loading, "Not live right now",
"Stream ended" and error screens.

One mpv plays a plain colour; the text is a subtitle file (osd-overlay and styled show-text were dropped by this window
on the Pi, subtitles are not). The file is written ahead of time with one event per second (the clock's colon blinks) or
per dot frame, timed to the video clock, so mpv does the animating and nothing redraws: reloading a subtitle every second
flashed the whole screen. While a stream plays the shell is paused and silent, so it costs the stream nothing.
shortcut: relies on the desktop stacking a newer fullscreen window above this one; if a stream ever appears behind it,
turn the home screen off from the remote and tell me what the Pi's desktop does."""
import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta

import mpvipc
import multi
from chat import _esc as esc

SOCK = "/tmp/tvremote-shell.sock"
LOG = "/tmp/tvremote-shell.log"
# The logo as ASS drawings (same shapes as app/logo.svg, solid accent colour), 260% size, centred at x=960. The screen's
# outline is a hollow shape with a thick border; the stand strokes are zero-width shapes whose border gives them width.
SCALE = 2.6                 # the mark is drawn at 260%
GLYPH_OFF, GLYPH_H = 45, 180  # px from the drawing's top edge to the visible mark, and the visible height (stand included)
GAP = 30                    # px between the stand and the text
LINE = 1.0                  # measured on the Pi: a text line is about its font size tall (the estimate of 1.17 sat the group 30 px high)
_L = r"{\an7\pos(830,%d)\fscx260\fscy260\shad0\p1"


def logo_pieces(y, accent=None):
    """The mark as ASS drawings with its drawing origin at height y (solid colour: the accent, or the default blue; subtitles have no gradients)."""
    c = accent or ACCENT
    head = (_L % y) + r"\1a&HFF&\3c&H" + c + r"&\bord8}"
    return [
        head + r"m 26 22 l 74 22 b 80 22 86 28 86 34 l 86 58 b 86 64 80 70 74 70 l 26 70 b 20 70 14 64 14 58 l 14 34 b 14 28 20 22 26 22{\p0}",
        (_L % y) + r"\1c&H" + c + r"&\3c&H" + c + r"&\bord5}m 44 36 l 44 56 l 61 46{\p0}",
        head + r"m 38 82 l 62 82{\p0}",
        head + r"m 50 73 l 50 82{\p0}",
    ]


def layout(text):
    """(logo y, text y): the mark and the text below it, as one group centred on the screen. The text height is worked out
    from its font sizes, one line per \\N."""
    body = re.sub(r"^\{[^}]*\}", "", text, count=1)
    h = sum(max([int(n) for n in re.findall(r"\\fs(\d+)", line)] or [0]) for line in body.split(r"\N")) * LINE
    top = (H - (GLYPH_H + GAP + h)) / 2
    return round(top - GLYPH_OFF), round(top + GLYPH_H + GAP)


QR_MODULE, QR_MARGIN = 8, 56  # px per dot; gap from the screen's right and bottom edges


def qr_pieces(url):
    """The remote's QR code as ASS drawings for the Ready screen's lower right: a light card, one drawing of rounded dark dots, a caption.
    [] when there is no address or the QR library isn't installed (the written address is still on screen)."""
    try:
        import segno
        rows = [list(r) for r in segno.make(url, error="m", boost_error=False).matrix] if url else []
    except Exception:
        return []
    if not rows:
        return []
    n, m = len(rows), QR_MODULE
    q = 4 * m  # the quiet zone a scanner needs round the code
    side = n * m + 2 * q
    x0, y0 = W - QR_MARGIN - side, H - QR_MARGIN - side - 44
    card = r"{\an7\pos(%d,%d)\p1\bord0\shad0\1c&H%s&}m 0 0 l %d 0 %d %d 0 %d{\p0}" % (x0, y0, TEXT, side, side, side, side)
    inset, r = 0.5, 2.6  # each dot is a rounded square a little smaller than its cell
    c, d = m - 2 * inset, []
    for j, row in enumerate(rows):
        for i, on in enumerate(row):
            if on:
                a, b = i * m + inset, j * m + inset
                d.append("m %.1f %.1f l %.1f %.1f %.1f %.1f %.1f %.1f %.1f %.1f %.1f %.1f %.1f %.1f %.1f %.1f" % (
                    a + r, b, a + c - r, b, a + c, b + r, a + c, b + c - r, a + c - r, b + c, a + r, b + c, a, b + c - r, a, b + r))
    dots = r"{\an7\pos(%d,%d)\p1\bord0\shad0\1c&H%s&}%s{\p0}" % (x0 + q, y0 + q, BG[4:] + BG[2:4] + BG[:2], " ".join(d))  # BG is RGB, ASS wants BGR
    cap = r"{\an8\pos(%d,%d)\fs28\b1\bord0\shad0\c&H%s&}Scan for the remote" % (x0 + side // 2, y0 + side + 8, DIM)
    return [card, dots, cap]


BG_IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "bg-full.png")


def _source():
    """The window's video: the shared background picture, looped at 2 frames a second (the 2 fps clock times the subtitles), or a plain colour
    if the picture is missing. The chat strip and the bar under the picture are cut from the same picture, so the two join with no seam."""
    if os.path.exists(BG_IMAGE):
        return f"movie=filename={BG_IMAGE},loop=loop=-1:size=1,fps=2"  # (the movie filter's own loop option hangs on a single picture; no format conversion: video colours made it differ from the chat window's picture by a visible step)
    return f"color=c=0x{BG}:s={W}x{H}:r=2"


ASS_FILES = ("/tmp/tvremote-shell-a.ass", "/tmp/tvremote-shell-b.ass")  # alternate, so a new one loads before the old goes
IDLE_SPAN = 300  # seconds of clock written ahead; the file is renewed a little before it runs out
FOREVER = 3 * 86400  # a still screen stays up for days of video time
W, H = 1920, 1080
BG = "0b0e14"
VO_ORDER = tuple(v for v in os.environ.get("TVREMOTE_SHELL_VO", "wlshm,gpu").split(",") if v)  # wlshm: plain software drawing; gpu if it won't open
# ASS colours are BGR
TEXT, DIM, ACCENT, BAD, WARN = "F6F1EE", "A9978C", "FFB062", "6B6BFF", "47B5FF"
SRC_BGR = {"Kick": "18FC53", "Twitch": "FF70A9", "YouTube": "3B3BFF"}  # the sites' colours, as ASS wants them (BGR)
HEADS = {"idle": ("Ready", ACCENT), "loading": ("Loading", ACCENT), "quality": ("Switching quality", ACCENT), "refresh": ("Refreshing the stream", ACCENT), "error": ("Couldn't play", BAD),
         "offline": ("Not live right now", WARN), "ended": ("Stream ended", DIM)}
PROPS = ("current-vo", "vo-configured", "fullscreen", "osd-width", "osd-height", "pause", "core-idle", "eof-reached",
         "time-pos", "idle-active", "window-minimized")


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent; this just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "this Pi"


def clean(s):
    """Drop emoji and other characters the screen's font can't draw (they come out as empty boxes)."""
    return re.sub(r"[𐀀-􏿿‍️☀-➿]", "", s or "").strip()


def clock12(now):
    """12-hour time; the colon blinks with the seconds (hidden, not removed, so the digits never shift)."""
    colon = ":" if now.second % 2 == 0 else r"{\alpha&HFF&}:{\alpha&H00&}"
    return "%d%s%02d %s" % (now.hour % 12 or 12, colon, now.minute, "AM" if now.hour < 12 else "PM")


def screen(kind, detail="", hint="", dots=0, now=None, favs=(), accent=None):
    """ASS text for one screen, centred. The Ready screen carries the clock, date and the remote's address."""
    head, colour = HEADS[kind]
    if accent and kind in ("idle", "loading", "quality", "refresh"):
        colour = accent  # the playing site's colour (errors and "not live" keep their own)
    detail = clean(detail)
    if kind == "idle":
        now = now or datetime.now()
        lines = [r"{\fs190\b1\c&H%s&}%s" % (TEXT, clock12(now)),
                 r"{\fs48\b0\c&H%s&}%s %d" % (DIM, now.strftime("%A, %B"), now.day),
                 r"{\fs50} ",
                 r"{\fs60\b1\c&H%s&}TV Remote   {\c&H%s&}%s" % (TEXT, colour, head)]
        if hint:
            lines.append(r"{\fs34\b0\c&H%s&}%s" % (DIM, esc(hint)))
        return r"{\an8\pos(960,@@Y@@)\bord0\shad0}" + r"\N".join(lines)
    if kind == "refresh":  # a spinning wheel: four quarter-filled circles in turn
        head = "◐◓◑◒"[dots % 4] + "  " + head
    elif kind in ("loading", "quality"):
        n = dots % 4
        head += "." * n + r"\h" * (3 - n)  # hard spaces keep the line the same width
    if kind == "error":  # what went wrong is the title, the details under it
        lines = [r"{\fs84\b1\c&H%s&}%s" % (colour, head)]
        if detail:
            lines.append(r"{\fs44\b0\c&H%s&}%s" % (TEXT, esc(detail)))
    else:  # loading, quality, refresh, offline, ended: the channel's name large (else the app's), what is happening under it, like the transition card
        lines = [r"{\fs84\b1\c&H%s&}%s" % (TEXT, esc(detail) or "TV Remote"), r"{\fs50\b0\c&H%s&}%s" % (colour, head)]
    if hint:
        lines.append(r"{\fs34\b0\c&H%s&}%s" % (DIM, esc(hint)))
    return r"{\an8\pos(960,@@Y@@)\bord0\shad0}" + r"\N".join(lines)


def transition_screen(title, line, source=None):
    """The card shown over the picture while a stream or its quality changes (a window of its own on the shared background): the logo, the
    channel's name, the site in its own colour and one line of what is happening. One ASS event text per line, joined by newlines."""
    accent = SRC_BGR.get(source, ACCENT)
    out = list(logo_pieces(255, accent))
    out.append(r"{\an8\pos(960,520)\fs84\b1\bord0\shad0\1c&H%s&}%s" % (TEXT, esc(clean(title))[:26] or "TV Remote"))
    if source:
        out.append(r"{\an8\pos(960,625)\fs34\b1\fsp6\bord0\shad0\1c&H%s&}%s" % (accent, esc(source).upper()))
    out.append(r"{\an8\pos(960,690)\fs46\b0\bord0\shad0\1c&H%s&}%s" % (DIM, esc(line)[:50]))
    return "\n".join(out)


def wall_screen(layout, labels):
    """The background behind several streams: soft colour glows, and each stream's name on the strip under its window.
    labels: [(name, source, ended)] in tile order; an ended stream's slot says so. Returns one ASS event text per piece."""
    def glow(x, y, w, h, colour, alpha):  # a big rectangle, blurred into a soft light
        return r"{\an7\pos(%d,%d)\p1\bord0\shad0\1c&H%s&\1a&H%s&\blur140}m 0 0 l %d 0 %d %d 0 %d{\p0}" % (x, y, colour, alpha, w, w, h, h)
    out = []  # the glows are in the background picture (tools/make_chat_bg.py), so the chat strip matches them exactly
    for (x, y, w, h), (name, source, ended) in zip(multi.tiles(layout, len(labels)), labels):
        # a panel under every window: where a stream is still starting (or has ended) it shows through and says which one. A playing
        # window covers it completely.
        out.append(r"{\an7\pos(%d,%d)\p1\bord0\shad0\1c&H000000&\1a&H%s&}m 0 0 l %d 0 %d %d 0 %d{\p0}" % (x, y, "70" if ended else "A0", w, w, h, h))
        out.append(r"{\an5\pos(%d,%d)\fs38\b1\bord0\shad0\c&H%s&}%s\N{\fs26\b0\c&H%s&}%s" % (
            x + w // 2, y + h // 2, TEXT if ended else DIM, "Went offline" if ended else "Loading", DIM, esc(clean(name))[:30]))
        dot = SRC_BGR.get(source, DIM) if not ended else DIM
        out.append(r"{\an4\pos(%d,%d)\fs26\b1\bord0\shad0\c&H%s&}%s  {\c&H%s&}%s" % (
            x + 2, y + h + multi.S // 2 + 2, dot, "●", TEXT, esc(clean(name))[:30]))
    return out


def stamp(sec):
    """Seconds as the ASS time h:mm:ss.cc."""
    cs = int(round(max(0.0, sec) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    sc, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{sc:02d}.{cs:02d}"


def ass_file(events, accent=None, logo=True, extra=()):
    """A subtitle file from [(start_seconds, end_seconds, text)], timed to the window's video clock."""
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,DejaVu Sans,60,&H00F6F1EE,&H00F6F1EE,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ly, ty = layout(events[0][2]) if events else (0, 0)
    body = "".join(f"Dialogue: 0,{stamp(a)},{stamp(b)},Default,,0,0,0,,{t.replace('@@Y@@', str(ty))}\n" for a, b, t in events)
    if events and logo:  # the logo is its own events spanning the file, so it never flickers when the text changes
        span = f"{stamp(min(e[0] for e in events))},{stamp(max(e[1] for e in events))}"
        body += "".join(f"Dialogue: 0,{span},Default,,0,0,0,,{piece}\n" for piece in [*logo_pieces(ly, accent), *extra])
    return head + body


def write_file(path, data):
    with open(path, "wb" if isinstance(data, bytes) else "w", **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as f:
        f.write(data)


class Shell:
    interval = 0.6        # seconds between loading-dot frames
    renew = IDLE_SPAN - 30  # seconds after which the Ready screen's file is written again

    def __init__(self, hint="", spawn=subprocess.Popen, ipc=mpvipc.send, ready=None, sleep=time.sleep,
                 clock=datetime.now, favs=lambda: [], write=write_file, qr=lambda: ""):
        self.hint, self._spawn, self._ipc, self._sleep = hint, spawn, ipc, sleep
        self._clock, self._favs, self._write = clock, favs, write
        self._qr = qr  # the address to draw as a QR code on the Ready screen, or "" for none
        self._n, self._sid = 0, None  # which file to write next; the subtitle track now showing
        self._ready = ready or (lambda: os.path.exists(SOCK))
        self.proc = self.vo = self.error = None
        self.kind, self.detail, self.accent = "idle", "", None
        self.wall = None  # (layout, [(name, source)]) while several streams are on the screen
        self._gen = 0
        self._lock = threading.RLock()

    def running(self):
        return bool(self.proc and self.proc.poll() is None)

    def _cmd(self, vo):
        return ["mpv", "--no-config", f"--vo={vo}", "--hwdec=no", "--fs", "--force-window=yes", "--keep-open=yes",
                "--loop-file=inf", "--pause=no", "--no-audio", "--no-terminal", "--osc=no", "--osd-level=1",
                "--cursor-autohide=always", "--input-default-bindings=no", "--input-vo-keyboard=no",
                "--wayland-app-id=tvremote-shell", "--title=TV Remote", f"--input-ipc-server={SOCK}",
                f"--background-color=#{BG}", f"av://lavfi:{_source()}"]

    def start(self):
        """Open the window with the first output that works. Returns whether it is running."""
        with self._lock:
            if self.running():
                return True
            self._sid = None
            for vo in VO_ORDER:
                try:
                    os.unlink(SOCK)
                except OSError:
                    pass
                try:
                    out = open(LOG, "a")
                except OSError:
                    out = subprocess.DEVNULL
                p = self._spawn(self._cmd(vo), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
                for _ in range(24):  # up to ~6 s for the window and its control socket
                    if p.poll() is not None or self._ready():
                        break
                    self._sleep(0.25)
                if p.poll() is None and self._ready():
                    self.proc, self.vo, self.error = p, vo, None
                    self._show_current()
                    return True
                if p.poll() is None:
                    p.terminate()
            self.error = "the home screen window wouldn't open (details: " + LOG + " inside the container)"
            return False

    def stop(self):
        with self._lock:
            self._gen += 1
            p, self.proc = self.proc, None
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(3)
                except Exception:
                    pass

    def show(self, kind, detail="", accent=None):
        """kind: idle, loading, error, offline, ended, or playing (hide everything). Returns whether it was shown."""
        with self._lock:
            self.kind, self.detail, self.accent = kind, detail, accent
            self._gen += 1
            return self._show_current() if self.running() else False

    def show_wall(self, layout, labels):
        """The background for several streams: the window stays up behind them (not paused, not hidden)."""
        with self._lock:
            self.kind, self.detail, self.accent, self.wall = "wall", "", None, (layout, labels)
            self._gen += 1
            return self._show_current() if self.running() else False

    def props(self):
        """What the window is doing, for the debug view."""
        out = {}
        for name in PROPS:
            r = self._ipc(SOCK, ["get_property", name])
            out[name] = r.get("data") if r and r.get("error") == "success" else None
        return out

    def _pos(self):
        r = self._ipc(SOCK, ["get_property", "time-pos"])
        v = r.get("data") if r and r.get("error") == "success" else None
        return float(v) if isinstance(v, (int, float)) else 0.0

    def _events(self, kind):
        """[(start, end, text)] on the window's video clock, starting now."""
        pos = self._pos()
        if kind == "idle":  # one event per wall-clock second, so the colon blinks in step with the real seconds
            now = self._clock()
            frac, base = now.microsecond / 1e6, now.replace(microsecond=0)
            out = []
            for k in range(IDLE_SPAN):
                a = pos - frac + k
                out.append((a, a + 1, screen("idle", hint=self.hint, now=base + timedelta(seconds=k), accent=self.accent)))
            return out
        if kind == "wall" and self.wall:
            return [(pos, pos + FOREVER, t) for t in wall_screen(*self.wall)]
        hint = "Pick something else on your phone" if kind in ("ended", "offline", "error") else ""
        if kind in ("loading", "quality", "refresh"):  # the dots (or wheel) move for 90 s, then the last frame stays
            step = 0.25 if kind == "refresh" else self.interval
            n = int(90 / step)
            out = [(pos + k * step, pos + (k + 1) * step, screen(kind, self.detail, hint, k, accent=self.accent)) for k in range(n)]
            out[-1] = (out[-1][0], pos + FOREVER, out[-1][2])
            return out
        return [(pos, pos + FOREVER, screen(kind, self.detail, hint, accent=self.accent))]

    def _load(self, events):
        """Write the events to the other file, show it, then drop the old one (no blank in between)."""
        path = ASS_FILES[self._n % 2]
        self._n += 1
        try:
            self._write(path, ass_file(events, self.accent, self.kind != "wall", qr_pieces(self._qr()) if self.kind == "idle" else ()))
        except OSError:
            return
        r = self._ipc(SOCK, ["sub-add", path, "select"])
        if not (r and r.get("error") == "success"):
            return
        old = self._sid
        sid = self._ipc(SOCK, ["get_property", "sid"])
        self._sid = sid.get("data") if sid and sid.get("error") == "success" else None
        if old is not None and old != self._sid:
            self._ipc(SOCK, ["sub-remove", old])

    def _show_current(self):
        kind = self.kind
        if kind == "playing":
            self._ipc(SOCK, ["set_property", "pause", True])
            self._ipc(SOCK, ["set_property", "sub-visibility", False])
            return True
        self._ipc(SOCK, ["set_property", "pause", False])
        self._ipc(SOCK, ["set_property", "sub-visibility", True])
        self._load(self._events(kind))
        if kind == "idle":
            threading.Thread(target=self._renew, args=(self._gen,), daemon=True).start()
        return True

    def _renew(self, gen):
        """The Ready screen's file only covers a few minutes ahead: write it again before it runs out."""
        while True:
            threading.Event().wait(self.renew)
            with self._lock:
                if gen != self._gen or not self.running():
                    return
                self._load(self._events("idle"))
