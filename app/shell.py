"""The TV's home screen: a fullscreen window that is always there behind the streams, so the Pi desktop never shows
between them. It shows Ready (clock, date, the remote's address, your favourites), Loading, "Not live right now",
"Stream ended" and error screens.

One mpv shows a plain still picture; the text is a subtitle file (osd-overlay and styled show-text were dropped by this
window on the Pi, subtitles are not). While a stream plays the shell is paused and silent, so it costs the stream nothing
(a redrawing overlay on top of moving video is what dropped frames for chat).
shortcut: relies on the desktop stacking a newer fullscreen window above this one; if a stream ever appears behind it,
turn the home screen off from the remote and tell me what the Pi's desktop does."""
import os
import re
import socket
import struct
import subprocess
import threading
import time
import zlib
from datetime import datetime

import mpvipc
from chat import _esc as esc

SOCK = "/tmp/tvremote-shell.sock"
LOG = "/tmp/tvremote-shell.log"
ASS_FILE = "/tmp/tvremote-shell.ass"
BG_FILE = "/tmp/tvremote-shell-bg.png"
W, H = 1920, 1080
BG = "0b0e14"
VO_ORDER = tuple(v for v in os.environ.get("TVREMOTE_SHELL_VO", "wlshm,gpu").split(",") if v)  # wlshm: plain software drawing; gpu if it won't open
# ASS colours are BGR
TEXT, DIM, ACCENT, BAD, WARN = "F6F1EE", "A9978C", "FFB062", "6B6BFF", "47B5FF"
HEADS = {"idle": ("Ready", ACCENT), "loading": ("Loading", ACCENT), "quality": ("Switching quality", ACCENT), "error": ("Couldn't play", BAD),
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


def screen(kind, detail="", hint="", dots=0, now=None, favs=()):
    """ASS text for one screen, centred. The Ready screen carries the clock, date and the remote's address."""
    head, colour = HEADS[kind]
    detail = clean(detail)
    if kind == "idle":
        now = now or datetime.now()
        lines = [r"{\fs190\b1\c&H%s&}%s" % (TEXT, clock12(now)),
                 r"{\fs48\b0\c&H%s&}%s %d" % (DIM, now.strftime("%A, %B"), now.day),
                 r"{\fs50} ",
                 r"{\fs60\b1\c&H%s&}TV Remote   {\c&H%s&}%s" % (TEXT, colour, head)]
        if hint:
            lines.append(r"{\fs34\b0\c&H%s&}%s" % (DIM, esc(hint)))
        return r"{\an5\pos(960,540)\bord0\shad0}" + r"\N".join(lines)
    if kind in ("loading", "quality"):
        n = dots % 4
        head += "." * n + r"\h" * (3 - n)  # hard spaces keep the line the same width
    lines = [r"{\fs110\b1\c&H%s&}TV Remote" % TEXT, r"{\fs60\b0\c&H%s&}%s" % (colour, head)]
    if detail:
        lines.append(r"{\fs44\c&H%s&}%s" % (TEXT, esc(detail)))
    if hint:
        lines.append(r"{\fs34\c&H%s&}%s" % (DIM, esc(hint)))
    return r"{\an5\pos(960,540)\bord0\shad0}" + r"\N".join(lines)


def bg_png(rgb_hex, w=64, h=36):
    """A small solid-colour PNG; mpv scales it to fill the screen."""
    rgb = bytes.fromhex(rgb_hex)
    raw = (b"\x00" + rgb * w) * h

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ass_file(text):
    """A subtitle file holding one event, on screen for the whole (very long) run of the still picture."""
    return (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,DejaVu Sans,60,&H00F6F1EE,&H00F6F1EE,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.00,9:59:59.99,Default,,0,0,0,,{}\n".format(text))


def write_file(path, data):
    with open(path, "wb" if isinstance(data, bytes) else "w", **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as f:
        f.write(data)


class Shell:
    interval = 0.6        # seconds between loading-dot frames
    idle_interval = 1     # the Ready screen redraws every second (the colon blinks); nothing is playing then, so it costs nothing

    def __init__(self, hint="", spawn=subprocess.Popen, ipc=mpvipc.send, ready=None, sleep=time.sleep,
                 clock=datetime.now, favs=lambda: [], write=write_file):
        self.hint, self._spawn, self._ipc, self._sleep = hint, spawn, ipc, sleep
        self._clock, self._favs, self._write = clock, favs, write
        self._sub = False  # whether the subtitle file is loaded in the window yet
        self._ready = ready or (lambda: os.path.exists(SOCK))
        self.proc = self.vo = self.error = None
        self.kind, self.detail = "idle", ""
        self._gen = 0
        self._lock = threading.RLock()

    def running(self):
        return bool(self.proc and self.proc.poll() is None)

    def _cmd(self, vo):
        return ["mpv", "--no-config", f"--vo={vo}", "--hwdec=no", "--fs", "--force-window=yes", "--keep-open=yes",
                "--image-display-duration=inf", "--pause=no", "--no-audio", "--no-terminal", "--osc=no", "--osd-level=1",
                "--cursor-autohide=always", "--input-default-bindings=no", "--input-vo-keyboard=no",
                "--wayland-app-id=tvremote-shell", "--title=TV Remote", f"--input-ipc-server={SOCK}",
                f"--background-color=#{BG}", BG_FILE]

    def start(self):
        """Open the window with the first output that works. Returns whether it is running."""
        with self._lock:
            if self.running():
                return True
            self._sub = False
            try:
                self._write(BG_FILE, bg_png(BG))
            except OSError:
                pass
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

    def show(self, kind, detail=""):
        """kind: idle, loading, error, offline, ended, or playing (hide everything). Returns whether it was shown."""
        with self._lock:
            self.kind, self.detail = kind, detail
            self._gen += 1
            return self._show_current() if self.running() else False

    def props(self):
        """What the window is doing, for the debug view."""
        out = {}
        for name in PROPS:
            r = self._ipc(SOCK, ["get_property", name])
            out[name] = r.get("data") if r and r.get("error") == "success" else None
        return out

    def _show_current(self):
        kind = self.kind
        if kind == "playing":
            self._ipc(SOCK, ["set_property", "pause", True])
            self._ipc(SOCK, ["set_property", "sub-visibility", False])
            return True
        self._ipc(SOCK, ["set_property", "pause", False])
        self._ipc(SOCK, ["set_property", "sub-visibility", True])
        self._draw(0)
        if kind in ("loading", "quality", "idle"):
            threading.Thread(target=self._animate, args=(self._gen, kind), daemon=True).start()
        return True

    def _draw(self, dots):
        kind = self.kind
        hint = self.hint if kind == "idle" else "Pick something else on your phone" if kind in ("ended", "offline", "error") else ""
        extra = {}
        if kind == "idle":
            try:
                extra = {"now": self._clock(), "favs": self._favs()}
            except Exception:
                extra = {}
        try:
            self._write(ASS_FILE, ass_file(screen(kind, self.detail, hint, dots, **extra)))
        except OSError:
            return
        if self._sub:
            self._ipc(SOCK, ["sub-reload"])
        else:
            r = self._ipc(SOCK, ["sub-add", ASS_FILE, "select"])
            self._sub = bool(r and r.get("error") == "success")

    def _animate(self, gen, kind):
        """Loading: move the dots. Ready: keep the clock right."""
        dots, deadline = 0, time.time() + 90
        wait = self.idle_interval if kind == "idle" else self.interval
        while kind == "idle" or time.time() < deadline:
            # idle: wake just after each whole second so the colon blinks in step with the real clock
            threading.Event().wait(wait if kind != "idle" or wait < 1 else (1.0 - time.time() % 1.0) + 0.02)
            if gen != self._gen:
                return
            dots += 1
            self._draw(dots)
