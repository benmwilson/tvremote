"""Fake tvremote API for checking the UI without the Pi or any TV.

    python tests/stub_server.py [port]          # default 8799
    curl "http://127.0.0.1:8799/__set?living=playing&bedroom=down"

States per TV: idle, playing, paused, buffering, starting, down, error.  Add play_fail=bedroom to make
/api/play fail for that TV.  All names and addresses are placeholders.
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP = Path(__file__).resolve().parents[1] / "app"
STATE = {"living": "playing", "bedroom": "idle", "play_fail": ""}
INFO = {
    "living": {"name": "Living room", "kind": "pi", "connection": "Pi on HDMI", "brand": "TCL", "model": "55C803", "size": 55,
               "tv_name": "TV One", "power": "PowerOn", "input": "Roku"},
    "bedroom": {"name": "Bedroom", "kind": "chromecast", "connection": "Chromecast", "brand": "TCL", "model": "43S425-CA",
                "size": 43, "tv_name": "TV Two", "power": "PowerOn", "input": "Chromecast", "device": "Chromecast"},
}
TITLE = {"living": "chan: a very long stream title that keeps going and going 🎮🎮🎮 with emoji", "bedroom": "Big Buck Bunny"}
SOURCE = {"living": "Kick", "bedroom": "YouTube"}


def status(tv):
    s = STATE[tv]
    if s in ("idle", "down"):
        return {"running": False, "target": tv}
    base = {"running": True, "target": tv, "title": TITLE[tv], "source": SOURCE[tv], "live": tv == "living",
            "paused": s == "paused", "position": 83.0, "duration": None if tv == "living" else 634.0,
            "quality": "1080p60,best" if tv == "living" else "1080", "volume": 60, "muted": False, "height": 1080,
            "hwdec": "v4l2m2m" if tv == "living" else "youtube", "drops": 2 if tv == "living" else None,
            "url": "https://example.com/" + tv, "buffering": s == "buffering", "chat_ok": False}
    if s == "starting":
        return {"running": False, "starting": True, "target": tv, "url": base["url"], "source": SOURCE[tv]}
    if s == "error":
        return {"running": False, "target": tv, "error": "HTTP error 404 Not Found", "url": base["url"]}
    return base


def health(tv):
    st = status(tv)
    state = ("error" if st.get("error") else "starting" if st.get("starting") else "buffering" if st.get("buffering")
             else "smooth" if st.get("running") else "idle")
    return {"state": state, "drops": st.get("drops"), "start_s": 9.0 if st.get("running") else None}


def card(tv):
    st, down = status(tv), STATE[tv] == "down"
    busy = st.get("running") or st.get("starting")
    now = {"title": TITLE[tv], "source": SOURCE[tv], "live": bool(st.get("live")), "paused": bool(st.get("paused")),
           "starting": bool(st.get("starting")), "position": st.get("position"),
           "stream": "1080p60" if tv == "living" else "1080p"} if busy and not down else None
    c = {"id": tv, **INFO[tv], "reachable": not down, "now": now, "health": health(tv), "type": INFO[tv]["kind"],
         "online": not down, "roku": True, "error": st.get("error")}
    if down:
        c.update(now=None, power=None, input=None)
    return c


class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json", code=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        tv = q.get("target", "living")
        if u.path in ("/", "/share"):
            return self._send((APP / "index.html").read_bytes(), "text/html")
        if u.path == "/__set":
            STATE.update({k: v for k, v in q.items() if k in STATE})
            return self._send(STATE)
        routes = {
            "/api/targets": lambda: [card("living"), card("bedroom")],
            "/api/status": lambda: status(tv),
            "/api/favourites": lambda: [],
            "/api/qualities": lambda: ["1080p60", "720p60", "480p"] if tv == "living" else ["1080", "720", "480"],
            "/api/roku": lambda: {"name": INFO[tv]["tv_name"], "power": "PowerOn", "app": INFO[tv]["input"]},
            "/api/tv": lambda: {"name": "TV One", "power": "on"},
            "/api/debug": lambda: {"state": {}, "proc": {}, "mpv": {"hwdec-current": "v4l2m2m"}, "versions": {}, "log": "stub"},
            "/manifest.json": lambda: {"name": "TV Remote"},
        }
        if u.path in routes:
            return self._send(routes[u.path]())
        self._send({"error": "not found"}, code=404)

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if u.path == "/api/play":
            time.sleep(0.3)
            if STATE["play_fail"] == q.get("target"):
                return self._send({"ok": False, "error": "The bedroom Chromecast isn't available. "
                                   "Check that it is powered on and on the same Wi-Fi."}, code=503)
            STATE[q.get("target", "living")] = "playing"
            return self._send({"ok": True})
        self._send({"ok": True})

    do_DELETE = do_POST

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    print(f"stub on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
