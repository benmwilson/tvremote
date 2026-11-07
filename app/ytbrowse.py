"""YouTube channels and playlists as lists of cards, with yt-dlp's flat extraction (no API key). Pure apart from the fetch, which is injected.
A card has the same shape as a search result: title, channel, duration, live, url, thumb (plus views)."""
import re
import threading
import time
from urllib.parse import parse_qs, urlparse

TTL = 300  # a listing is remembered five minutes
TABS = ("videos", "streams", "shorts")
_cache = {}  # (url, tab) -> (when, listing)
_lock = threading.Lock()


def _is_youtube(host):
    return host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be"


def kind(url):
    """"channel", "playlist" or "video" for a YouTube link; None for anything else."""
    p = urlparse(url or "")
    if p.scheme not in ("http", "https") or not _is_youtube((p.hostname or "").lower()):
        return None
    q = parse_qs(p.query)
    if p.path.rstrip("/") == "/playlist" and q.get("list"):
        return "playlist"
    if re.match(r"/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(/(videos|streams|shorts|featured|live))?/?$", p.path):
        return "channel"
    return "video"


def list_url(url, tab="videos"):
    """The address yt-dlp should list: a channel's tab (videos, streams or shorts), or the playlist itself."""
    p = urlparse(url)
    if kind(url) == "playlist":
        return f"https://www.youtube.com/playlist?list={parse_qs(p.query)['list'][0]}"
    base = re.sub(r"/(videos|streams|shorts|featured|live)/?$", "", p.path.rstrip("/"))
    return f"https://www.youtube.com{base}/{tab if tab in TABS else 'videos'}"


def card(e):
    vid = e.get("id")
    return {"title": e.get("title"), "channel": e.get("channel") or e.get("uploader"), "duration": e.get("duration"),
            "live": e.get("live_status") == "is_live", "views": e.get("view_count"),
            "url": e.get("url") if str(e.get("url", "")).startswith("http") else f"https://www.youtube.com/watch?v={vid}",
            "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"}


def avatar(info):
    """The channel's profile picture from the listing's thumbnails (the square one), or None."""
    for t in info.get("thumbnails") or []:
        if t.get("id") == "avatar_uncropped" or (t.get("width") and t.get("width") == t.get("height")):
            return t.get("url")
    return None


def listing(url, tab="videos", count=30, fetch=None):
    """{"kind", "title", "channel", "avatar", "subscribers", "tab", "entries": [cards]} for a channel tab or a playlist; remembered for TTL.
    fetch(address, count) -> yt-dlp's flat info dict. Raises ValueError for a link that is not a channel or a playlist."""
    k = kind(url)
    if k not in ("channel", "playlist"):
        raise ValueError("not a channel or a playlist")
    tab = tab if tab in TABS else "videos"
    target = list_url(url, tab)
    now = time.time()
    with _lock:
        hit = _cache.get((target, tab))
        if hit and now - hit[0] < TTL:
            return hit[1]
    info = fetch(target, count)
    out = {"kind": k, "tab": tab if k == "channel" else None, "title": info.get("title") or info.get("channel"),
           "channel": info.get("channel") or info.get("uploader"), "avatar": avatar(info), "subscribers": info.get("channel_follower_count"),
           "entries": [card(e) for e in (info.get("entries") or []) if e and e.get("id") and e.get("_type", "url") != "playlist"][:count]}
    with _lock:
        if len(_cache) > 60:
            _cache.pop(next(iter(_cache)))
        _cache[(target, tab)] = (now, out)
    return out


if __name__ == "__main__":
    assert kind("https://www.youtube.com/@mkbhd") == "channel" and kind("https://www.youtube.com/@mkbhd/videos") == "channel"
    assert kind("https://www.youtube.com/playlist?list=PL1") == "playlist"
    assert kind("https://www.youtube.com/watch?v=abc&list=PL1") == "video" and kind("https://youtu.be/abc") == "video"
    assert kind("https://kick.com/x") is None
    assert list_url("https://www.youtube.com/@mkbhd/featured", "streams") == "https://www.youtube.com/@mkbhd/streams"
    assert list_url("https://www.youtube.com/playlist?list=PL1&x=1") == "https://www.youtube.com/playlist?list=PL1"
    print("ytbrowse ok")
