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

from flask import Flask, Response, abort, jsonify, send_file, redirect, request, send_from_directory

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

import hlsproxy
import live
import chat
import mpvipc
import roku
import shell
import targets

try:
    from cast import CastTarget
except ImportError:  # pychromecast missing: Pi target only
    CastTarget = None
CAST = None  # created at startup (mDNS discovery runs in the background)

app = Flask(__name__, static_folder=None)


def tgt():
    """The Chromecast target for ?target=bedroom, None for the Pi (no target, or "living"). Anything else is
    refused: guessing would risk controlling the wrong TV."""
    t = request.args.get("target") or "living"
    if t == "living":
        return None
    if t == "bedroom":
        if not CAST:  # never fall back to the Pi: that would control the wrong TV
            abort(Response(json.dumps({"ok": False, "error": "The bedroom Chromecast isn't available. "
                                       "Check that it is powered on and on the same Wi-Fi."}),
                           503, mimetype="application/json"))
        return CAST
    abort(Response(json.dumps({"ok": False, "error": "Unknown TV."}), 400, mimetype="application/json"))


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
    if not PASSWORD or re.fullmatch(r"/cast/[\w-]+/(live\.m3u8|seg)", request.path):
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
    return mpvipc.send(SOCK, cmd, timeout)


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


SETTINGS_FILE = os.path.join(DATA, "settings.json")
settings = {"shell": os.environ.get("TVREMOTE_SHELL", "1") != "0"}  # the TV's home screen; TVREMOTE_SHELL=0 starts with it off
HOME = None  # the home screen (shell.Shell), created at startup


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return {**settings, **json.load(f)}
    except (OSError, ValueError):
        return dict(settings)


def save_settings():
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f)
    os.replace(tmp, SETTINGS_FILE)


def screen(kind, detail=""):
    """Show a screen on the TV's home screen, if it is on."""
    if HOME and settings.get("shell"):
        HOME.show(kind, detail)


def describe_error(raw):
    """Plain words for the player's last error."""
    if not raw:
        return None
    if re.search(r"No playable streams|offline", raw, re.I):
        return "Isn't live right now"
    if re.search(r"HTTP.?error 404|Not Found", raw, re.I):
        return "That link wasn't found (404)"
    if re.search(r"logged-in|log in|login|sign in", raw, re.I):
        return "That needs a login, which isn't supported"
    return raw


def on_input(tv_id):
    """True when this TV is awake and already showing the HDMI input its target uses. Re-selecting an input the
    TV is already on relaunches it, which flashes the screen."""
    ip, hdmi = ROKU_IPS.get(tv_id), INPUTS.get(tv_id)
    if not (ip and hdmi):
        return False
    try:
        info = roku.info(ip)
    except (roku.RokuError, OSError):
        return False
    return info.get("power") == "PowerOn" and info.get("app_id") == "tvinput." + hdmi.lower()


def switch_tv_to_pi():
    if on_input("living"):
        return
    cec("--to", "0", "--image-view-on")
    cec("--active-source", f"phys-addr={CEC_PHYS}")


def stop_player():
    p = state["proc"]
    state["proc"] = None  # first, so the watcher knows this end was on purpose
    if p and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(3)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    chat_stop()


_chat = {"obj": None, "pref": False}  # off by default: redrawing the overlay drops about 2 frames/s on the Pi


def chat_send(text):
    """Draw (or clear) the chat overlay in mpv; True when mpv accepted it."""
    r = mpv(["osd-overlay", 1, "ass-events", text, 1920, 1080] if text else ["osd-overlay", 1, "none"])
    return bool(r and r.get("error") == "success")


def chat_stop():
    c, _chat["obj"] = _chat["obj"], None
    if c:
        c.stop()


def chat_sync():
    """Match the overlay to what is playing: a live Twitch channel with chat switched on."""
    chat_stop()
    ch = chat.channel_of(state["url"] or "") if state["source"] == "Twitch" and state["proc"] else None
    if ch and _chat["pref"]:
        _chat["obj"] = chat.TwitchChat(ch, chat_send)


def _watch(proc):
    """Follow one player: hide the loading screen once it is up, and show how it ended."""
    for _ in range(120):  # up to a minute for mpv to open its window
        if proc.poll() is not None or prop("pid") is not None:
            break
        time.sleep(0.5)
    if proc.poll() is None:
        screen("playing")
    proc.wait()
    if state["proc"] is not proc:
        return  # replaced, or stopped on purpose
    raw = last_error()
    name = fav_name(state["url"] or "")
    if raw and re.search(r"No playable streams|offline", raw, re.I):
        screen("offline", name)
    elif proc.returncode or raw:
        screen("error", describe_error(raw) or "The player stopped unexpectedly.")
    else:
        screen("ended", name)


def buffering_screen():
    """Drawn only while the picture is stalled, so redrawing costs the stream nothing."""
    return "\n".join([
        r"{\an7\pos(0,0)\p1\bord0\shad0\1c&H000000&\1a&HB0&}m 0 0 l 1920 0 1920 1080 0 1080{\p0}",
        r"{\an5\pos(960,500)\fs72\b1\bord0\shad0\1c&HF6F1EE&}Buffering\u2026",
        r"{\an5\pos(960,590)\fs36\b0\bord0\shad0\1c&HA9978C&}Waiting for the stream. If this keeps happening, lower the quality on your phone."])


def _stall_monitor(proc):
    """Count stalls and show Buffering on the TV while the picture waits for data."""
    stalled_at = None
    while proc.poll() is None:
        time.sleep(1)
        stalled = prop("paused-for-cache") is True
        if stalled and stalled_at is None:
            stalled_at = time.time()
            state["stalls"] = state.get("stalls", 0) + 1
            mpv(["osd-overlay", 3, "ass-events", buffering_screen(), 1920, 1080])
        elif not stalled and stalled_at is not None:
            state["stall_s"] = state.get("stall_s", 0) + time.time() - stalled_at
            stalled_at = None
            mpv(["osd-overlay", 3, "none"])


def start_player(url, quality=None, screen_kind="loading"):
    # before the old stream stops, so a swap never shows the desktop
    screen(screen_kind, fav_name(url) + ("  \u00b7  " + quality if screen_kind == "quality" and quality else ""))
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
    state.update(proc=proc, url=url, quality=q, source=source, player=player, cmd=cmd, started=time.time(), stalls=0, stall_s=0)
    chat_sync()
    threading.Thread(target=_watch, args=(proc,), daemon=True).start()
    if is_channel(url):
        threading.Thread(target=livewatch.refresh_one, args=(url,), daemon=True).start()
    threading.Thread(target=_stall_monitor, args=(proc,), daemon=True).start()
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
@app.get("/share")
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
    return redirect(f"/?target={c.id if c else 'living'}", code=303)


def switch_input(tid):
    """Wake the Roku TV and select this target's HDMI input (e.g. TVREMOTE_INPUT_BEDROOM=HDMI2)."""
    ip, hdmi = ROKU_IPS.get(tid), INPUTS.get(tid)
    if not (ip and hdmi):
        return
    try:
        info = roku.info(ip)
        if info.get("power") != "PowerOn":
            roku.key(ip, "PowerOn")
            time.sleep(6)  # TV needs a few seconds before it accepts an input change
        elif info.get("app_id") == "tvinput." + hdmi.lower():
            return  # already on it: re-selecting relaunches the input
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
    c = tgt()  # outside the try: its 503 must reach the client, not be turned into a 502
    try:
        play_on(c, url, data.get("quality"), data.get("via"))
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
            state.update(url=None, source=None, quality=None)  # a deliberate stop leaves no old error or "now playing" behind
        screen("idle")
        return jsonify(ok=True)
    if action == "restart":
        if not state["url"]:
            return jsonify(ok=False, error="Nothing to restart"), 400
        with lock:
            start_player(state["url"], data.get("quality") or state["quality"], "quality")
        return jsonify(ok=True)
    if action == "chat":
        _chat["pref"] = data.get("value") != "off"
        chat_sync()
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
    if ok and action == "pause":
        osd_paused(bool(prop("pause")))
    if ok and action in ("volume", "volume_delta", "mute", "unmute"):
        osd_volume()
    return jsonify(ok=ok)


def paused_screen(title=""):
    """ASS for the pause overlay: the picture dimmed, a pause symbol and "Paused". Overlay 2 (chat uses 1).
    Drawn once and left alone: the picture is frozen while paused, so unlike the chat overlay it costs no frames."""
    parts = [r"{\an7\pos(0,0)\p1\bord0\shad0\1c&H000000&\1a&H99&}m 0 0 l 1920 0 1920 1080 0 1080{\p0}",
             r"{\an7\pos(0,0)\p1\bord0\shad0\1c&HF6F1EE&}m 850 380 l 920 380 920 580 850 580 m 1000 380 l 1070 380 1070 580 1000 580{\p0}",
             r"{\an8\pos(960,620)\fs72\b1\bord0\shad0\1c&HF6F1EE&}Paused"]
    if title:
        parts.append(r"{\an8\pos(960,720)\fs38\b0\bord0\shad0\1c&HA9978C&}" + chat._esc(title))
    return "\n".join(parts)


def osd_paused(paused):
    """Show the pause overlay while paused; clear it on resume."""
    if paused:
        mpv(["osd-overlay", 2, "ass-events", paused_screen(prop("media-title") or ""), 1920, 1080])
    else:
        mpv(["osd-overlay", 2, "none"])


def osd_volume():
    """Draw a TV-style volume message on the stream (mpv OSD)."""
    vol, muted = prop("volume"), prop("mute")
    if vol is not None:
        mpv(["show-text", "Muted" if muted else f"Volume {round(vol)}%", 1500])


PROPS = {
    "title": "media-title", "paused": "pause", "volume": "volume", "muted": "mute",
    "position": "time-pos", "duration": "duration", "height": "height",
    "hwdec": "hwdec-current", "drops": "frame-drop-count", "buffering": "paused-for-cache",
}


@app.get("/api/status")
def api_status():
    c = tgt()
    if c:
        try:
            return jsonify(c.status())
        except Exception as e:
            return jsonify(running=False, target=c.id, error=str(e))
    return jsonify(pi_status())


def pi_status():
    """Status of the Pi's own player (mpv); also feeds the living-room card."""
    running = prop("pid") is not None
    data = {"running": running, "url": state["url"], "quality": state["quality"],
            "source": state["source"], "streamlink": state["player"] == "streamlink"}
    if running:
        for key, name in PROPS.items():
            data[key] = prop(name)
        if state["player"] == "streamlink":
            data["title"] = display_title(state["url"])
        data["stalls"], data["stall_s"] = state.get("stalls", 0), round(state.get("stall_s", 0))
        facts = livewatch.snapshot().get(state["url"] or "", {})
        data["game"], data["live_for"] = facts.get("game"), livewatch.live_for(state["url"] or "")
        data["chat_ok"] = state["source"] == "Twitch" and bool(chat.channel_of(state["url"] or ""))
        data["chat"] = bool(_chat["obj"])
        # live = streamlink, or a stream with no duration
        data["live"] = state["player"] == "streamlink" or data["duration"] is None
    else:
        p = state["proc"]
        data["starting"] = bool(p and p.poll() is None)
        if state["url"] and not data["starting"]:
            data["error"] = describe_error(last_error())
    return data


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


def from_chromecast():
    """Defense in depth next to the token: the request must also come from the Chromecast's address."""
    d = CAST.device() if CAST else None
    return bool(d) and request.remote_addr == d.host


@app.get("/cast/<token>/live.m3u8")
def cast_playlist(token):
    """Relay playlist for the Chromecast (see hlsproxy.py). Unauthenticated (the Chromecast can't send a password) but guarded by a random per-cast token in the path."""
    if not (hlsproxy.valid(token) and from_chromecast()):
        abort(404)
    try:
        body = hlsproxy.playlist()
    except Exception:
        return Response("upstream unavailable", 502)
    return Response(body, mimetype="application/x-mpegURL",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})


@app.get("/cast/<token>/seg")
def cast_segment(token):
    if not (hlsproxy.valid(token) and from_chromecast()):
        abort(404)
    data = hlsproxy.segment(request.args.get("u", ""))
    if not data:
        abort(404)
    return Response(data, mimetype="video/mp2t", headers={"Access-Control-Allow-Origin": "*"})


ORDER = ["living", "bedroom"]
LABELS = {"living": "Living room", "bedroom": "Bedroom"}
KINDS = {"living": ("pi", "Pi on HDMI"), "bedroom": ("chromecast", "Chromecast")}


def stream_label(quality):
    """'1080p60,best' -> '1080p60'; '720' -> '720p'."""
    first = (quality or "").split(",")[0]
    return (first if "p" in first else first + "p") if first else None


_roku = {}       # tv_id -> (fetched at, info)
_roku_busy = set()
ROKU_TTL = 15    # seconds: TV details change rarely, and every ECP call costs two round trips


def fetch_roku(tv_id):
    try:
        info = roku.info(ROKU_IPS[tv_id])
    except (roku.RokuError, OSError):
        info = _roku.get(tv_id, (0, {}))[1]  # a TV that won't talk ECP keeps its last details
    _roku[tv_id] = (time.time(), info)
    _roku_busy.discard(tv_id)


def roku_details(tv_id):
    """Cached TV details. A stale or missing entry is refreshed in the background, so a card never waits on a TV."""
    if not ROKU_IPS.get(tv_id):
        return {}
    fetched, info = _roku.get(tv_id, (0, {}))
    if time.time() - fetched > ROKU_TTL and tv_id not in _roku_busy:
        _roku_busy.add(tv_id)
        threading.Thread(target=fetch_roku, args=(tv_id,), daemon=True).start()
    return info


def make_card(tv_id, status):
    kind, connection = KINDS[tv_id]
    r = roku_details(tv_id)
    busy = status.get("running") or status.get("starting")
    now = {"title": status.get("title") or fav_name(status.get("url") or ""), "source": status.get("source"),
           "live": bool(status.get("live")), "paused": bool(status.get("paused")),
           "starting": bool(status.get("starting")), "position": status.get("position"),
           "game": livewatch.snapshot().get(status.get("url") or "", {}).get("game"),
           "live_for": livewatch.live_for(status.get("url") or ""),
           "stream": stream_label(status.get("quality"))} if busy else None
    return {"id": tv_id, "name": LABELS[tv_id], "kind": kind, "connection": connection,
            "brand": r.get("brand"), "model": r.get("model"), "size": r.get("size"), "tv_name": r.get("name"),
            "power": r.get("power"), "input": r.get("app"), "input_id": r.get("app_id"), "now": now, "error": status.get("error"),
            "health": targets.health(kind, status)}


def living_card():
    return make_card("living", pi_status())


def bedroom_card():
    if not CAST or not CAST.info()["online"]:
        raise OSError("Chromecast not found")
    card = make_card("bedroom", CAST.status())
    card["device"] = CAST.info()["device"]
    return card


cards = targets.Cards({"living": living_card, "bedroom": bedroom_card}, ORDER)


SHELL_SHOT = "/tmp/tvremote-shell.png"


def shell_state():
    running = bool(HOME and HOME.running())
    return {"enabled": bool(settings.get("shell")), "running": running,
            "vo": HOME.vo if HOME else None, "error": HOME.error if HOME else None,
            "props": HOME.props() if running and hasattr(HOME, "props") else None}  # what the window is doing, for debugging


SCREEN_SHOT = "/tmp/tvremote-screen.png"


@app.get("/api/screen.png")
def api_screen():
    """The Pi's real screen, as the desktop is showing it right now (for debugging what the TV displays)."""
    try:
        subprocess.run(["grim", "-o", os.environ.get("TVREMOTE_OUTPUT", "HDMI-A-1"), SCREEN_SHOT],
                       check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return jsonify(ok=False, error="Couldn't capture the screen (is grim installed, and is the desktop running?)."), 502
    return send_file(SCREEN_SHOT, mimetype="image/png", max_age=0)


@app.get("/api/shell/screenshot")
def api_shell_screenshot():
    """What the home screen window is drawing right now, as a PNG (for debugging)."""
    r = mpvipc.send(shell.SOCK, ["screenshot-to-file", SHELL_SHOT, "window"], 4)
    if not (r and r.get("error") == "success"):
        return jsonify(ok=False, error="The home screen isn't running, or couldn't take a screenshot."), 502
    return send_file(SHELL_SHOT, mimetype="image/png", max_age=0)


@app.get("/api/shell")
def api_shell():
    return jsonify(shell_state())


@app.post("/api/shell")
def api_shell_set():
    """Turn the TV's home screen on or off (off shows the Pi desktop again). The app and this remote keep running."""
    settings["shell"] = bool((request.get_json(silent=True) or {}).get("enabled"))
    save_settings()
    if HOME:
        if settings["shell"]:
            HOME.start()
            if state["proc"] and state["url"]:  # the new window opens on top of a playing stream, so bring the stream back up
                with lock:
                    start_player(state["url"], state["quality"])
        else:
            HOME.stop()
    return jsonify(shell_state())


@app.get("/api/targets")
def api_targets():
    """One card per TV from the background cache; never waits on a TV. type/online/roku are kept for the old UI."""
    out = []
    for c in cards.snapshot():
        kind, connection = KINDS[c["id"]]
        out.append({"name": LABELS[c["id"]], "kind": kind, "connection": connection, **c,
                    "type": kind, "online": c["reachable"], "roku": bool(ROKU_IPS.get(c["id"]))})
    return jsonify(out)


INPUTS = {"living": os.environ.get("TVREMOTE_INPUT_LIVING", ""),
          "bedroom": os.environ.get("TVREMOTE_INPUT_BEDROOM", "")}


def roku_ip():
    return ROKU_IPS.get(request.args.get("target") or "living")  # None for a TV we don't know


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


def is_channel(url):
    """A Kick or Twitch channel page (not a video, clip or directory)."""
    if route(url)[0] not in ("Kick", "Twitch"):
        return False
    parts = [p for p in urlparse(url).path.split("/") if p]
    return len(parts) == 1 and parts[0] not in ("videos", "directory", "clips")


def run_streamlink_json(url, timeout=30):
    """`streamlink --json` for a channel: streams and metadata when it is live, an error when not, None if it could not run.
    Runs at a low priority so it never competes with a stream that is playing."""
    try:
        out = subprocess.run(["nice", "-n", "10", "streamlink", "--json", url],
                             capture_output=True, text=True, timeout=timeout).stdout
        return json.loads(out)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def live_seconds(j):
    """How long a channel has been live, from the elapsed-time tag in the stream's own playlist (Kick and Twitch carry one)."""
    try:
        text = hlsproxy._get(j["streams"]["best"]["url"]).decode(errors="replace")
    except Exception:
        return None
    m = re.search(r"ELAPSED-SECS:(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def check_live(url):
    """{"live", "game", "title", "uptime_s"} for a channel, {"live": False} when offline, None when it could not be checked."""
    if not is_channel(url):
        return None
    j = run_streamlink_json(url)
    if j is None:
        return None
    if j.get("streams"):
        meta = j.get("metadata") or {}
        return {"live": True, "game": meta.get("category"), "title": meta.get("title"), "uptime_s": live_seconds(j)}
    if "No playable streams" in (j.get("error") or ""):
        return {"live": False}
    return None


def watched_urls():
    urls = [f.get("url") or "" for f in load_favs()]
    if state.get("url"):
        urls.append(state["url"])
    return list(dict.fromkeys(u for u in urls if is_channel(u)))


def pi_playing():
    p = state.get("proc")
    return bool(p and p.poll() is None)


livewatch = live.LiveWatch(os.path.join(DATA, "live.json"), urls=watched_urls, check=check_live, busy=pi_playing)


@app.get("/api/favourites")
def favs_get():
    livewatch.poke()
    snap = livewatch.snapshot()
    out = []
    for f in load_favs():
        e = snap.get(f.get("url"), {})
        out.append({**f, "live": e.get("live"), "game": e.get("game"), "last_live": e.get("last_live"),
                    "checked": e.get("checked"), "live_for": livewatch.live_for(f.get("url"))})
    return jsonify(out)


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
    cards.start()
    livewatch.start()
    settings.update(load_settings())
    HOME = shell.Shell(hint=f"Open http://{shell.lan_ip()}:{PORT} on your phone",
                       favs=lambda: [(f.get("name") or "")[:28] for f in load_favs()][:5])
    if settings["shell"]:
        threading.Thread(target=HOME.start, daemon=True).start()
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=8)
