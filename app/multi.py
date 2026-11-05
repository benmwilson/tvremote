"""Several streams on the Pi's screen at once (up to four): one mpv window per stream.

Wayland lets the desktop, not mpv, place windows. So every layout, count and tile has its own window name (tvr-grid4-2)
and config/labwc-rc.xml (made by tools/make_labwc_rules.py from the table below) says where that name goes. Adding or
removing a stream changes the geometry of the others, so all the windows restart, one after another: starting four
hardware decoders in the same instant made one of them stall on the Pi (staggering by ~3 s did not).

Tile 0 is the main player (app.py owns it: its volume, pause and status keep working). The others are started here."""
import os
import re
import signal
import subprocess
import threading
import time
from xml.sax.saxutils import quoteattr

import mpvipc

W, H = 1920, 1080
M, G, S = 24, 16, 36  # screen margin, gap between tiles, strip under each tile (its name is drawn there, on the background)
MAX_TILES = 4
LAYOUTS = ("grid", "focus")
STAGGER = float(os.environ.get("TVREMOTE_WALL_STAGGER", "0"))  # seconds between windows starting: 0 = all together (so the streams join in step)


def _row(n, w, h, y):
    x0 = (W - (n * w + (n - 1) * G)) // 2
    return [(x0 + i * (w + G), y, w, h) for i in range(n)]


def tiles(layout, n):
    """One (x, y, w, h) per tile, tile 0 first (the big one in focus). n is 2..MAX_TILES."""
    if layout == "grid":
        if n == 2:
            w = (W - 2 * M - G) // 2
            h = w * 9 // 16
            return _row(2, w, h, (H - (h + S)) // 2)
        w, h = 840, 472  # the size that was tested with four at once
        return _row(2, w, h, M) + _row(n - 2, w, h, M + h + S + G)
    hs = 270
    hb = H - 2 * M - G - 2 * S - hs
    return _row(1, hb * 16 // 9, hb, M) + _row(n - 1, hs * 16 // 9, hs, M + hb + S + G)


def window_id(layout, n, i):
    return f"tvr-{layout}{n}-{i + 1}"


def window_args(layout, n, i):
    x, y, w, h = tiles(layout, n)[i]
    return ["--fs=no", "--no-border", f"--wayland-app-id={window_id(layout, n, i)}", f"--geometry={w}x{h}"], w


def cap(player, width):
    """The stream quality a window of this width needs (the decoder's budget is shared by all the windows)."""
    big = width >= 1000
    if player == "streamlink":
        return "720p60,720p,480p,360p,worst" if big else "480p,360p,160p,worst"
    return "720" if big else "480"


QUALITIES = ("1080", "720", "480", "360")  # what a stream's picker offers, besides Auto (the window's size decides)


def quality_for(player, label):
    """The stream quality for a picked label, else None (Auto)."""
    if label == "1080":  # the decoder's budget is shared: one 1080p60 stream is about all it can do, with others at lower quality
        return "1080p60,1080p,720p60,720p,480p,worst" if player == "streamlink" else "1080"
    if label == "720":
        return "720p60,720p,480p,360p,worst" if player == "streamlink" else "720"
    if label == "480":
        return "480p,360p,160p,worst" if player == "streamlink" else "480"
    if label == "360":
        return "360p,160p,worst" if player == "streamlink" else "480"  # yt-dlp is limited to 1080/720/480 elsewhere
    return None


def friendly(raw):
    """Plain words for why a window's player stopped (what mpv or streamlink last said is not for a person)."""
    if not raw or re.search(r"No playable streams|offline|Closing currently open stream|End of file|Exiting|404|Not Found", raw, re.I):
        return "Went offline"
    return raw[:100]


def rules_xml():
    """The desktop (labwc) window rules for every layout, count and tile."""
    rules = []
    for layout in LAYOUTS:
        for n in range(2, MAX_TILES + 1):
            for i, (x, y, w, h) in enumerate(tiles(layout, n)):
                rules.append(f'    <windowRule identifier={quoteattr(window_id(layout, n, i))}><serverDecoration>no</serverDecoration>'
                             f'<action name="MoveTo" x="{x}" y="{y}"/><action name="ResizeTo" width="{w}" height="{h}"/></windowRule>')
    return '<?xml version="1.0"?>\n<labwc_config>\n  <windowRules>\n' + "\n".join(rules) + "\n  </windowRules>\n</labwc_config>\n"


def _sock(i):
    return f"/tmp/tvremote-mpv-t{i}.sock"


class Wall:
    """build(url, quality, sock, extra) -> (cmd, quality); resolve(url) -> (source, player); name(url) -> a short label."""

    def __init__(self, build, resolve, name, log_dir="/tmp", volume=lambda: None):
        self.build, self.resolve, self.name, self.log_dir, self.volume = build, resolve, name, log_dir, volume
        self.on_change = lambda: None  # called when a window ends by itself (a stream went offline), so the screen can say so
        self.layout = None
        self.extras = []  # tiles 1..: {"url", "source", "proc", "error"}
        self.main_url = None  # the stream the main window should play: what the wall says. The window catches up (a swap is changed in place)
        self.selected = 0  # the stream the remote's card controls (title, pause, favourite, quality): not tied to any window's place
        self.sound = {0}  # the tiles with sound on: the main stream only, to start with. Any number can be switched on
        self.main_quality = None  # the main tile's picked label (None = Auto)
        self.saved_quality = None  # what the main stream was set to before the wall, put back when it ends
        self.lock = threading.RLock()
        self._gen = 0  # a newer start makes an older, still-staggering one stop

    def count(self):
        return 1 + len(self.extras)

    def active(self):
        return self.layout is not None and bool(self.extras)

    def main_args(self):
        """(mpv args, width) for the main player's window while the wall is up, else ([], None)."""
        if not self.active():
            return [], None
        args, width = window_args(self.layout, self.count(), 0)
        return args + ([] if 0 in self.sound else ["--mute=yes"]), width  # a new window starts with sound: not when it is switched off

    def main_wid(self):
        """The window name the main player should have right now (None when it is full screen)."""
        return window_id(self.layout, self.count(), 0) if self.active() else None

    def sock(self, i, main_sock):
        return main_sock if i == 0 else _sock(i)

    # ---- the extra windows ----
    def _kill(self, t):
        p, t["proc"] = t.get("proc"), None  # first, so the watcher knows this end was on purpose
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(3)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def stop_windows(self):
        """End the extra windows' players, keep the list."""
        with self.lock:
            self._gen += 1
            for t in self.extras:
                self._kill(t)

    def clear(self):
        with self.lock:
            self.stop_windows()
            self.extras, self.layout, self.sound, self.main_quality, self.selected, self.main_url = [], None, {0}, None, 0, None

    def _current(self, i, t):
        p = t.get("proc")
        return bool(p and p.poll() is None and t.get("started") == t["url"] and t.get("q_started") == t.get("quality") and t.get("wid") == window_id(self.layout, self.count(), i))

    def _command(self, i, t):
        """(command, source, player) for tile i's window. Slow (asking streamlink for the stream's address): never call it under the lock."""
        source, player = self.resolve(t["url"])
        args, width = window_args(self.layout, self.count(), i)
        extra = args + ([] if i in self.sound else ["--mute=yes"]) + ["--no-terminal"]
        if self.volume() is not None:
            extra.append(f"--volume={float(self.volume()):g}")  # the level the main stream has, so switching a tile on is not a jump
        cmd, _q = self.build(t["url"], quality_for(player, t.get("quality")) or cap(player, width), _sock(i), extra)
        return cmd, source, player

    def _launch(self, i, t, cmd, source, player):
        try:
            os.unlink(_sock(i))
        except OSError:
            pass
        log = open(os.path.join(self.log_dir, f"tvremote-tile{i}.log"), "w")
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        # hot: mpv plays the stream's own address, so the window can change stream in place (loadfile) instead of being restarted
        t.update(source=source, proc=proc, error=None, wid=window_id(self.layout, self.count(), i), started=t["url"], q_started=t.get("quality"),
                 hot=cmd[0] == "mpv" and player == "streamlink")
        threading.Thread(target=self._watch, args=(i, t, proc), daemon=True).start()

    def _watch(self, i, t, proc):
        proc.wait()
        if t.get("proc") is proc:  # not replaced or stopped on purpose
            t["proc"] = None
            try:
                with open(os.path.join(self.log_dir, f"tvremote-tile{i}.log"), errors="replace") as f:
                    lines = [l for l in f.read().splitlines() if l.strip()]
            except OSError:
                lines = []
            t["error"] = friendly(re.sub(r"^\[[^\]]*\]\s*(ERROR:\s*)?", "", lines[-1]) if lines else None)
            self.on_change()

    def start_windows(self, pause=STAGGER):
        """(Re)start the extra windows that need it, each in its own thread so they all begin together (or `pause` seconds apart). A
        window that is already playing the right stream in the right place is left alone."""
        with self.lock:
            self._gen += 1
            gen = self._gen
            wanted = [t for i, t in enumerate(self.extras, start=1) if not self._current(i, t)]

        def one(k, t):
            if k and pause:
                time.sleep(pause * k)
            with self.lock:
                if gen != self._gen or t not in self.extras:
                    return
                self._kill(t)
                i = self.extras.index(t) + 1
            try:
                made = self._command(i, t)  # outside the lock: it can take seconds
            except Exception as e:
                t["error"] = str(e)[:120]
                return
            with self.lock:
                if gen != self._gen or t not in self.extras:
                    return
                self._launch(self.extras.index(t) + 1, t, *made)
        for k, t in enumerate(wanted):
            threading.Thread(target=one, args=(k, t), daemon=True).start()

    # ---- sound ----
    def set_sound(self, i, on, main_sock):
        """Switch tile i's sound on or off. The others are left as they are: any number of streams can be heard at once."""
        with self.lock:
            if not 0 <= i < self.count():
                return
            (self.sound.add if on else self.sound.discard)(i)
            mpvipc.send(self.sock(i, main_sock), ["set_property", "mute", not on])

    # ---- the tile list ----
    def add(self, url, main_url):
        """True when added. A link already on the wall (or one too many) is refused."""
        with self.lock:
            if not self.extras or self.main_url is None:
                self.main_url = main_url  # the wall starts from what is playing now
            urls = [self.main_url] + [t["url"] for t in self.extras]
            if url in urls or self.count() >= MAX_TILES:
                return False
            self.extras.append({"url": url, "source": self.resolve(url)[0], "proc": None, "error": None})
            self.layout = self.layout or "grid"
            return True

    def remove(self, i):
        """Take extra tile i (1..) off the wall."""
        with self.lock:
            if 1 <= i <= len(self.extras):
                self._kill(self.extras.pop(i - 1))
                self.sound = {k if k < i else k - 1 for k in self.sound if k != i}  # the tiles after it move up one
                self.selected = 0 if self.selected == i else self.selected - 1 if self.selected > i else self.selected
            if not self.extras:
                self.layout = None

    def select(self, i):
        """The card controls tile i from now on. No window changes."""
        with self.lock:
            if 0 <= i < self.count():
                self.selected = i

    def retry(self, i):
        """Try an extra window again (its stream may be back): forget the failure, then start_windows() restarts it."""
        with self.lock:
            if 1 <= i <= len(self.extras):
                self.extras[i - 1].update(error=None, proc=None)

    def swap(self, i, main_url=None):
        """Put extra tile i's stream on the big tile, and the big tile's stream on tile i. Works from the wall's own record of the main stream
        (not from what the main window is playing right now, which lags), so quick repeated taps can never copy one stream into two tiles.
        Returns the new main url, or None."""
        with self.lock:
            if self.main_url is None:
                self.main_url = main_url
            if not 1 <= i <= len(self.extras) or not self.main_url:
                return None
            t = self.extras[i - 1]
            t["url"], self.main_url = self.main_url, t["url"]
            t["quality"], self.main_quality = self.main_quality, t.get("quality")  # a picked quality goes with its stream
            self.selected = i if self.selected == 0 else 0 if self.selected == i else self.selected  # the selection follows the stream
            a, b = 0 in self.sound, i in self.sound  # and so does the sound
            self.sound = (self.sound - {0, i}) | ({i} if a else set()) | ({0} if b else set())
            t["source"] = self.resolve(t["url"])[0]
            return self.main_url

    def status(self):
        if not self.active():
            return None
        with self.lock:
            tiles_ = [{"i": i, "url": t["url"], "name": self.name(t["url"]), "source": t.get("source"),
                       "running": bool(t.get("proc") and t["proc"].poll() is None), "error": t.get("error"),
                       "quality": t.get("quality")}
                      for i, t in enumerate(self.extras, start=1)]
            return {"layout": self.layout, "sound": sorted(self.sound), "selected": self.selected, "tiles": tiles_, "max": MAX_TILES, "main_quality": self.main_quality}
