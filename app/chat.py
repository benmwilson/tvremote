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


def parse(line):
    """(name, colour, text) for a chat message line, else None."""
    m = re.match(r"^(?:@(\S+) )?:(\w+)!\S+ PRIVMSG #\S+ :(.*)$", line)
    if not m:
        return None
    tags = dict(t.split("=", 1) for t in (m.group(1) or "").split(";") if "=" in t)
    text = re.sub(r"^\x01ACTION (.*?)\x01?$", r"\1", m.group(3))
    return tags.get("display-name") or m.group(2), tags.get("color", ""), text


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
        while not self.stopped.is_set():
            try:
                raw = socket.create_connection((HOST, PORT), timeout=15)
                s = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
                s.settimeout(330)  # Twitch pings about every 5 minutes
                for line in ("CAP REQ :twitch.tv/tags", f"NICK justinfan{random.randint(10000, 99999)}", f"JOIN #{self.channel}"):
                    s.sendall((line + "\r\n").encode())
                buf = b""
                while not self.stopped.is_set():
                    data = s.recv(4096)
                    if not data:
                        break
                    buf += data
                    while b"\r\n" in buf:
                        line, buf = buf.split(b"\r\n", 1)
                        line = line.decode(errors="replace")
                        if line.startswith("PING"):
                            s.sendall(b"PONG :tmi.twitch.tv\r\n")
                        elif (m := parse(line)):
                            self.msgs.append((time.time(), m))
            except OSError:
                pass
            self.stopped.wait(5)  # reconnect after a pause

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
