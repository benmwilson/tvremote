"""What a TV's own app is playing (title, channel, picture), for the card and the TV page.

The Roku only says which app is open, whether it plays, and where it is (ECP has no titles). Apps that publish more get a provider here:
YouTube listens to its own "Lounge" interface (the one phones use to cast) as a read-only remote and never sends a command.
Unofficial: if it breaks, nothing else does, and the card falls back to the app name and position.
Netflix and Prime Video have no known local interface; Plex would need its server's address and token (not built).

A listener only runs while somebody is looking at that TV (sync() is called from the Roku refresh) and stops 90 s after the last look."""
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid

YT_ORIGIN = "package:com.google.android.youtube"  # the Roku's DIAL only answers callers with an approved Origin; this is one it accepts
LINGER = 90  # seconds a listener keeps going after the last look at the TV

_lock = threading.Lock()
_info = {}    # tv_id -> {"app", "title", "channel", "thumb", "video"}
_state = {}   # tv_id -> {"seen": time, "stop": Event, "app": name}
_titles = {}  # video id -> (title, channel, thumb) | None


def parse_events(text):
    """The newest nowPlaying event in a chunk of the Lounge stream: {"videoId", ...} or None."""
    hits = re.findall(r'\["nowPlaying",\s*(\{[^{}]*\})\]', text or "")
    for raw in reversed(hits):
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if d.get("videoId"):
            return d
    return None


def video_details(video_id):
    """(title, channel, thumb) from YouTube's public oEmbed (no key), remembered; None when it can't be had."""
    if not re.fullmatch(r"[\w-]{6,20}", video_id or ""):
        return None
    if video_id in _titles:
        return _titles[video_id]
    try:
        url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote("https://www.youtube.com/watch?v=" + video_id, safe="")
        d = json.load(urllib.request.urlopen(url, timeout=8))
        out = (d.get("title"), d.get("author_name"), d.get("thumbnail_url"))
    except (OSError, ValueError):
        return None  # not remembered: a blip must not stick
    if len(_titles) > 200:
        _titles.clear()
    _titles[video_id] = out
    return out


def _open(url, headers=None, data=None, timeout=15):
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers or {}), timeout=timeout)


def _youtube_session(ip, stop, publish):
    """One Lounge session: find the TV's screen id, bind as a read-only remote, publish every nowPlaying until the stream ends or stop is set."""
    x = _open(f"http://{ip}:8060/dial/YouTube", {"Origin": YT_ORIGIN}).read().decode()
    screen = re.search(r"<screenId>([^<]+)", x)
    if not screen:
        return
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    tok = json.load(_open("https://www.youtube.com/api/lounge/pairing/get_lounge_token_batch", form,
                          urllib.parse.urlencode({"screen_ids": screen.group(1)}).encode()))
    lt = tok["screens"][0]["loungeToken"]
    dev = str(uuid.uuid4())
    base = {"VER": "8", "CVER": "1", "device": "REMOTE_CONTROL", "id": dev, "loungeIdToken": lt, "t": "1"}
    r = _open("https://www.youtube.com/api/lounge/bc/bind?" + urllib.parse.urlencode(
        {**base, "RID": "1", "name": "tvremote (read only)", "app": "tvremote", "mdx-version": "3", "zx": uuid.uuid4().hex[:12]}), form, b"count=0").read().decode()
    sid, gs = re.search(r'"c","([^"]+)"', r), re.search(r'"S","([^"]+)"', r)
    if not (sid and gs):
        return
    resp = _open("https://www.youtube.com/api/lounge/bc/bind?" + urllib.parse.urlencode(
        {**base, "RID": "rpc", "SID": sid.group(1), "gsessionid": gs.group(1), "AID": "0", "CI": "0", "TYPE": "xmlhttp", "zx": uuid.uuid4().hex[:12]}), timeout=70)
    buf = ""
    while not stop.is_set():
        chunk = resp.read1(4096)
        if not chunk:
            return
        buf = (buf + chunk.decode(errors="replace"))[-20000:]
        ev = parse_events(buf)
        if ev:
            publish(ev)


def _run(tv_id, ip, st):
    delay = 5
    while not st["stop"].is_set() and time.time() - st["seen"] < LINGER:
        try:
            def publish(ev):
                vid = ev["videoId"]
                with _lock:
                    cur = _info.get(tv_id) or {}
                if cur.get("video") != vid:
                    d = video_details(vid) or (None, None, None)
                    with _lock:
                        _info[tv_id] = {"app": "YouTube", "video": vid, "title": d[0], "channel": d[1], "thumb": d[2]}
                    nonlocal delay
                    delay = 5
            _youtube_session(ip, st["stop"], publish)
        except (OSError, ValueError, KeyError, IndexError):
            pass  # a session that can't start or drops: try again after a pause
        st["stop"].wait(delay)
        delay = min(delay * 2, 60)
    with _lock:
        _info.pop(tv_id, None)
        if _state.get(tv_id) is st:
            del _state[tv_id]


def sync(tv_id, ip, app):
    """Called when the TV's open app is known (None when it is on an input, home or off): start, keep or stop its listener."""
    with _lock:
        st = _state.get(tv_id)
        if st and st["app"] != app:
            st["stop"].set()
            _info.pop(tv_id, None)
            del _state[tv_id]
            st = None
        if app != "YouTube":
            return
        if st:
            st["seen"] = time.time()
            return
        st = _state[tv_id] = {"seen": time.time(), "stop": threading.Event(), "app": app}
    threading.Thread(target=_run, args=(tv_id, ip, st), daemon=True).start()


def get(tv_id, app):
    """{"title", "channel", "thumb", "video"} for what the TV's app is playing, or None (unknown, or a different app now)."""
    with _lock:
        d = _info.get(tv_id)
        return dict(d) if d and d.get("app") == app else None
