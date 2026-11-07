"""streamlink and yt-dlp inside the app's own process.

Starting either as a command on the Pi costs about 3 seconds before it does anything (Python and its imports), and every stream lookup, quality
list, live check and search started one. In-process, after a one-time load, a lookup is 0.4 s instead of 5-9 s. Every function here raises when
the library can't do it, and the caller falls back to the command, so a library problem is slower, never broken.
Nothing here decides what to play: it answers the same questions the commands did, in the same shapes."""
import os
import threading
import time

ENABLED = os.environ.get("TVREMOTE_INPROC", "1") != "0"  # 0: always use the commands (the tests do, they stand in for them)


class Unavailable(Exception):
    """The libraries are switched off: the caller uses the command."""

_lock = threading.Lock()
_session = None


def _load():
    """Import yt-dlp before streamlink, always: streamlink swaps a part of urllib3 (the URL percent-encoding helper) for its own, and a yt-dlp that is
    imported after it fails with "'Urllib3UtilUrlPercentReOverride' object has no attribute 'sub'", which also breaks streamlink's own requests.
    Imported in this order both work. Found on the Pi, where streams quietly fell back to the slow path."""
    import yt_dlp  # noqa: F401
    import streamlink  # noqa: F401


def session():
    """One streamlink session for the whole app (made once; plugins load on first use)."""
    global _session
    if not ENABLED:
        raise Unavailable()
    with _lock:
        if _session is None:
            _load()
            from streamlink import Streamlink
            _session = Streamlink()
        return _session


def streamlink_json(url):
    """The same answer as `streamlink --json <url>`: {"plugin", "metadata": {author, category, title, id}, "streams": {name: {"url"}}}, or {"error"}
    when there is nothing to play (an offline channel, a link no plugin handles). Raises for anything unexpected."""
    from streamlink.exceptions import NoPluginError, PluginError
    s = session()
    try:
        name, cls, resolved = s.resolve_url(url)
    except NoPluginError:
        return {"error": f"No plugin can handle URL: {url}"}
    plugin = cls(s, resolved)
    try:
        streams = plugin.streams()
    except PluginError as e:
        return {"error": str(e)}
    if not streams:
        return {"error": f"No playable streams found on this URL: {url}"}
    out = {}
    for k, v in streams.items():
        try:
            out[k] = {"url": v.to_url()}
        except Exception:  # a stream that is not one address (it needs several): the command's output has no address for it either
            out[k] = {"url": None}
    return {"plugin": name, "streams": out,
            "metadata": {"id": plugin.get_id(), "author": plugin.get_author(), "category": plugin.get_category(), "title": plugin.get_title()}}


def stream_address(url, quality):
    """The address `streamlink --stream-url <url> <quality>` prints: the first of the comma-separated qualities that exists. Raises RuntimeError with
    what the command would have said when there is none."""
    j = streamlink_json(url)
    if j.get("error"):
        raise RuntimeError(j["error"][:200])
    streams = j["streams"]
    for q in (x.strip() for x in quality.split(",")):
        if streams.get(q, {}).get("url"):
            return streams[q]["url"]
    raise RuntimeError(f"The specified stream(s) '{quality}' could not be found")


_ydl = {}


def _yt(**opts):
    if not ENABLED:
        raise Unavailable()
    _load()
    from yt_dlp import YoutubeDL
    base = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True, "socket_timeout": 15}
    return YoutubeDL({**base, **opts})


def youtube_is_live(url):
    """True/False from yt-dlp's own `is_live` (the command printed the same)."""
    with _yt() as ydl:
        info = ydl.extract_info(url, download=False)
    return bool(info and info.get("is_live"))


def youtube_search(query, count=10):
    """`yt-dlp ytsearch<count>:<query> --flat-playlist -J` as a dict."""
    with _yt(extract_flat=True, noplaylist=False) as ydl:
        info = ydl.extract_info(f"ytsearch{int(count)}:{query}", download=False)
        return ydl.sanitize_info(info)


def youtube_list(url, count=30):
    """`yt-dlp --flat-playlist -J --playlist-end <count> <url>` as a dict: a channel tab or a playlist, no video pages fetched."""
    with _yt(extract_flat=True, noplaylist=False, playlistend=int(count)) as ydl:
        return ydl.sanitize_info(ydl.extract_info(url, download=False))


def warm():
    """Do the one-time work in the background at startup, so the first tap is not the one that pays for it: load streamlink and its Kick and
    Twitch plugins, and yt-dlp's YouTube code. Never raises."""
    t = time.time()
    try:
        s = session()
        for u in ("https://kick.com/warm", "https://www.twitch.tv/warm"):
            s.resolve_url(u)  # loads the plugin module (no network)
        _yt()
    except Exception:
        return None
    return time.time() - t


def ytdlp_address(url, fmt):
    """What `yt-dlp -g -f <fmt> <url>` prints first: the address of the chosen format."""
    with _yt(format=fmt) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("url"):
        return info["url"]
    for f in info.get("requested_formats") or []:
        if f.get("url"):
            return f["url"]
    raise RuntimeError("no video address")


def video_id(url):
    """`yt-dlp --print id <url>`."""
    with _yt() as ydl:
        return ydl.extract_info(url, download=False)["id"]
