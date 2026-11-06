"""Twitch for the phone, without the ads. Twitch stitches its ads into the stream's playlist. The Pi's own player filters them out inside
streamlink; a phone that is handed the stream's address would play them. So the phone is handed this relay's playlist instead: the Pi
reads Twitch's playlist, drops the ad segments (using streamlink's own ad detection, so it follows whatever streamlink learns about Twitch),
and serves the rest. The pictures themselves still come straight from Twitch's servers to the phone.
During an ad the playlist simply doesn't grow, so the phone waits a moment and picks up where the real stream resumes."""
import math
import secrets
import threading
import time
from collections import deque

WINDOW = 5  # segments in the playlist we serve (Twitch's are about 2 s each)
SESSION_TTL = 3600  # a session nobody has asked for in an hour goes away
MAX_SESSIONS = 6
_sessions = {}  # token -> Relay
_lock = threading.Lock()


def _parse(text, base):
    import inproc
    inproc._load()  # yt-dlp first, then streamlink (see inproc._load)
    from streamlink.plugins.twitch import TwitchM3U8Parser
    from streamlink.stream.hls import parse_m3u8
    return parse_m3u8(text, base_uri=base, parser=TwitchM3U8Parser)


class Relay:
    def __init__(self, upstream, fetch, resolve=None):
        """upstream: the stream's media playlist address. fetch(url) -> bytes. resolve() -> a fresh address when the old one stops working."""
        self.upstream, self.fetch, self.resolve = upstream, fetch, resolve
        self.kept = deque(maxlen=WINDOW)  # (sequence number, address, duration, discontinuity before it)
        self.seen = deque(maxlen=300)
        self.next_seq, self.gap, self.used = 0, False, time.time()
        self.lock = threading.Lock()

    def _text(self):
        try:
            return self.fetch(self.upstream).decode(errors="replace")
        except Exception:
            if not self.resolve:
                raise
            self.upstream = self.resolve()  # the signed address expired: ask for a new one once
            return self.fetch(self.upstream).decode(errors="replace")

    def playlist(self):
        with self.lock:
            self.used = time.time()
            pl = _parse(self._text(), self.upstream)
            for seg in pl.segments:
                if getattr(seg, "prefetch", False):
                    continue  # not ready yet, and its ad state is a guess
                if seg.ad:
                    self.gap = True  # the stream carries on after the ad: tell the player the picture jumps
                    continue
                if seg.uri in self.seen:
                    continue
                self.seen.append(seg.uri)
                self.kept.append((self.next_seq, seg.uri, float(seg.duration or 2), self.gap))
                self.next_seq += 1
                self.gap = False
            return self.render()

    def render(self):
        segs = list(self.kept)
        target = math.ceil(max([d for _, _, d, _ in segs] or [2]))
        out = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}", f"#EXT-X-MEDIA-SEQUENCE:{segs[0][0] if segs else self.next_seq}"]
        for _, uri, dur, disc in segs:
            if disc:
                out.append("#EXT-X-DISCONTINUITY")
            out += [f"#EXTINF:{dur:.3f},", uri]
        return "\n".join(out) + "\n"


def create(upstream, fetch, resolve=None):
    """A new relay for a stream; answers its token (the playlist is at /watch/<token>.m3u8)."""
    now = time.time()
    with _lock:
        for t in [t for t, r in _sessions.items() if now - r.used > SESSION_TTL]:
            del _sessions[t]
        while len(_sessions) >= MAX_SESSIONS:
            del _sessions[min(_sessions, key=lambda t: _sessions[t].used)]
        token = secrets.token_urlsafe(16)
        _sessions[token] = Relay(upstream, fetch, resolve)
    return token


def get(token):
    with _lock:
        return _sessions.get(token)
