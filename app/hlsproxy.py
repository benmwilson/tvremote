"""HLS relay for the Chromecast: the Pi (wired) fetches the live segments and keeps the latest few in memory,
then serves a playlist pointing at itself, so the Chromecast only does plain HTTP on the LAN.
shortcut: media playlists only (no EXT-X-MAP/KEY rewriting, no master playlist), upgrade if a source needs them."""
import hmac
import ipaddress
import secrets
import socket
import threading
import urllib.request
from collections import OrderedDict
from urllib.parse import quote, urljoin, urlparse

MAX_BYTES = 30_000_000  # per playlist/segment
MAX_CACHED = 14  # ~1 minute of 4s segments, about 40 MB at 1080p60
_lock = threading.Lock()
_up = {"url": None, "token": None}
_cache = OrderedDict()  # segment url -> _Seg
_allowed = set()        # only segments listed in the playlist can be fetched through us


class _Seg:
    def __init__(self):
        self.done = threading.Event()
        self.data = None


ALLOW_PRIVATE = False  # only the self-check below turns this on


def _check(url):
    """Playlists are untrusted: only public http(s) hosts (no file://, loopback, LAN or link-local addresses).
    shortcut: DNS can change between this check and the fetch (rebinding); pin the resolved IP if that matters."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("unsupported URL")
    if not ALLOW_PRIVATE:
        for info in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80)):
            if not ipaddress.ip_address(info[4][0]).is_global:
                raise ValueError("non-public address")


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_open = urllib.request.build_opener(_Redirect).open


def _get(url, timeout=15):
    _check(url)
    with _open(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=timeout) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("response too large")
    return data


def token():
    return _up["token"]


def valid(candidate):
    t = _up["token"]
    return bool(t) and hmac.compare_digest(candidate.encode(), t.encode())


def clear():
    """End of the relay session: the token and upstream stop working."""
    with _lock:
        _up["url"] = _up["token"] = None
        _cache.clear()
        _allowed.clear()


def set_upstream(url):
    with _lock:
        _up["url"] = url
        _up["token"] = secrets.token_urlsafe(16)  # unguessable path segment: the Chromecast gets it in the URL we cast
        _cache.clear()
        _allowed.clear()


def _fetch(u):
    with _lock:
        seg = _cache.get(u)
        if seg:
            return seg
        seg = _cache[u] = _Seg()
        while len(_cache) > MAX_CACHED:
            _cache.popitem(last=False)

    def work():
        try:
            seg.data = _get(u)
        except Exception:
            seg.data = None
            with _lock:
                if _cache.get(u) is seg:
                    del _cache[u]  # let the next request retry
        seg.done.set()
    threading.Thread(target=work, daemon=True).start()
    return seg


def playlist():
    """Upstream media playlist with every segment URL rewritten to this server (and prefetched)."""
    base = _up["url"]
    if not base:
        raise RuntimeError("no upstream set")
    out = []
    for line in _get(base).decode(errors="replace").splitlines():
        if line and not line.startswith("#"):
            u = urljoin(base, line)
            try:
                _check(u)
            except ValueError:
                continue  # drop segments we refuse to fetch
            with _lock:
                _allowed.add(u)
            _fetch(u)
            line = "seg?u=" + quote(u, safe="")
        out.append(line)
    return "\n".join(out) + "\n"


def segment(u):
    """Bytes of a segment from the latest playlist, or None."""
    with _lock:
        if u not in _allowed:
            return None
    seg = _fetch(u)
    seg.done.wait(20)
    return seg.data


if __name__ == "__main__":  # self-check against a local fake upstream
    import http.server
    import re

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/p.m3u8":
                b = b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nhttp://127.0.0.1:8798/s1.ts\n#EXTINF:4,\n/s2.ts\n"
            else:
                b = b"\x47" + self.path.encode()
            self.send_response(200); self.end_headers(); self.wfile.write(b)

        def log_message(*a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8798), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ALLOW_PRIVATE = True
    set_upstream("http://127.0.0.1:8798/p.m3u8")
    pl = playlist()
    segs = re.findall(r"seg\?u=(\S+)", pl)
    assert len(segs) == 2 and "%2Fs2.ts" in segs[1], pl           # relative URL resolved, both rewritten
    from urllib.parse import unquote
    assert segment(unquote(segs[0])) == b"\x47/s1.ts"
    assert segment("http://evil.example/x") is None               # not in the playlist -> refused
    ALLOW_PRIVATE = False
    for bad in ("file:///etc/passwd", "ftp://example.com/x", "http://127.0.0.1/x", "http://192.168.1.1/x", "http://169.254.169.254/x"):
        try:
            _check(bad); raise SystemExit("allowed: " + bad)
        except ValueError:
            pass
    _check("https://example.com/x")
    print("hlsproxy ok")
