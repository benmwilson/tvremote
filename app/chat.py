"""Twitch chat drawn over the stream on the Pi's screen (mpv osd-overlay).
Anonymous IRC over TLS: no login and no extra dependency. Twitch only for now.
shortcut: Kick and YouTube chat are not supported; emotes show as their text names."""
import json
import random
import re
import textwrap
import socket
import ssl
import threading
import time
from collections import deque
from urllib.request import Request, urlopen

import chatimg

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


HISTORY = "https://recent-messages.robotty.de/api/v2/recent-messages/{}?limit={}"  # the same public service other Twitch clients use for history


def recent_lines(channel, limit=40):
    """The channel's last chat messages as the raw IRC lines (oldest first), so a chat that opens is not empty until someone types. Twitch's own chat
    connection only delivers what is said after it joins. [] when the service is down or slow: the chat then just starts live."""
    try:
        req = Request(HISTORY.format(channel, limit), headers={"User-Agent": "tvremote/1.0"})
        return [l for l in json.loads(urlopen(req, timeout=5).read()).get("messages", []) if " PRIVMSG " in l][-limit:]
    except Exception:
        return []


def read_lines(channel, stopped, on_line):
    """Anonymous Twitch IRC: calls on_line(line) for every line until `stopped` is set; reconnects after a pause.
    The channel's recent history is delivered first, once."""
    for line in recent_lines(channel):
        if stopped.is_set():
            return
        on_line(line)
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
    for name, colour, text, *_ in msgs:
        rgb = colour[1:] if re.fullmatch(r"#[0-9A-Fa-f]{6}", colour) else "FFFFFF"
        bgr = rgb[4:6] + rgb[2:4] + rgb[0:2]  # ASS colours are BGR
        rows.append("{\\c&H" + bgr + "&\\b1}" + _esc(name) + "{\\r}: " + _esc(text))
    return "{\\an1\\fs34\\bord3\\shad0\\q2}" + "\\N".join(rows)


COL_REDRAW_S = 2      # the chat window is its own surface, so a redraw never touches the stream
COL_W, COL_H = 480, 1080  # the strip the picture makes room for (the picture is 1440 wide), drawn as a window of its own
COL_CHARS, COL_LINES = 24, 30
IMG_X, IMG_Y, IMG_H = 28, 118, 932  # where the messages picture goes in the strip: below the header rule, down to the bottom margin


def _plain(s):
    """Drop emoji and symbols the screen's font draws as empty boxes."""
    return re.sub("[\U00010000-\U0010ffff\u200d\ufe0f\u2600-\u27bf]", "", s)


SITE_BGR = {"Kick": "18FC53", "Twitch": "FF70A9"}  # the sites' colours, as ASS wants them (BGR)


def ass_column(channel, msgs, source="Twitch"):
    """The chat as ASS for the 480x1080 chat window: background, a header, then the newest messages growing up from the bottom, wrapped to fit."""
    accent = SITE_BGR.get(source, "FFB062")
    lines = []
    for name, colour, text, *_ in msgs:
        rgb = colour[1:] if re.fullmatch(r"#[0-9A-Fa-f]{6}", colour) else "FFFFFF"
        bgr = rgb[4:6] + rgb[2:4] + rgb[0:2]
        who = _esc(name)
        wrapped = textwrap.wrap(who + ": " + _plain(_esc(text)), COL_CHARS, break_long_words=True) or [who]
        if wrapped[0].startswith(who + ": "):
            wrapped[0] = "{\\c&H" + bgr + "&\\b1}" + who + "{\\r}: " + wrapped[0][len(who) + 2:]
        lines += wrapped
        lines.append("{\\fs10} {\\fs26}")  # a little air between messages (and the size back, or the next name stays tiny)
    head = "{\\an7\\pos(28,44)\\fs28\\b1\\bord0\\shad0\\1c&H" + accent + "&}\u25cf  {\\1c&HA9978C&\\fsp3}CHAT  {\\1c&HF6F1EE&\\fsp0}" + _esc(channel).upper()
    body = "{\\an1\\pos(28,1050)\\fs26\\bord2\\shad0\\q2}" + "\\N".join(lines[-COL_LINES:])
    rule = r"{\an7\pos(28,100)\p1\bord0\shad0\1c&H%s&\1a&H60&}m 0 0 l 424 0 424 2 0 2{\p0}" % accent
    return "\n".join([rule, head, body])  # the glows are in the window's background picture (tools/make_chat_bg.py)


BAR_X = 48


def ass_bar(name, title, source="Twitch"):
    """The 'now playing' bar under the picture (a 1440x270 window): the site's dot, the channel, and the stream's title, on the same background."""
    accent = SITE_BGR.get(source, "FFB062")
    out = []  # the glows are in the window's background picture (tools/make_chat_bg.py)
    out.append((r"{\an7\pos(%d,38)\fs22\b1\fsp5\bord0\shad0\1c&H%s&}" % (BAR_X, accent)) + "\u25cf" + r"  {\1c&HA9978C&}NOW PLAYING")
    out.append(r"{\an7\pos(%d,78)\fs60\b1\bord0\shad0\1c&HF6F1EE&}%s" % (BAR_X, _esc(_plain(name))[:28]))
    if title:
        out.append(r"{\an7\pos(%d,160)\fs30\b0\bord0\shad0\1c&HA9978C&}%s" % (BAR_X, _esc(_plain(title))[:70]))
    return "\n".join(out)


class TwitchChat:
    SOURCE = "Twitch"
    """send(ass_text or None) draws or clears the overlay and returns True when mpv accepted it.
    column=True: a chat column that stays up (the player has made room for it), else short-lived lines over the picture."""

    def __init__(self, channel, send, column=False):
        self.channel, self.send, self.column = channel, send, column
        self.msgs = deque(maxlen=COL_LINES if column else MAX_LINES)  # (arrival time, (name, colour, text, Twitch's own emotes))
        self.stopped = threading.Event()
        # Emotes in the column: drawn as a picture over the window (chatimg) when Pillow is there.
        self.pics = chatimg.Pictures() if column and chatimg.available() else None
        self.words = {}  # third-party emote name -> picture address, filled in the background
        if self.pics:
            threading.Thread(target=self._load_emotes, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._draw, daemon=True).start()

    def stop(self):
        self.stopped.set()
        self.send(None)

    def _load_emotes(self):
        try:
            import emotes
            words = {}
            for s in emotes.for_channel(self.channel):
                if s["provider"] != "twitch":  # Twitch's own come with each message
                    for e in s["emotes"]:
                        words.setdefault(e["n"], e["u"])  # the channel's sets come first, so they win over the global ones
            self.words = words
        except Exception:
            pass

    def _read(self):
        def on_line(line):
            if (m := parse_full(line)):
                self.msgs.append((time.time(), (m["name"], m["color"], m["text"], m["emotes"])))
        read_lines(self.channel, self.stopped, on_line)

    def _draw(self):
        last = None
        while not self.stopped.wait(COL_REDRAW_S if self.column else 1):  # every redraw of the overlay flashes on this video output, so the column redraws rarely
            now = time.time()
            live = [m for t, m in list(self.msgs) if self.column or now - t < SHOW_S]
            if self.pics:  # the header is a subtitle; the messages are a picture with the emotes in it. Drawn again only when something changed
                sig = (tuple(live), self.pics.version, len(self.words))
                if sig != last:
                    img = chatimg.render(live, self.pics, COL_W - 2 * IMG_X, IMG_H, self.words)
                    if self.send(ass_column(self.channel, [], self.SOURCE), (img[0], img[1], img[2], IMG_X, IMG_Y) if img else None):
                        last = sig
                continue
            text = ass_column(self.channel, live, self.SOURCE) if self.column else ass(live) if live else None
            if text != last and self.send(text):  # only remember it once mpv accepted it
                last = text


KICK_API = "https://kick.com/api/v2/channels/"
KICK_POLL_S = 2


def _kick_get(path):
    """JSON from Kick's own site API, the way a browser asks (Kick refuses plain scripts; keep it gentle: one small request every few seconds)."""
    import json
    import urllib.request
    req = urllib.request.Request(KICK_API + path, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


KICK_EMOTE = re.compile(r"\[emote:(\d+):([^\]]+)\]")


def kick_text(content):
    """Kick's message text with each [emote:id:name] written as its name, and where those names are: (text, [{u: picture address, s, e}])."""
    out, emotes, at = [], [], 0
    pos = 0
    for m in KICK_EMOTE.finditer(content or ""):
        out.append(content[at:m.start()])
        pos += m.start() - at
        emotes.append({"u": f"https://files.kick.com/emotes/{m.group(1)}/fullsize", "s": pos, "e": pos + len(m.group(2)) - 1})
        out.append(m.group(2))
        pos += len(m.group(2))
        at = m.end()
    out.append((content or "")[at:])
    return "".join(out), emotes


def kick_parse(m):
    """(name, colour, text, emotes) from one of Kick's message objects, emotes as their names (and where they are); None for anything but a plain message."""
    if m.get("type") != "message":
        return None
    who = m.get("sender") or {}
    text, emotes = kick_text(m.get("content"))
    return ((who.get("username") or "?"), ((who.get("identity") or {}).get("color") or ""), text, emotes) if text.strip() else None


class KickFeed(ChatFeed):
    """Kick's last messages for the phone's chat view, from the same recent-messages endpoint as the TV column (see KickChat). A message is
    {name, color, text, emotes: [{u: picture address, s, e}], id}; stops by itself when nobody has asked for a minute."""

    def __init__(self, channel, get=_kick_get):
        self._get = get
        super().__init__(channel, autostart=False)
        threading.Thread(target=self._poll, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _poll(self):
        seen, room = set(), None
        while not self.stopped.is_set():
            try:
                if room is None:
                    room = self._get(self.channel)["id"]
                msgs = (self._get(f"{room}/messages?_={int(time.time() * 1000)}").get("data") or {}).get("messages") or []
                for m in sorted(msgs, key=lambda x: x.get("created_at", "")):  # oldest first: the first poll is the channel's recent history
                    if m.get("id") not in seen:
                        seen.add(m.get("id"))
                        if (p := kick_parse(m)):
                            self.n += 1
                            self.msgs.append({"name": p[0], "color": p[1], "text": p[2], "emotes": p[3], "id": self.n})
                seen &= {m.get("id") for m in msgs}
            except Exception:
                room = None
            self.stopped.wait(KICK_POLL_S)


class KickChat(TwitchChat):
    """Kick's chat for the TV column, from its recent-messages endpoint every few seconds (Kick's own websocket is closed to scripts).
    shortcut: unofficial, can change or block without notice; the phone's chat view still works either way."""
    SOURCE = "Kick"

    def __init__(self, channel, send, column=False, get=_kick_get):
        self._get = get
        super().__init__(channel, send, column)

    def _load_emotes(self):  # Kick's own come with each message; the channel's 7TV set and 7TV's global one are looked up by the streamer's id
        try:
            import emotes
            j = self._get(self.channel)
            uid = j.get("user_id") or (j.get("user") or {}).get("id")
            self.words = {e["n"]: e["u"] for s in emotes.for_kick(uid) for e in reversed(s["emotes"])}  # (reversed, so the first of a name wins)
        except Exception:
            pass

    def _read(self):
        seen, room = set(), None
        while not self.stopped.is_set():
            try:
                if room is None:
                    room = self._get(self.channel)["id"]
                msgs = (self._get(f"{room}/messages?_={int(time.time() * 1000)}").get("data") or {}).get("messages") or []  # the changing value keeps Kick's cache from answering with old messages
                for m in sorted(msgs, key=lambda x: x.get("created_at", "")):  # oldest first, so the newest ends up at the bottom
                    if m.get("id") not in seen:
                        seen.add(m.get("id"))
                        if (p := kick_parse(m)):
                            self.msgs.append((time.time(), p))
                seen &= {m.get("id") for m in msgs}  # only what Kick still sends, so the set can't grow for hours
            except Exception:
                room = None
            self.stopped.wait(KICK_POLL_S)


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
