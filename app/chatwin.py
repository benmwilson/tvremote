"""The windows around the stream while chat is on the TV: a chat window in the strip to the right of the picture and a "now playing" bar under it.
The stream is shrunk to make room (video-margin-ratio-right, picture aligned to the top). Each is a small mpv window of its own, so drawing them
never touches the stream's player: drawing inside the stream's own window flashed and could not be sized reliably. Like the home screen
(shell.py) a window plays a plain colour and shows its text as a subtitle file; the desktop (labwc) places it by its app id
(config/labwc-rc.xml, made by tools/make_labwc_rules.py)."""
import os
import subprocess
import time

import mpvipc

BG = "0b0e14"
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def ass_file(text, w, h):
    """One subtitle file that shows every piece of `text` (events separated by newlines) from the start for a long time."""
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 2\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,DejaVu Sans,26,&H00F6F1EE,&H00F6F1EE,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    return head + "".join(f"Dialogue: 0,0:00:00.00,9:00:00.00,Default,,0,0,0,,{piece}\n" for piece in text.split("\n"))


class Panel:
    """One small window: app_id places it, (w, h) is its size, name keeps its socket and files apart from the others."""

    def __init__(self, name, app_id, w, h, bg=None, spawn=subprocess.Popen, ipc=mpvipc.send, write=None, ready=None, sleep=time.sleep):
        self.app_id, self.w, self.h = app_id, w, h
        self.bg = bg  # a picture to show behind the text (drawn once; a moving source re-renders the text on every frame)
        self.sock, self.log = f"/tmp/tvremote-{name}.sock", f"/tmp/tvremote-{name}.log"
        self.files = (f"/tmp/tvremote-{name}-a.ass", f"/tmp/tvremote-{name}-b.ass")  # alternate, so the new one loads before the old goes
        self._spawn, self._ipc, self._sleep = spawn, ipc, sleep
        self._write = write or self._write_file
        self._ready = ready or (lambda: os.path.exists(self.sock))
        self.proc, self._n, self._sid, self.last = None, 0, None, None

    @staticmethod
    def _write_file(path, data):
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)

    def running(self):
        return bool(self.proc and self.proc.poll() is None)

    def cmd(self):
        return ["mpv", "--no-config", "--vo=wlshm", "--hwdec=no", "--fs=no", "--no-border", f"--geometry={self.w}x{self.h}", "--force-window=yes",
                "--keep-open=yes", "--pause=no", "--no-audio", "--no-terminal", "--osc=no", "--osd-level=0", "--cursor-autohide=always",
                "--input-default-bindings=no", "--input-vo-keyboard=no", f"--wayland-app-id={self.app_id}", "--title=TV Remote",
                f"--input-ipc-server={self.sock}", f"--background-color=#{BG}",
                *(["--image-display-duration=inf", self.bg] if self.bg and os.path.exists(self.bg) else [f"av://lavfi:color=c=0x{BG}:s={self.w}x{self.h}:r=1"])]

    def open(self, tries=24):
        """Start the window, waiting up to tries x 0.25 s for it. True when it is up and answering."""
        if self.running():
            return True
        try:
            os.unlink(self.sock)
        except OSError:
            pass
        try:
            out = open(self.log, "a")
        except OSError:
            out = subprocess.DEVNULL
        self.proc, self._sid, self.last = self._spawn(self.cmd(), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT), None, None
        for _ in range(tries):  # for the window and its control socket
            if self.proc.poll() is not None or self._ready():
                break
            self._sleep(0.25)
        return self.running() and self._ready()

    def update(self, text):
        """Show `text` (ASS events separated by newlines). The new subtitle is loaded and selected before the old one is dropped.
        Nothing happens when it is what is already showing. True when mpv has it."""
        if text == self.last:
            return True
        path = self.files[self._n % 2]
        self._n += 1
        try:
            self._write(path, ass_file(text, self.w, self.h))
        except OSError:
            return False
        r = self._ipc(self.sock, ["sub-add", path, "select"])
        if not (r and r.get("error") == "success"):
            return False
        old = self._sid
        sid = self._ipc(self.sock, ["get_property", "sid"])
        self._sid = sid.get("data") if sid and sid.get("error") == "success" else None
        if old is not None and old != self._sid:
            self._ipc(self.sock, ["sub-remove", old])
        self.last = text
        return True

    def overlay(self, image):
        """Lay a picture over the window: image = (BGRA bytes with premultiplied alpha, width, height, x, y), or None to take it away. The new picture
        replaces the old one in a single step (mpv keeps an overlay under its id). True when mpv has it."""
        if image is None:
            r = self._ipc(self.sock, ["overlay-remove", 1])
            return bool(r and r.get("error") in ("success", "property not found"))
        data, w, h, x, y = image
        path = self.files[self._n % 2].replace(".ass", ".bgra")
        self._n += 1
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError:
            return False
        r = self._ipc(self.sock, ["overlay-add", 1, x, y, path, 0, "bgra", w, h, w * 4])
        return bool(r and r.get("error") == "success")

    def close(self):
        p, self.proc, self._sid, self.last = self.proc, None, None, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(3)
            except Exception:
                p.kill()


def chat_panel(**kw):
    return Panel("chat", "tvr-chat", 480, 1080, bg=os.path.join(ASSETS, "chat-bg-strip.png"), **kw)


def transition_panel(**kw):
    return Panel("transition", "tvr-transition", 1920, 1080, bg=os.path.join(ASSETS, "bg-full.png"), **kw)


def bar_panel(**kw):
    return Panel("chatbar", "tvr-chatbar", 1440, 270, bg=os.path.join(ASSETS, "chat-bg-bar.png"), **kw)
