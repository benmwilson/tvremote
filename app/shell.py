"""The TV's home screen: a fullscreen window that is always there behind the streams, so the Pi desktop never shows
between them. It shows Ready, Loading, "Not live right now", "Stream ended" and error screens.

One mpv plays a plain colour; the text is drawn with osd-overlay. While a stream plays the shell is paused and silent,
so it costs the stream nothing (a redrawing overlay on top of moving video is what dropped frames for chat).
shortcut: relies on the desktop stacking a newer fullscreen window above this one; if a stream ever appears behind it,
turn the home screen off from the remote and tell me what the Pi's desktop does."""
import os
import socket
import subprocess
import threading
import time

import mpvipc
from chat import _esc as esc

SOCK = "/tmp/tvremote-shell.sock"
LOG = "/tmp/tvremote-shell.log"
W, H = 1920, 1080
BG = "0b0e14"
VO_ORDER = ("wlshm", "gpu")  # wlshm: plain software drawing, no GPU libraries needed; gpu if it won't open
# ASS colours are BGR
TEXT, DIM, ACCENT, BAD, WARN = "F6F1EE", "A9978C", "FFB062", "6B6BFF", "47B5FF"
HEADS = {"idle": ("Ready", ACCENT), "loading": ("Loading", ACCENT), "error": ("Couldn't play", BAD),
         "offline": ("Not live right now", WARN), "ended": ("Stream ended", DIM)}


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent; this just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "this Pi"


def screen(kind, detail="", hint="", dots=0):
    """ASS text for one screen, centred."""
    head, colour = HEADS[kind]
    if kind == "loading":
        n = dots % 4
        head += "." * n + r"\h" * (3 - n)  # hard spaces keep the line the same width
    lines = [r"{\fs110\b1\c&H%s&}TV Remote" % TEXT, r"{\fs60\b0\c&H%s&}%s" % (colour, head)]
    if detail:
        lines.append(r"{\fs44\c&H%s&}%s" % (TEXT, esc(detail)))
    if hint:
        lines.append(r"{\fs34\c&H%s&}%s" % (DIM, esc(hint)))
    return r"{\an5\pos(960,540)\bord0\shad0}" + r"\N".join(lines)


class Shell:
    interval = 0.6  # seconds between loading-dot frames

    def __init__(self, hint="", spawn=subprocess.Popen, ipc=mpvipc.send, ready=None, sleep=time.sleep):
        self.hint, self._spawn, self._ipc, self._sleep = hint, spawn, ipc, sleep
        self._ready = ready or (lambda: os.path.exists(SOCK))
        self.proc = self.vo = self.error = None
        self.kind, self.detail = "idle", ""
        self._gen = 0
        self._lock = threading.RLock()

    def running(self):
        return bool(self.proc and self.proc.poll() is None)

    def _cmd(self, vo):
        return ["mpv", "--no-config", f"--vo={vo}", "--hwdec=no", "--fs", "--force-window=yes", "--keep-open=yes",
                "--no-audio", "--no-terminal", "--osc=no", "--osd-level=1", "--cursor-autohide=always",
                "--input-default-bindings=no", "--input-vo-keyboard=no", "--wayland-app-id=tvremote-shell",
                "--title=TV Remote", f"--input-ipc-server={SOCK}", f"--background-color=#{BG}",
                f"av://lavfi:color=c=0x{BG}:s={W}x{H}:r=2"]

    def start(self):
        """Open the window with the first output that works. Returns whether it is running."""
        with self._lock:
            if self.running():
                return True
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

    def _show_current(self):
        kind = self.kind
        if kind == "playing":
            self._ipc(SOCK, ["set_property", "pause", True])
            self._ipc(SOCK, ["osd-overlay", 1, "none"])
            return True
        self._ipc(SOCK, ["set_property", "pause", False])
        self._draw(0)
        if kind == "loading":
            threading.Thread(target=self._animate, args=(self._gen,), daemon=True).start()
        return True

    def _draw(self, dots):
        hint = self.hint if self.kind == "idle" else "Pick something else on your phone" if self.kind in ("ended", "offline", "error") else ""
        self._ipc(SOCK, ["osd-overlay", 1, "ass-events", screen(self.kind, self.detail, hint, dots), W, H])

    def _animate(self, gen):
        dots, deadline = 0, time.time() + 90
        while time.time() < deadline:
            threading.Event().wait(self.interval)
            if gen != self._gen:
                return
            dots += 1
            self._draw(dots)
