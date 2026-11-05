#!/usr/bin/env python3
"""TV Remote: web controller for mpv / streamlink on the Pi."""
import hmac
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("TVREMOTE_DATA", "/data")
PASSWORD = os.environ.get("TVREMOTE_PASSWORD", "")
PORT = int(os.environ.get("TVREMOTE_PORT", "8765"))
CEC_PHYS = os.environ.get("TVREMOTE_CEC_PHYS", "1.0.0.0")
SOCK = "/tmp/tvremote-mpv.sock"
FAV_FILE = os.path.join(DATA, "favourites.json")
LOG_FILE = os.path.join(DATA, "player.log")

# Routing table: one line per site. player: "streamlink", "ytdlp" or "auto" (YouTube: live -> streamlink).
SOURCES = [
    # (name,      hosts,                              player)
    ("Kick",    ("kick.com",),                       "streamlink"),
    ("Twitch",  ("twitch.tv",),                      "streamlink"),
    ("YouTube", ("youtube.com", "youtu.be"),         "auto"),
]
SL_DEFAULT = "1080p60,best"
YTDL_HEIGHTS = ("1080", "720", "480")

import roku

try:
    from cast import CastTarget
except ImportError:  # pychromecast missing: Pi target only
    CastTarget = None
CAST = None  # created at startup (mDNS discovery runs in the background)

app = Flask(__name__, static_folder=None)


def tgt():
    """The Chromecast target when ?target=bedroom, else None (= the Pi)."""
    if request.args.get("target") == "bedroom":
        if not CAST:  # never fall back to the Pi: that would control the wrong TV
            abort(Response("Chromecast support is not running", 503))
        return CAST
    return None
lock = threading.Lock()
state = {"proc": None, "url": None, "quality": None, "source": None, "player": None}
_qcache = {}
_live_cache = {}


@app.before_request
def guard():
    """CSRF check, then Basic auth when TVREMOTE_PASSWORD is set (/play also takes ?t=<password>)."""
    token_ok = bool(PASSWORD) and hmac.compare_digest(request.args.get("t", "").encode(), PASSWORD.encode())
    origin = request.headers.get("Origin")
    if request.method != "GET" and origin and urlparse(origin).netloc != request.host:
        return Response("Cross-site request blocked", 403)
    if request.path == "/play" and request.headers.get("Sec-Fetch-Site") == "cross-site" and not token_ok:
        return Response("Cross-site request blocked", 403)
    if not PASSWORD:
        return None
    if request.remote_addr == "127.0.0.1" and request.path == "/api/status":  # container healthcheck
        return None
    a = request.authorization
    if a and hmac.compare_digest((a.password or "").encode(), PASSWORD.encode()):
        return None
    if request.path == "/play" and token_ok:
        return None
    return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="TV Remote"'})


# ---------- routing ----------

def route(url):
    """Return (source name, player) for a URL."""
    host = (urlparse(url).hostname or "").lower()
    for name, hosts, player in SOURCES:
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name, player
    return "Other", "ytdlp"


def youtube_is_live(url):
    if re.search(r"/live/?($|\?)", urlparse(url).path + "?"):
        return True
    if url in _live_cache:
        return _live_cache[url]
    try:
        out = subprocess.run(["yt-dlp", "--no-playlist", "--print", "is_live", url],
                             capture_output=True, text=True, timeout=20).stdout
        live = out.strip().splitlines()[-1] == "True"
    except (OSError, subprocess.TimeoutExpired, IndexError):
        live = False
    _live_cache[url] = live
    return live


def resolve(url):
    """(source, player) with 'auto' resolved to streamlink or ytdlp."""
    source, player = route(url)
    if player == "auto":
        player = "streamlink" if youtube_is_live(url) else "ytdlp"
    return source, player


def normalise(text, site="kick"):
    """Pull the first URL out of shared text; a bare word becomes a channel on `site`."""
    text = (text or "").strip()
    m = re.search(r"https?://[^\s<>\"']+", text)
    if m:
        return m.group(0).rstrip(").,")
    if re.fullmatch(r"[A-Za-z0-9_\-]{2,40}", text):
        return f"https://{'twitch.tv' if site == 'twitch' else 'kick.com'}/{text}"
    return None


# ---------- mpv ----------

def mpv(cmd, timeout=1.0):
    """Send one command over mpv's IPC socket and return the reply."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(SOCK)
            s.sendall((json.dumps({"command": cmd, "request_id": 1}) + "\n").encode())
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return None
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("request_id") == 1:
                        return msg
    except OSError:
        return None


def prop(name):
    r = mpv(["get_property", name])
    if r and r.get("error") == "success":
        return r.get("data")
    return None


def cec(*args):
    try:
        subprocess.run(["cec-ctl", "-d", "/dev/cec0", *args],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def switch_tv_to_pi():
    cec("--to", "0", "--image-view-on")
    cec("--active-source", f"phys-addr={CEC_PHYS}")


def stop_player():
    p = state["proc"]
    if p and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(3)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    state["proc"] = None


def start_player(url, quality=None):
    stop_player()
    source, player = resolve(url)
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    ipc = f"--input-ipc-server={SOCK}"
    if player == "streamlink":
        # quality reaches argv: allow only stream names like "1080p60,best" (no leading "-")
        ok = quality and re.fullmatch(r"[a-z0-9_]+(,[a-z0-9_]+)*", quality) and not quality.isdigit()
        q = quality if ok else SL_DEFAULT
        cmd = ["streamlink", "--player-verbose", "--player", "mpv", "--player-args", ipc]
        if source == "Twitch":
            cmd.append("--twitch-disable-ads")
        cmd += [url, q]
    else:
        h = quality if quality in YTDL_HEIGHTS else "1080"
        fmt = f"bv*[vcodec^=avc1][height<={h}]+ba/b[height<={h}]/b"
        cmd = ["mpv", ipc, f"--ytdl-format={fmt}", url]
        q = h
    log = open(LOG_FILE, "w")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    state.update(proc=proc, url=url, quality=q, source=source, player=player, cmd=cmd, started=time.time())
    if player == "streamlink":  # warm the metadata cache for the title
        threading.Thread(target=sl_info, args=(url,), daemon=True).start()
    threading.Thread(target=switch_tv_to_pi, daemon=True).start()


def log_lines(tail=100_000):
    """Player log lines without mpv's per-frame 'AV:' status spam (it ends in CR, so splitlines splits it)."""
    try:
        with open(LOG_FILE, errors="replace") as f:
            lines = f.read()[-tail:].splitlines()
    except OSError:
        return []
    return [l for l in lines if not re.match(r"\s*(AV|A|V):", l)]


def last_error():
    """Last error-looking line from the player log, e.g. 'HTTP error 404 Not Found'."""
    lines = log_lines()
    # specific causes first; generic "Exiting..."/"Failed to open" lines are only a fallback
    for pat in (r"HTTP.?error|ERROR:|logged-in|login|not found|no playable|unsupported", r"error|failed"):
        for line in reversed(lines):
            if re.search(pat, line, re.I) and not line.startswith(("Exiting", "Failed to open")):
                return re.sub(r"^\[[^\]]*\]\s*(ERROR:\s*)?", "", line.strip())[:200]
    return None


def qualities(url):
    if not url:
        return []
    player = state["player"] if url == state["url"] else resolve(url)[1]
    if player == "ytdlp":
        return list(YTDL_HEIGHTS)
    return sl_info(url)[0]


def sl_info(url):
    """(quality list, metadata dict) from `streamlink --json`, cached 2 min."""
    cached = _qcache.get(url)
    if cached and time.time() - cached[0] < 120:
        return cached[1], cached[2]
    try:
        out = subprocess.run(["streamlink", "--json", url],
                             capture_output=True, text=True, timeout=20).stdout
        j = json.loads(out)
        streams, meta = list(j.get("streams", {})), j.get("metadata") or {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        streams, meta = [], {}
    order = [s for s in streams if s not in ("worst", "best")][::-1]
    if order:  # don't cache failures
        _qcache[url] = (time.time(), order, meta)
    return order, meta


def display_title(url):
    """Streamlink sources: 'author: title' from metadata, else the channel name (never the raw URL)."""
    meta = (_qcache.get(url) or (0, 0, {}))[2]
    parts = [meta.get("author"), meta.get("title")]
    return ": ".join(p for p in parts if p) or fav_name(url)


# ---------- favourites ----------

def load_favs():
    try:
        with open(FAV_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_favs(favs):
    tmp = FAV_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(favs, f, indent=2)
    os.replace(tmp, FAV_FILE)


def fav_name(url):
    path = urlparse(url).path.strip("/")
    return path.split("/")[0] if path else (urlparse(url).hostname or url)


# ---------- routes ----------

@app.get("/")
def index():
    return send_from_directory(BASE, "index.html", max_age=0)


@app.get("/manifest.json")
def manifest():
    return jsonify(name="TV Remote", short_name="TV Remote", start_url="/", display="standalone",
                   background_color="#0b0b0f", theme_color="#0b0b0f")


@app.get("/play")
def play_get():
    """Used by the iPhone Shortcut: start playing, then show the remote."""
    url = normalise(request.args.get("u") or request.args.get("url"))
    c = tgt()
    if url:
        try:
            play_on(c, url, request.args.get("q"), request.args.get("via"))
        except Exception as e:  # shown on the remote via /api/status
            if c:
                c.state["error"] = str(e)
    return redirect(f"/?target={c.id}" if c else "/", code=303)


def switch_input(tid):
    """Wake the Roku TV and select this target's HDMI input (e.g. TVREMOTE_INPUT_BEDROOM=HDMI2)."""
    ip, hdmi = ROKU_IPS.get(tid), INPUTS.get(tid)
    if not (ip and hdmi):
        return
    try:
        if roku.info(ip).get("power") != "PowerOn":
            roku.key(ip, "PowerOn")
            time.sleep(6)  # TV needs a few seconds before it accepts an input change
        roku.key(ip, "Input" + hdmi)
    except (roku.RokuError, OSError):
        pass  # best effort: playback already started


def play_on(c, url, quality, via=None):
    """Start `url` on the Chromecast target `c`, or on the Pi when c is None."""
    if c:
        c.play(url, quality, via)
    else:
        with lock:
            start_player(url, quality)
    threading.Thread(target=switch_input, args=(c.id if c else "living",), daemon=True).start()


@app.post("/api/play")
def api_play():
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    url = normalise(data.get("url") or data.get("text") or request.get_data(as_text=True),
                    data.get("site", "kick"))
    if not url:
        return jsonify(ok=False, error="No link found"), 400
    try:
        play_on(tgt(), url, data.get("quality"), data.get("via"))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502
    return jsonify(ok=True, url=url)


COMMANDS = {
    "pause": lambda v: ["cycle", "pause"],
    "mute": lambda v: ["cycle", "mute"],
    "volume": lambda v: ["set_property", "volume", max(0.0, min(130.0, float(v)))],
    "volume_delta": lambda v: ["add", "volume", float(v)],
    "seek": lambda v: ["seek", float(v), "relative"],
}


@app.post("/api/cmd")
def api_cmd():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    c = tgt()
    if c:
        try:
            ok, err = c.cmd(action, data.get("value"), data.get("quality"))
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 502
        return jsonify(ok=ok, error=err), (200 if ok else 400)
    if action == "stop":
        with lock:
            stop_player()
        return jsonify(ok=True)
    if action == "restart":
        if not state["url"]:
            return jsonify(ok=False, error="Nothing to restart"), 400
        with lock:
            start_player(state["url"], data.get("quality") or state["quality"])
        return jsonify(ok=True)
    fn = COMMANDS.get(action)
    if not fn:
        return jsonify(ok=False, error="Unknown action"), 400
    try:
        cmd = fn(data.get("value", 0))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Bad value"), 400
    r = mpv(cmd)
    ok = bool(r and r.get("error") == "success")
    if ok and action in ("volume", "volume_delta", "mute", "unmute"):
        osd_volume()
    return jsonify(ok=ok)


def osd_volume():
    """Draw a TV-style volume message on the stream (mpv OSD)."""
    vol, muted = prop("volume"), prop("mute")
    if vol is not None:
        mpv(["show-text", "Muted" if muted else f"Volume {round(vol)}%", 1500])


PROPS = {
    "title": "media-title", "paused": "pause", "volume": "volume", "muted": "mute",
    "position": "time-pos", "duration": "duration", "height": "height",
    "hwdec": "hwdec-current", "drops": "frame-drop-count",
}


@app.get("/api/status")
def api_status():
    c = tgt()
    if c:
        try:
            return jsonify(c.status())
        except Exception as e:
            return jsonify(running=False, target=c.id, error=str(e))
    running = prop("pid") is not None
    data = {"running": running, "url": state["url"], "quality": state["quality"],
            "source": state["source"], "streamlink": state["player"] == "streamlink"}
    if running:
        for key, name in PROPS.items():
            data[key] = prop(name)
        if state["player"] == "streamlink":
            data["title"] = display_title(state["url"])
        # live = streamlink, or a stream with no duration
        data["live"] = state["player"] == "streamlink" or data["duration"] is None
    else:
        p = state["proc"]
        data["starting"] = bool(p and p.poll() is None)
        if state["url"] and not data["starting"]:
            data["error"] = last_error()
    return jsonify(data)


_tv = {"t": 0, "v": None}


@app.get("/api/tv")
def api_tv():
    """TV name and power state over CEC (cached 30s; CEC is slow and shared)."""
    if time.time() - _tv["t"] > 30:
        try:
            out = subprocess.run(["cec-ctl", "-d", "/dev/cec0", "--to", "0", "--give-osd-name",
                                  "--give-device-power-status"], capture_output=True, text=True, timeout=8).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        name = re.search(r"name: (.+)", out)
        pwr = re.search(r"pwr-state: (\w+)", out)
        _tv.update(t=time.time(), v={"name": name.group(1).strip() if name else None,
                                     "power": pwr.group(1) if pwr else None})
    return jsonify(_tv["v"])


@app.get("/api/qualities")
def api_qualities():
    c = tgt()
    if c:
        return jsonify(c.qualities(request.args.get("url") or c.state["url"], qualities))
    return jsonify(qualities(request.args.get("url") or state["url"]))


ROKU_IPS = {"living": os.environ.get("TVREMOTE_ROKU_LIVING", ""),
            "bedroom": os.environ.get("TVREMOTE_ROKU_BEDROOM", "")}


@app.get("/api/targets")
def api_targets():
    out = [{"id": "living", "name": "Living room", "type": "pi", "online": True}]
    if CAST:
        out.append(CAST.info())
    for t in out:
        t["roku"] = bool(ROKU_IPS.get(t["id"]))
    return jsonify(out)


INPUTS = {"living": os.environ.get("TVREMOTE_INPUT_LIVING", ""),
          "bedroom": os.environ.get("TVREMOTE_INPUT_BEDROOM", "")}


def roku_ip():
    return ROKU_IPS.get("bedroom" if request.args.get("target") == "bedroom" else "living")


@app.get("/api/roku")
def api_roku():
    ip = roku_ip()
    if not ip:
        return jsonify(ok=False, error="No Roku configured for this TV"), 404
    try:
        return jsonify(roku.info(ip))
    except roku.RokuError as e:
        return jsonify(ok=False, error=str(e)), 502


@app.post("/api/roku/key")
def api_roku_key():
    ip = roku_ip()
    if not ip:
        return jsonify(ok=False, error="No Roku configured for this TV"), 404
    k = (request.get_json(silent=True) or {}).get("key")
    if k not in roku.KEYS:
        return jsonify(ok=False, error="Unknown key"), 400
    try:
        roku.key(ip, k)
    except roku.RokuError as e:
        return jsonify(ok=False, error=str(e)), 502
    return jsonify(ok=True)


@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    try:
        out = subprocess.run(["yt-dlp", f"ytsearch10:{q}", "--flat-playlist", "-J"],
                             capture_output=True, text=True, timeout=30).stdout
        entries = json.loads(out).get("entries", [])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return jsonify(ok=False, error="Search failed"), 502
    return jsonify([{
        "title": e.get("title"), "channel": e.get("channel") or e.get("uploader"),
        "duration": e.get("duration"), "live": e.get("live_status") == "is_live",
        "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
        "thumb": f"https://i.ytimg.com/vi/{e.get('id')}/mqdefault.jpg",
    } for e in entries if e.get("id")])


DEBUG_PROPS = ["hwdec-current", "video-codec", "width", "height", "container-fps", "estimated-vf-fps",
               "frame-drop-count", "decoder-frame-drop-count", "vo-delayed-frame-count", "avsync",
               "audio-codec-name", "demuxer-cache-duration", "cache-speed", "current-vo", "current-ao", "pid"]
_versions = {}


def versions():
    if not _versions:
        for name, cmd in {"mpv": ["mpv", "--version"], "streamlink": ["streamlink", "--version"],
                          "yt-dlp": ["yt-dlp", "--version"], "deno": ["deno", "--version"]}.items():
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
                _versions[name] = out.strip().splitlines()[0] if out.strip() else "?"
            except (OSError, subprocess.TimeoutExpired):
                _versions[name] = "missing"
    return _versions


@app.get("/api/debug")
def api_debug():
    c = tgt()
    if c:
        d = c.debug()
        d["versions"] = {**versions(), **d["versions"]}
        return jsonify(d)
    p = state["proc"]
    log = chr(10).join(log_lines())[-8000:]
    return jsonify(
        state={k: v for k, v in state.items() if k != "proc"},
        proc={"pid": p.pid if p else None, "returncode": p.poll() if p else None},
        uptime=round(time.time() - state["started"]) if state.get("started") else None,
        mpv={k: prop(k) for k in DEBUG_PROPS} if prop("pid") is not None else None,
        versions=versions(), last_error=last_error(), log=log)


@app.get("/api/favourites")
def favs_get():
    return jsonify(load_favs())


@app.post("/api/favourites")
def favs_add():
    data = request.get_json(silent=True) or {}
    url = normalise(data.get("url") or state["url"])
    if not url:
        return jsonify(ok=False), 400
    favs = [f for f in load_favs() if f.get("url") != url]
    name = data.get("name") or fav_name(url)
    favs.append({"name": name[:40], "url": url, "source": route(url)[0]})
    save_favs(favs)
    return jsonify(favs)


@app.delete("/api/favourites")
def favs_del():
    url = (request.get_json(silent=True) or {}).get("url")
    favs = [f for f in load_favs() if f.get("url") != url]
    save_favs(favs)
    return jsonify(favs)


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    if CastTarget:
        CAST = CastTarget(DATA, resolve, display_title, YTDL_HEIGHTS, SL_DEFAULT)
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=8)
