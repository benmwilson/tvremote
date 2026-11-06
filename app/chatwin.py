"""The chat window on the Pi's screen: a small mpv window in the strip to the right of the stream (the stream is shrunk to make room with
video-margin-ratio-right). It is a window of its own, so drawing the chat never touches the stream's player: drawing inside the stream's
own window flashed and could not be sized reliably. Like the home screen (shell.py) it plays a plain colour and shows the chat as a subtitle
file; the desktop (labwc) places it by its app id (config/labwc-rc.xml, made by tools/make_labwc_rules.py)."""
import os
import subprocess
import time

import mpvipc

SOCK = "/tmp/tvremote-chat.sock"
APP_ID = "tvr-chat"
LOG = "/tmp/tvremote-chat.log"
W, H = 480, 1080
BG = "0b0e14"
FILES = ("/tmp/tvremote-chat-a.ass", "/tmp/tvremote-chat-b.ass")  # alternate, so the new one loads before the old goes
HEADER = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\n\n"
          "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
          "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
          "Style: Default,DejaVu Sans,26,&H00F6F1EE,&H00F6F1EE,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n\n"
          "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")


def ass_file(text):
    """One subtitle file that shows every piece of `text` (events separated by newlines) from the start for a long time."""
    return HEADER + "".join(f"Dialogue: 0,0:00:00.00,9:00:00.00,Default,,0,0,0,,{piece}\n" for piece in text.split("\n"))


class ChatWindow:
    def __init__(self, spawn=subprocess.Popen, ipc=mpvipc.send, write=None, ready=None, sleep=time.sleep):
        self._spawn, self._ipc, self._sleep = spawn, ipc, sleep
        self._write = write or self._write_file
        self._ready = ready or (lambda: os.path.exists(SOCK))
        self.proc, self._n, self._sid = None, 0, None

    @staticmethod
    def _write_file(path, data):
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)

    def running(self):
        return bool(self.proc and self.proc.poll() is None)

    def cmd(self):
        return ["mpv", "--no-config", "--vo=wlshm", "--hwdec=no", "--fs=no", "--no-border", f"--geometry={W}x{H}", "--force-window=yes",
                "--keep-open=yes", "--pause=no", "--no-audio", "--no-terminal", "--osc=no", "--osd-level=0", "--cursor-autohide=always",
                "--input-default-bindings=no", "--input-vo-keyboard=no", f"--wayland-app-id={APP_ID}", "--title=TV Remote chat",
                f"--input-ipc-server={SOCK}", f"--background-color=#{BG}", f"av://lavfi:color=c=0x{BG}:s={W}x{H}:r=2"]

    def open(self):
        """Start the window. True when it is up and answering."""
        if self.running():
            return True
        try:
            os.unlink(SOCK)
        except OSError:
            pass
        try:
            out = open(LOG, "a")
        except OSError:
            out = subprocess.DEVNULL
        self.proc, self._sid = self._spawn(self.cmd(), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT), None
        for _ in range(24):  # up to ~6 s for the window and its control socket
            if self.proc.poll() is not None or self._ready():
                break
            self._sleep(0.25)
        return self.running() and self._ready()

    def update(self, text):
        """Show `text` (ASS events separated by newlines). The new subtitle is loaded and selected before the old one is dropped. True when mpv took it."""
        path = FILES[self._n % 2]
        self._n += 1
        try:
            self._write(path, ass_file(text))
        except OSError:
            return False
        r = self._ipc(SOCK, ["sub-add", path, "select"])
        if not (r and r.get("error") == "success"):
            return False
        old = self._sid
        sid = self._ipc(SOCK, ["get_property", "sid"])
        self._sid = sid.get("data") if sid and sid.get("error") == "success" else None
        if old is not None and old != self._sid:
            self._ipc(SOCK, ["sub-remove", old])
        return True

    def close(self):
        p, self.proc, self._sid = self.proc, None, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(3)
            except Exception:
                p.kill()
