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
    lines = [r"{\fs110\b1\c&H%s&}TV Remote" % TEXT, r"{\fs60\b0\c&H%s&}%s" % (colour, head)]
    if detail:
        lines.append(r"{\fs44\c&H%s&}%s" % (TEXT, esc(detail)))
    if hint:
        lines.append(r"{\fs34\c&H%s&}%s" % (DIM, esc(hint)))
    return r"{\an8\pos(960,@@Y@@)\bord0\shad0}" + r"\N".join(lines)


def stamp(sec):
    """Seconds as the ASS time h:mm:ss.cc."""
    cs = int(round(max(0.0, sec) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    sc, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{sc:02d}.{cs:02d}"


def ass_file(events, accent=None):
    """A subtitle file from [(start_seconds, end_seconds, text)], timed to the window's video clock."""
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,DejaVu Sans,60,&H00F6F1EE,&H00F6F1EE,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ly, ty = layout(events[0][2]) if events else (0, 0)
    body = "".join(f"Dialogue: 0,{stamp(a)},{stamp(b)},Default,,0,0,0,,{t.replace('@@Y@@', str(ty))}\n" for a, b, t in events)
    if events:  # the logo is its own events spanning the file, so it never flickers when the text changes
        span = f"{stamp(min(e[0] for e in events))},{stamp(max(e[1] for e in events))}"
        body += "".join(f"Dialogue: 0,{span},Default,,0,0,0,,{piece}\n" for piece in logo_pieces(ly, accent))
    return head + body


def write_file(path, data):
    with open(path, "wb" if isinstance(data, bytes) else "w", **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as f:
        f.write(data)


class Shell:
    interval = 0.6        # seconds between loading-dot frames
    renew = IDLE_SPAN - 30  # seconds after which the Ready screen's file is written again

    def __init__(self, hint="", spawn=subprocess.Popen, ipc=mpvipc.send, ready=None, sleep=time.sleep,
                 clock=datetime.now, favs=lambda: [], write=write_file):
        self.hint, self._spawn, self._ipc, self._sleep = hint, spawn, ipc, sleep
        self._clock, self._favs, self._write = clock, favs, write
        self._n, self._sid = 0, None  # which file to write next; the subtitle track now showing
        self._ready = ready or (lambda: os.path.exists(SOCK))
        self.proc = self.vo = self.error = None
        self.kind, self.detail, self.accent = "idle", "", None
        self._gen = 0
        self._lock = threading.RLock()

    def running(self):
        return bool(self.proc and self.proc.poll() is None)

    def _cmd(self, vo):
        return ["mpv", "--no-config", f"--vo={vo}", "--hwdec=no", "--fs", "--force-window=yes", "--keep-open=yes",
                "--loop-file=inf", "--pause=no", "--no-audio", "--no-terminal", "--osc=no", "--osd-level=1",
                "--cursor-autohide=always", "--input-default-bindings=no", "--input-vo-keyboard=no",
                "--wayland-app-id=tvremote-shell", "--title=TV Remote", f"--input-ipc-server={SOCK}",
                f"--background-color=#{BG}", f"av://lavfi:color=c=0x{BG}:s={W}x{H}:r=2"]

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
            self._write(path, ass_file(events, self.accent))
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
