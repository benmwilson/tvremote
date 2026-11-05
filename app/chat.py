"""Twitch chat drawn over the stream on the Pi's screen (mpv osd-overlay).
Anonymous IRC over TLS: no login and no extra dependency. Twitch only for now.
shortcut: Kick and YouTube chat are not supported; emotes show as their text names."""
import random
import re
import socket
import ssl
import threading
import time
from collections import deque

HOST, PORT = "irc.chat.twitch.tv", 6697
SHOW_S = 25      # a message stays on screen this long
MAX_LINES = 7
MAX_CHARS = 90   # long lines would run off the screen (no wrapping)


def channel_of(url):
    """Twitch channel name from a live-channel URL, else None (VODs, clips and the directory have no live chat)."""
    parts = [p for p in re.sub(r"^https?://[^/]+", "", url).split("?")[0].split("/") if p]
    return parts[0].lower() if len(parts) == 1 and re.fullmatch(r"\w{3,25}", parts[0]) and parts[0] != "directory" else None


def parse_full(line):
    """{name, color, text, emotes: [{id, s, e}]} for a chat message line, else None."""
    m = re.match(r"^(?:@(\S+) )?:(\w+)!\S+ PRIVMSG #\S+ :(.*)$", line)
    if not m:
        return None
    tags = dict(t.split("=", 1) for t in (m.group(1) or "").split(";") if "=" in t)
    text = re.sub(r"^\x01ACTION (.*?)\x01?$", r"\1", m.group(3))
    emotes = []
    for part in filter(None, tags.get("emotes", "").split("/")):  # "25:0-4,12-16/1902:6-10"
        eid, _, spans = part.partition(":")
        for span in spans.split(","):
            a, _, b = span.partition("-")
            if re.fullmatch(r"[\w-]+", eid) and a.isdigit() and b.isdigit():
                emotes.append({"id": eid, "s": int(a), "e": int(b)})
    return {"name": tags.get("display-name") or m.group(2), "color": tags.get("color", ""), "text": text,
            "emotes": sorted(emotes, key=lambda x: x["s"])}


def parse(line):
    """(name, colour, text) for a chat message line, else None."""
    m = parse_full(line)
    return (m["name"], m["color"], m["text"]) if m else None


def read_lines(channel, stopped, on_line):
    """Anonymous Twitch IRC: calls on_line(line) for every line until `stopped` is set; reconnects after a pause."""
    while not stopped.is_set():
        try:
            raw = socket.create_connection((HOST, PORT), timeout=15)
            s = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
            s.settimeout(330)  # Twitch pings about every 5 minutes
            for line in ("CAP REQ :twitch.tv/tags", f"NICK justinfan{random.randint(10000, 99999)}", f"JOIN #{channel}"):
                s.sendall((line + "\r\n").encode())
            buf = b""
            while not stopped.is_set():
                data = s.recv(4096)
                if not data:
                    break
                buf += data
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    line = line.decode(errors="replace")
                    if line.startswith("PING"):
                        s.sendall(b"PONG :tmi.twitch.tv\r\n")
                    else:
                        on_line(line)
        except OSError:
            pass
        stopped.wait(5)  # reconnect after a pause


class ChatFeed:
    """The last messages of one channel for the phone's chat view. Stops by itself when nobody has asked for a minute."""
    KEEP, IDLE_S = 200, 60

    def __init__(self, channel, autostart=True):
        self.channel, self.msgs, self.n = channel, deque(maxlen=self.KEEP), 0
        self.stopped, self.touched = threading.Event(), time.time()
        if autostart:
            threading.Thread(target=read_lines, args=(channel, self.stopped, self.on_line), daemon=True).start()
            threading.Thread(target=self._watch, daemon=True).start()

    def on_line(self, line):
        m = parse_full(line)
        if m:
            self.n += 1
            self.msgs.append({**m, "id": self.n})

    def since(self, last):
        self.touched = time.time()
        return [m for m in list(self.msgs) if m["id"] > last]

    def _watch(self):
        while not self.stopped.wait(10):
            if time.time() - self.touched > self.IDLE_S:
                self.stopped.set()

    def stop(self):
        self.stopped.set()


def _esc(s):
    """ASS text: drop backslashes/braces (override-tag syntax) and control characters."""
    return re.sub(r"[\\{}\x00-\x1f]", "", s)[:MAX_CHARS]


def ass(msgs):
    """One ASS event block, bottom-left, newest at the bottom."""
    rows = []
    for name, colour, text in msgs:
        rgb = colour[1:] if re.fullmatch(r"#[0-9A-Fa-f]{6}", colour) else "FFFFFF"
        bgr = rgb[4:6] + rgb[2:4] + rgb[0:2]  # ASS colours are BGR
        rows.append("{\\c&H" + bgr + "&\\b1}" + _esc(name) + "{\\r}: " + _esc(text))
    return "{\\an1\\fs34\\bord3\\shad0\\q2}" + "\\N".join(rows)


class TwitchChat:
    """send(ass_text or None) draws or clears the overlay and returns True when mpv accepted it."""

    def __init__(self, channel, send):
        self.channel, self.send = channel, send
        self.msgs = deque(maxlen=MAX_LINES)  # (arrival time, (name, colour, text))
        self.stopped = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._draw, daemon=True).start()

    def stop(self):
        self.stopped.set()
        self.send(None)

    def _read(self):
        def on_line(line):
            if (m := parse(line)):
                self.msgs.append((time.time(), m))
        read_lines(self.channel, self.stopped, on_line)

    def _draw(self):
        last = None
        while not self.stopped.wait(1):
            now = time.time()
            live = [m for t, m in list(self.msgs) if now - t < SHOW_S]
            text = ass(live) if live else None
            if text != last and self.send(text):  # only remember it once mpv accepted it
                last = text


if __name__ == "__main__":
    assert channel_of("https://www.twitch.tv/xqc") == "xqc"
    assert channel_of("https://twitch.tv/XQC?sr=a") == "xqc"
    assert channel_of("https://www.twitch.tv/videos/123") is None and channel_of("https://www.twitch.tv/directory") is None
    line = "@badge-info=;color=#FF8000;display-name=Foo;emotes= :foo!foo@foo.tmi.twitch.tv PRIVMSG #xqc :hello {\\b1} world"
    assert parse(line) == ("Foo", "#FF8000", "hello {\\b1} world")
    assert parse(":foo!foo@foo.tmi.twitch.tv PRIVMSG #xqc :\x01ACTION waves\x01")[2] == "waves"
    assert parse(":tmi.twitch.tv 001 justinfan1 :Welcome") is None
    out = ass([parse(line), ("Bar", "", "x" * 300)])
    assert "{\\c&H0080FF&" in out and "hello b1 world" in out and out.count("\\N") == 1 and "x" * 91 not in out, out
    print("chat ok")
