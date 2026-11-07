"""Live hover preview on the TV's launcher: the TV's browser only talks to the app. The app reads the stream's media playlist, rewrites every
segment address to /tv/pv/seg, and fetches only segments the last playlist listed (public http(s) hosts only, via hlsproxy._check).
Many sessions at once (unlike hlsproxy, which serves one Chromecast). A Twitch stream goes through twitchads first (ads taken out).
shortcut: media playlists only (no master playlist, EXT-X-MAP or KEY rewriting), like hlsproxy; upgrade if a source needs them."""
import secrets
import threading
import time
from urllib.parse import quote, urljoin

import hlsproxy

MAX_SESSIONS = 2  # one preview at a time is the plan; the second covers the old one ending while a new one starts
TTL = 120  # a preview nobody has asked for in two minutes goes away
_lock = threading.Lock()
_sessions = {}  # token -> {"text": callable -> playlist text, "base": playlist address, "allowed": set, "used": time}


def create(text, base):
    """text(): the upstream media playlist as text. base: where its relative addresses are relative to. Answers the token."""
    now = time.time()
    with _lock:
        for t in [t for t, s in _sessions.items() if now - s["used"] > TTL]:
            del _sessions[t]
        while len(_sessions) >= MAX_SESSIONS:
            del _sessions[min(_sessions, key=lambda t: _sessions[t]["used"])]
        token = secrets.token_urlsafe(12)
        _sessions[token] = {"text": text, "base": base, "allowed": set(), "used": now}
    return token


def playlist(token):
    """The playlist with segment addresses pointing at /tv/pv/seg, or None for an unknown token."""
    with _lock:
        s = _sessions.get(token)
    if not s:
        return None
    s["used"] = time.time()
    out, allowed = [], set()
    for line in s["text"]().splitlines():
        if line and not line.startswith("#"):
            u = urljoin(s["base"], line)
            try:
                hlsproxy._check(u)
            except ValueError:
                continue  # a segment we refuse to fetch is left out
            allowed.add(u)
            line = f"seg?t={token}&u=" + quote(u, safe="")
        out.append(line)
    s["allowed"] = allowed
    return "\n".join(out) + "\n"


def segment(token, u):
    """Bytes of a segment the last playlist listed, else None."""
    with _lock:
        s = _sessions.get(token)
    if not s or u not in s["allowed"]:
        return None
    s["used"] = time.time()
    return hlsproxy._get(u)


def end(token):
    with _lock:
        _sessions.pop(token, None)
