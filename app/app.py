#!/usr/bin/env python3
"""TV Remote: web controller for mpv / streamlink on the Pi."""
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import quote, urlparse

from flask import Flask, Response, abort, g, has_request_context, jsonify, send_file, redirect, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("TVREMOTE_DATA", "/data")
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
import access
import accounts
import activity
import chat
import discover
import kick
import mpvipc
import roku
import shell
import targets
import tlscallback
import tvconfig

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
    """CSRF check, then login (a cookie from the login page, or Basic auth with an account for scripts)."""
    g.user = accounts.OWNER  # the stand-in for "nobody is set up yet"; a login cookie says who it really is
    origin = request.headers.get("Origin")
    if request.method != "GET" and origin and urlparse(origin).netloc != request.host:
        return Response("Cross-site request blocked", 403)
    if request.path == "/play" and request.headers.get("Sec-Fetch-Site") == "cross-site":
        return Response("Cross-site request blocked", 403)
    if not auth_required():
        # Nobody can log in until setup has made the first admin, and an upgrade's older data must not be readable meanwhile
        # either: only the wizard's own page and calls (and the container's health check) work.
        if ((request.method in ("GET", "HEAD") and (request.path in SETUP_GET or re.fullmatch(r"/icon-\d+\.png", request.path)))
                or (request.method == "POST" and request.path == "/api/setup/admin")
                or (request.remote_addr == "127.0.0.1" and request.path == "/api/status")):
            return None
        if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
            return redirect("/", 303)
        return Response(json.dumps({"ok": False, "error": "Finish the setup first."}), 403, mimetype="application/json")
    if re.fullmatch(r"/cast/[\w-]+/(live\.m3u8|seg)|/logo\.svg|/icon-\d+\.png|/manifest\.json|/request|/join/[\w-]+", request.path):
        return None
    if request.remote_addr == "127.0.0.1" and request.path == "/api/status":  # container healthcheck
        return None
    if request.path in ("/login", "/logout"):
        return None
    who = session_user(request.cookies.get("tv_auth", ""))  # one read of the session: valid and whose, together
    if who:
        g.user = who
        if who == accounts.GUEST and guest_blocked():
            return Response(json.dumps({"ok": False, "error": "Guests can control the TVs, but not do that."}), 403, mimetype="application/json")
        return None
    a = request.authorization
    wants_page = "text/html" in request.headers.get("Accept", "")
    # a browser page load needs the login cookie: Safari remembers the old password pop-up and would otherwise stay logged in after logout
    if a and not wants_page:  # curl and scripts: Basic auth with an account
        name = (a.username or "").strip().lower()
        if _throttled(request.remote_addr, name):
            return too_many()
        if name and accounts.verify(name, a.password or ""):
            g.user = name
            return None
        _note_fail(request.remote_addr, name)
        time.sleep(0.8)  # slows guessing, like the login page
    if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):  # a browser: show the login page
        return redirect("/login?next=" + quote(request.full_path.rstrip("?")), 303)
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="TV Remote"'})


SESSION_DAYS = 180
SETUP_GET = {"/", "/logo.svg", "/manifest.json", "/login", "/api/auth", "/api/setup"}  # what works before the first admin exists


# What a one-time-link guest may do: use the TVs and see the pages that show them. Everything else (including any route added
# later) is refused, so a new endpoint is closed to guests until it is listed here on purpose.
GUEST_GET = {"/", "/share", "/logo.svg", "/manifest.json", "/login", "/logout", "/play", "/api/auth", "/api/status", "/api/tv",
             "/api/qualities", "/api/targets", "/api/roku", "/api/recents", "/api/search", "/api/favourites", "/api/kick",
             "/api/shell", "/api/stats", "/api/chat/feed", "/api/sleep"}
GUEST_POST = {"/api/play", "/api/cmd", "/api/tv/wake", "/api/roku/key", "/api/sleep", "/logout"}


def guest_blocked():
    path = request.path
    if request.method in ("GET", "HEAD"):
        return not (path in GUEST_GET or re.fullmatch(r"/icon-\d+\.png", path))
    return not (request.method == "POST" and path in GUEST_POST)


_sessions_lock = threading.Lock()


def _sess_file():
    return os.path.join(DATA, "sessions.json")


def _sess_load():
    try:
        with open(_sess_file()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _sess_save(d):
    try:
        with open(_sess_file() + ".tmp", "w") as f:
            json.dump(d, f)
        os.replace(_sess_file() + ".tmp", _sess_file())
    except OSError:
        pass  # sessions then last until the next restart


_revoked = set()  # logged-out tokens, kept in memory too so a failed disk write still ends the session


def auth_required():
    """Login is needed once accounts have ever been set up. A fresh install is closed to changes until setup makes the first admin.
    An unreadable accounts file counts as set up (fail closed)."""
    return accounts.initialised()


# Failed logins (the form, a script, the setup code), newest last. Keys: "<address>" (everything from one address, IPv6 by /64),
# "<address>|<name>" (one account from one address) and "setup" (every guess at the setup code, from anywhere).
_fails = {}
WINDOW = 600


def _bucket(ip):
    try:
        a = ipaddress.ip_address(ip or "")
        return str(ipaddress.ip_network(f"{a}/64", strict=False)) if a.version == 6 else str(a)
    except ValueError:
        return ip or "?"


def _recent(key, now=None):
    now = now or time.time()
    r = [t for t in _fails.get(key, []) if now - t < WINDOW]
    if r:
        _fails[key] = r
    else:
        _fails.pop(key, None)  # no entry for anyone with no recent failures
    return r


def _prune(now):
    if len(_fails) > 5000:  # only ever drops what has expired, then the oldest: a flood of addresses can't reset anyone's count
        for k in list(_fails):
            _recent(k, now)
        for k in sorted(_fails, key=lambda k: _fails[k][-1])[:max(0, len(_fails) - 4000)]:
            del _fails[k]


def _throttled(ip, who=""):
    now = time.time()
    _prune(now)
    ip = _bucket(ip)
    return len(_recent(ip, now)) >= 30 or bool(who) and len(_recent(f"{ip}|{who}", now)) >= 10


def _note_fail(ip, who=""):
    ip, now = _bucket(ip), time.time()
    for k in (ip, f"{ip}|{who}" if who else None):
        if k:
            _fails.setdefault(k, []).append(now)


def _note_ok(ip, who):
    _fails.pop(f"{_bucket(ip)}|{who}", None)  # only this account's count: a login that works can't wipe the failures against others


def too_many():
    return Response("Too many failed tries. Wait a few minutes and try again.", 429)


_key = {"v": None}


def _session_key():
    if _key["v"] is None:  # a random key kept in /data
        path = os.path.join(DATA, "session.key")
        try:
            with open(path, "rb") as f:
                _key["v"] = f.read()
        except OSError:
            _key["v"] = secrets.token_bytes(32)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(_key["v"])
            except OSError:
                pass  # sessions then last until the next restart
    return _key["v"]


def _h(tok):
    # keyed: only hashes are stored (the file can't be used as a cookie)
    return hmac.new(_session_key(), tok.encode(), hashlib.sha256).hexdigest()


def new_session(user=accounts.OWNER, seconds=None, extra=None):
    """A random token for one login; the server remembers it (hashed) until it expires or the user logs out."""
    tok = secrets.token_urlsafe(32)
    with _sessions_lock:
        d = {k: v for k, v in _sess_load().items() if _exp(v) > time.time()}
        exp = time.time() + (seconds or SESSION_DAYS * 86400)
        d[_h(tok)] = {"exp": exp, "user": user, **(extra or {})}
        _sess_save(d)
    return tok


def _exp(v):
    return v["exp"] if isinstance(v, dict) else v


def session_user(tok):
    """Who a login cookie belongs to ("owner" or an account name), or None when it is not a live session."""
    if not tok or _h(tok) in _revoked:
        return None
    v = _sess_load().get(_h(tok))
    if v is None or _exp(v) <= time.time():
        return None
    if not isinstance(v, dict):
        return None  # an old-style session (from when the .env password existed) is not a login any more
    return v["user"] if v["user"] == accounts.GUEST or accounts.exists(v["user"]) else None


def guest_sessions():
    return [{"id": k[:8], "label": v.get("label", ""), "exp": v["exp"], "since": v.get("since"), "last": activity.last("guest:" + k[:8])}
            for k, v in _sess_load().items()
            if isinstance(v, dict) and v.get("user") == accounts.GUEST and v["exp"] > time.time() and k not in _revoked]


def drop_guest(gid):
    activity.forget("guest:" + gid)
    with _sessions_lock:
        d = _sess_load()
        gone = [k for k, v in d.items() if len(gid) >= 8 and k.startswith(gid) and isinstance(v, dict) and v.get("user") == accounts.GUEST]
        for k in gone:
            del d[k]
            _revoked.add(k)
        if gone:
            _sess_save(d)
        return bool(gone)


def valid_session(tok):
    return session_user(tok) is not None


def drop_user_sessions(name):
    """End every login of an account (it was removed or its password changed)."""
    with _sessions_lock:
        d = _sess_load()
        gone = [k for k, v in d.items() if isinstance(v, dict) and v.get("user") == name]
        for k in gone:
            del d[k]
            _revoked.add(k)
        if gone:
            _sess_save(d)


def drop_session(tok):
    with _sessions_lock:
        d = _sess_load()
        if d.pop(_h(tok), None) is not None:  # only real sessions are remembered: /logout needs no login, so junk must not pile up
            _revoked.add(_h(tok))
            _sess_save(d)


def safe_next(n):
    """Where to go after login: only a plain path on this site. Browsers treat a backslash like a slash, so backslashes and
    control characters are out too."""
    if not n or not n.startswith("/") or n.startswith("//") or "\\" in n or any(ord(c) < 32 for c in n):
        return "/"
    p = urlparse(n)
    return n if not p.scheme and not p.netloc else "/"


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0b0e14"><title>TV Remote login</title>
<style>:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0e14;color:#f6f1ee;font:17px -apple-system,system-ui,sans-serif}
form{width:min(340px,86vw);display:grid;gap:14px}h1{margin:0 0 6px;font-size:28px}
input,button{font:inherit;min-height:52px;border-radius:14px;border:0;padding:0 16px;box-sizing:border-box;width:100%}
input{background:#1a2030;color:#f6f1ee}button{background:#62b0ff;color:#0b0e14;font-weight:700}.e{color:#ff6b6b;min-height:1.2em;margin:0}</style></head>
<body><form method="post" action="/login"><img src="/logo.svg" width="84" height="84" alt="" style="justify-self:center;border-radius:20px"><h1 style="text-align:center">TV Remote</h1>
<input name="username" placeholder="Username" autocomplete="username" autocapitalize="none" aria-label="Username">
<input type="password" name="password" placeholder="Password" autocomplete="current-password" aria-label="Password" autofocus required>
<input type="hidden" name="next" value="__NEXT__"><p class="e" role="alert">__ERR__</p><button>Log in</button><a href="/request" style="text-align:center;color:#8c97a8;padding:10px">Request access</a></form></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = safe_next(request.values.get("next", "/"))
    if not auth_required():
        return redirect(nxt, 303)
    err = ""
    if request.method == "POST":
        who = request.form.get("username", "").strip().lower()
        if _throttled(request.remote_addr, who):
            return too_many()
        user = who if accounts.verify(who, request.form.get("password", "")) else None  # always runs the slow hash, even for an unknown name
        if user:
            _note_ok(request.remote_addr, user)
            accounts.touch(user)
            activity.note(user, "Logged in")
            r = redirect(nxt, 303)
            r.set_cookie("tv_auth", new_session(user), max_age=SESSION_DAYS * 86400, httponly=True, samesite="Lax")
            return r
        _note_fail(request.remote_addr, who)
        time.sleep(0.8)  # slows guessing
        err = "Wrong username or password"
    return Response(LOGIN_PAGE.replace("__NEXT__", html.escape(nxt, quote=True)).replace("__ERR__", err), 401 if err else 200,
                    mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.route("/logout", methods=["GET", "POST"])
def logout():
    drop_session(request.cookies.get("tv_auth", ""))
    r = redirect("/login", 303)
    r.delete_cookie("tv_auth")
    return r


@app.get("/api/auth")
def api_auth():
    info = {}
    if g.user == accounts.GUEST:
        v = _sess_load().get(_h(request.cookies.get("tv_auth", ""))) or {}
        info = {"guest_until": v.get("exp"), "label": v.get("label", "")}
    return jsonify(required=auth_required(), user=g.user, owner=accounts.is_owner(g.user), role=accounts.role(g.user),
                   admin=accounts.role(g.user) == "admin", guest=g.user == accounts.GUEST, **info)


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
settings = {"stats": False, "shell": os.environ.get("TVREMOTE_SHELL", "1") != "0"}  # the TV's home screen; TVREMOTE_SHELL=0 starts with it off
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


def screen(kind, detail="", source=None):
    """Show a screen on the TV's home screen, if it is on. `source` (Kick, Twitch, YouTube) tints it in that site's colour."""
    if HOME and settings.get("shell"):
        accent = shell.SRC_BGR.get(source)
        if accent:
            HOME.show(kind, detail, accent)
        else:
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
    r = mpv(["osd-overlay", 1, "ass-events", text, 1920, 1080] if text else ["osd-overlay", 1, "none", ""])
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
        if settings.get("volume") is not None:
            mpv(["set_property", "volume", settings["volume"]])  # a new mpv starts at 100: put back what was chosen
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
        r"{\an5\pos(960,500)\fs72\b1\bord0\shad0\1c&HF6F1EE&}Buffering…",
        r"{\an5\pos(960,590)\fs36\b0\bord0\shad0\1c&HA9978C&}Waiting for the stream. If this keeps happening, lower the quality on your phone."])


def lag_pill():
    """A small corner note, drawn only while the stream stutters (overlay 4), so a lag has an explanation on screen."""
    return "\n".join([
        r"{\an9\pos(1860,50)\p1\bord0\shad0\1c&H000000&\1a&H60&}m -330 -22 l 0 -22 0 34 -330 34{\p0}",
        r"{\an9\pos(1840,56)\fs34\b1\bord0\shad0\1c&HF6F1EE&}Stream lagging…"])


def stats_overlay(drop_delta, drops, cache_s, fps, stalls):
    """Debug numbers as plain text for show-text (top-left). Plain show-text stays up; osd-overlay text kept vanishing."""
    c = "-" if cache_s is None else f"{cache_s:.1f}s"
    text = f"dropped {drop_delta}/s ({drops} total)   buffer {c}   {fps or '-'} fps   stalls {stalls}"
    return text


def is_lagging(drop_delta, cache_s):
    """A stutter: a burst of dropped frames, or the buffer running nearly dry."""
    return drop_delta >= 8 or (cache_s is not None and cache_s < 0.6)


def _stall_monitor(proc):
    """Count stalls and show Buffering on the TV while the picture waits for data; a smaller note while it only stutters."""
    stalled_at = None
    drops, lagging, calm, tick, stats_up = None, False, 0, 0, False
    while proc.poll() is None:
        time.sleep(1)
        stalled = prop("paused-for-cache") is True
        d, cache = prop("frame-drop-count"), prop("demuxer-cache-duration")
        if isinstance(d, (int, float)) and prop("pause") is not True and not stalled:
            bad = drops is not None and is_lagging(d - drops, cache if isinstance(cache, (int, float)) else None)
            calm = 0 if bad else calm + 1
            if bad and not lagging:
                lagging = True
                mpv(["osd-overlay", 4, "ass-events", lag_pill(), 1920, 1080])
            elif lagging and calm >= 4:  # stays up a few seconds so it can be read
                lagging = False
                mpv(["osd-overlay", 4, "none", ""])
        tick += 1
        if settings.get("stats") and tick % 2 == 0 and isinstance(d, (int, float)):  # a redraw costs a little, so only every 2 s
            fps = prop("estimated-vf-fps")
            mpv(["show-text", stats_overlay(int(d - drops) if drops is not None else 0, int(d),
                 cache if isinstance(cache, (int, float)) else None, round(fps) if isinstance(fps, (int, float)) else None,
                 state.get("stalls", 0)), 2600])  # a bit longer than the 2 s redraw, so it never blinks off
            if not stats_up:
                for k, v in (("osd-font-size", 26), ("osd-border-size", 2), ("osd-margin-x", 28), ("osd-margin-y", 22), ("osd-color", "#C8D0DC")):
                    mpv(["set_property", k, v])
            stats_up = True
        elif stats_up and not settings.get("stats"):
            for k, v in (("osd-font-size", 44), ("osd-border-size", 3), ("osd-margin-x", 60), ("osd-margin-y", 50), ("osd-color", "#FFFFFF")):
                mpv(["set_property", k, v])
            mpv(["show-text", "", 1])
            stats_up = False
        if isinstance(d, (int, float)):
            drops = d
        if stalled and stalled_at is None:
            stalled_at = time.time()
            state["stalls"] = state.get("stalls", 0) + 1
            mpv(["osd-overlay", 3, "ass-events", buffering_screen(), 1920, 1080])
        elif not stalled and stalled_at is not None:
            state["stall_s"] = state.get("stall_s", 0) + time.time() - stalled_at
            stalled_at = None
            mpv(["osd-overlay", 3, "none", ""])


def start_player(url, quality=None, screen_kind="loading"):
    # before the old stream stops, so a swap never shows the desktop
    screen(screen_kind, fav_name(url) + ("  \u00b7  " + quality if screen_kind == "quality" and quality else ""), route(url)[0])
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

def fav_file(user=None):
    """Each account has its own favourites file. Outside a request (background threads, the TV's home screen) it is the owner's."""
    user = user or (g.user if has_request_context() else (accounts.founder_name() or accounts.OWNER))
    # a guest maps to favourites.guest.json, which never exists (guests can't save): they see an empty list, never anyone's favourites
    return FAV_FILE if user == accounts.OWNER else os.path.join(DATA, f"favourites.{user}.json")


def load_favs(user=None):
    try:
        with open(fav_file(user)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_favs(favs, user=None):
    path = fav_file(user)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(favs, f, indent=2)
    os.replace(tmp, path)


def fav_name(url):
    path = urlparse(url).path.strip("/")
    return path.split("/")[0] if path else (urlparse(url).hostname or url)


# ---------- routes ----------

@app.get("/")
@app.get("/share")
def index():
    return send_from_directory(BASE, "index.html", max_age=0)


@app.get("/logo.svg")
def logo():
    return send_from_directory(BASE, "logo.svg", max_age=86400)


@app.get("/icon-<int:n>.png")
def icon(n):
    if n not in (180, 512):
        abort(404)
    return send_from_directory(BASE, f"icon-{n}.png", max_age=86400)


@app.get("/manifest.json")
def manifest():
    return jsonify(name="TV Remote", short_name="TV Remote", start_url="/", display="standalone",
                   background_color="#0b0e14", theme_color="#0b0e14",
                   icons=[{"src": "/icon-180.png", "sizes": "180x180", "type": "image/png"},
                          {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                          {"src": "/logo.svg", "sizes": "any", "type": "image/svg+xml"}])


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
    """Wake the Roku TV and select this target's HDMI input (set in the setup wizard, e.g. HDMI2)."""
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


RECENT_FILE = os.path.join(DATA, "recents.json")


def load_recents():
    try:
        with open(RECENT_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def remember(url):
    """Keep the last few links that were played, newest first."""
    try:
        items = [r for r in load_recents() if r.get("url") != url]
        items.insert(0, {"name": fav_name(url)[:40], "url": url, "source": route(url)[0]})
        with open(RECENT_FILE + ".tmp", "w") as f:
            json.dump(items[:8], f)
        os.replace(RECENT_FILE + ".tmp", RECENT_FILE)
    except OSError:
        pass  # history is a convenience


_sleep = {}  # per TV: when to stop (epoch seconds)


@app.route("/api/sleep", methods=["GET", "POST"])
def api_sleep():
    """Stop playing (and switch the TV off) after N minutes. POST {minutes}: 0 cancels."""
    tid = request.args.get("target") or "living"
    if tid not in ("living", "bedroom"):
        return jsonify(ok=False, error="Unknown TV."), 400
    if request.method == "POST":
        try:
            m = int((request.get_json(silent=True) or {}).get("minutes", 0))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Bad value"), 400
        if not 0 <= m <= 720:
            return jsonify(ok=False, error="Choose up to 12 hours."), 400
        if m:
            _sleep[tid] = time.time() + m * 60
        else:
            _sleep.pop(tid, None)
    return jsonify(ok=True, at=_sleep.get(tid))


def on_sleep(tid):
    """The timer ran out: stop what is playing and turn the TV off."""
    if tid == "living":
        screen("idle")
        with lock:
            stop_player()
            state.update(url=None, source=None, quality=None)
    elif CAST:
        try:
            CAST.cmd("stop")
        except Exception:
            pass
    ip = ROKU_IPS.get(tid)
    if ip:
        try:
            roku.key(ip, "PowerOff")
        except (roku.RokuError, OSError):
            pass


def check_sleep(now=None):
    now = now or time.time()
    for tid in [t for t, at in _sleep.items() if at <= now]:
        _sleep.pop(tid, None)
        on_sleep(tid)


def sleep_loop():
    while True:
        time.sleep(5)
        check_sleep()


@app.get("/api/recents")
def api_recents():
    return jsonify(load_recents())


_start_seq = {}  # per TV: the newest start request. A request that is no longer the newest quits without starting.


def play_on(c, url, quality, via=None):
    """Start `url` on the Chromecast target `c`, or on the Pi when c is None. Newest tap wins: an older start that is
    still working out its link (yt-dlp can take ~20 s) gives up instead of running after the newer one."""
    tid = c.id if c else "living"
    cur = c.state if c else state
    if cur.get("url") == url and time.time() - (cur.get("started") or 0) < 3:
        return  # the same link was started a moment ago (a double tap): leave it be
    remember(url)
    seq = _start_seq[tid] = _start_seq.get(tid, 0) + 1
    newest = lambda: _start_seq.get(tid) == seq
    if c:
        c.play(url, quality, via, cancelled=lambda: not newest())
    else:
        resolve(url)  # the slow part (cached afterwards), done before taking the player lock so a newer tap is never stuck behind it
        with lock:
            if newest():
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
    "seek_to": lambda v: ["seek", max(0.0, float(v)), "absolute"],
}


@app.post("/api/cmd")
def api_cmd():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    c = tgt()
    if action == "stop":  # a start still working out its link must not launch after this
        _start_seq[c.id if c else "living"] = _start_seq.get(c.id if c else "living", 0) + 1
    if c:
        try:
            ok, err = c.cmd(action, data.get("value"), data.get("quality"))
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 502
        return jsonify(ok=ok, error=err), (200 if ok else 400)
    if action == "stop":
        screen("idle")  # first: the home screen still shows its last screen (in the old site's colour) until it is redrawn
        with lock:  # waits for a start that is mid-way, then ends it
            stop_player()
            state.update(url=None, source=None, quality=None)  # a deliberate stop leaves no old error or "now playing" behind
        screen("idle")  # again: that start may have drawn its Loading screen after the first Ready
        return jsonify(ok=True)
    if action == "restart":
        if not state["url"]:
            return jsonify(ok=False, error="Nothing to restart"), 400
        with lock:
            start_player(state["url"], data.get("quality") or state["quality"], "quality" if data.get("quality") else "refresh")
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
    if ok and action in ("volume", "volume_delta") and prop("volume") is not None:
        settings["volume"] = prop("volume")
        save_settings()
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
        mpv(["osd-overlay", 2, "none", ""])


def volume_text(vol, muted):
    """Plain text for show-text: a block bar and the number, or Muted. (osd-overlay drawing flashed and lagged the stream.)"""
    v = max(0, min(100, round(vol)))
    return "Muted" if muted else "Volume %d%%  %s%s" % (v, "█" * (v // 5), "░" * (20 - v // 5))


def osd_volume():
    """Show the volume on the TV for 1.5 s."""
    vol, muted = prop("volume"), prop("mute")
    if vol is not None:
        mpv(["show-text", volume_text(vol, muted), 1500])


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
            d = c.status()
        except Exception as e:
            d = dict(running=False, target=c.id, error=str(e))
        tid = c.id
    else:
        d, tid = pi_status(), "living"
    if not d.get("running") and not d.get("starting") and not d.get("error"):
        d["external"] = external_now(tid)
    return jsonify({**d, "sleep_at": _sleep.get(tid)})


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


def tv_cec():
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
    return _tv["v"]


@app.get("/api/tv")
def api_tv():
    return jsonify(tv_cec())


@app.get("/api/qualities")
def api_qualities():
    c = tgt()
    if c:
        return jsonify(c.qualities(request.args.get("url") or c.state["url"], qualities))
    return jsonify(qualities(request.args.get("url") or state["url"]))


ROKU_IPS = {"living": "", "bedroom": ""}  # the Rokus' addresses: filled from /data/targets.json (the setup wizard) at startup


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


def on_tv_off(tv_id):
    """The TV went to standby: stop a live stream on the Pi (a paused live stream falls behind and can't resume cleanly),
    pause anything else, including the Chromecast."""
    try:
        if tv_id == "living":
            if not pi_playing():
                return
            if pi_status().get("live"):
                screen("idle")
                with lock:
                    stop_player()
                    state.update(url=None, source=None, quality=None)
            elif prop("pause") is not True:
                mpv(["set_property", "pause", True])
                osd_paused(True)
        elif tv_id == "bedroom" and CAST:
            st = CAST.status()
            if st.get("running") and not st.get("paused"):
                CAST.cmd("pause")
    except Exception:
        pass  # best effort: the TV is off either way


def fetch_roku(tv_id):
    was_on = (_roku.get(tv_id, (0, {}))[1] or {}).get("power") == "PowerOn"
    try:
        info = roku.info(ROKU_IPS[tv_id])
        if external_app(info):  # an app on the TV itself (YouTube, Netflix...): ask what its player is doing. Read-only.
            try:
                info["media"] = roku.media(ROKU_IPS[tv_id])
            except (roku.RokuError, OSError):
                pass
        if was_on and info.get("power") != "PowerOn":  # went to standby (the TV's own remote, or the app)
            threading.Thread(target=on_tv_off, args=(tv_id,), daemon=True).start()
    except (roku.RokuError, OSError):
        info = _roku.get(tv_id, (0, {}))[1]  # a TV that won't talk ECP keeps its last details
    _roku[tv_id] = (time.time(), info)
    _roku_busy.discard(tv_id)


def external_app(r):
    """The app the TV itself is running (YouTube, Netflix...), or None when it is on an HDMI input, the home screen, or off."""
    app, app_id = (r or {}).get("app"), (r or {}).get("app_id") or ""
    if (r or {}).get("power") != "PowerOn" or not app or app_id.startswith("tvinput.") or app.strip().lower() in ("roku", "home"):
        return None
    return app


def external_now(tv_id):
    """What the TV is playing on its own, from the cached Roku details (never waits on the TV), or None."""
    r = roku_details(tv_id)
    app = external_app(r)
    if not app:
        return None
    state = (r.get("media") or {}).get("state")
    return {"app": app, "state": state, "paused": state == "pause", "duration": (r.get("media") or {}).get("duration")}


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
            "external": None if busy or status.get("error") else external_now(tv_id),  # playing on the TV itself, not through this remote
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
    admin_only()
    try:
        subprocess.run(["grim", "-o", os.environ.get("TVREMOTE_OUTPUT", "HDMI-A-1"), SCREEN_SHOT],
                       check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return jsonify(ok=False, error="Couldn't capture the screen (is grim installed, and is the desktop running?)."), 502
    return send_file(SCREEN_SHOT, mimetype="image/png", max_age=0)


@app.get("/api/stats")
def api_stats():
    return jsonify(enabled=bool(settings.get("stats")))


@app.post("/api/stats")
def api_stats_set():
    admin_only()
    settings["stats"] = bool((request.get_json(silent=True) or {}).get("enabled"))
    save_settings()
    return jsonify(enabled=settings["stats"])


_feed = {"obj": None}


@app.get("/api/chat/feed")
def api_chat_feed():
    """New chat messages for the phone's chat view (Twitch only). `since` is the last message id the phone has."""
    c = tgt()
    url, source = (c.state.get("url"), c.state.get("source")) if c else (state["url"] if pi_playing() else None, state["source"])
    ch = chat.channel_of(url or "") if source == "Twitch" else None
    if not ch:
        return jsonify(ok=False, channel=None, msgs=[], last=0)
    f = _feed["obj"]
    if not f or f.channel != ch or f.stopped.is_set():
        if f:
            f.stop()
        f = _feed["obj"] = chat.ChatFeed(ch)
    last = request.args.get("since", type=int) or 0
    reset = last > f.n  # the feed was started afresh since the phone last asked
    return jsonify(ok=True, channel=ch, msgs=f.since(0 if reset else last), last=f.n, reset=reset)


@app.get("/api/shell/screenshot")
def api_shell_screenshot():
    """What the home screen window is drawing right now, as a PNG (for debugging)."""
    admin_only()
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
        if not TV_ENABLED.get(c["id"], True):
            continue  # switched off in setup (for instance no screen on the Pi)
        kind, connection = KINDS[c["id"]]
        out.append({"name": LABELS[c["id"]], "kind": kind, "connection": connection, **c,
                    "type": kind, "online": c["reachable"], "roku": bool(ROKU_IPS.get(c["id"]))})
    return jsonify(out)


INPUTS = {"living": "", "bedroom": ""}  # the HDMI input each TV switches to when you play on it
TV_ENABLED = {"living": True, "bedroom": True}


def apply_tv_config(cfg):
    """Put the saved TV settings to use: Roku addresses, inputs, names, which TVs show. Takes effect at once, no restart."""
    for tid in tvconfig.IDS:
        c = cfg.get(tid) or {}
        ROKU_IPS[tid] = c.get("roku") or ""
        INPUTS[tid] = c.get("input") or ""
        TV_ENABLED[tid] = c.get("enabled", True) is not False
        if c.get("name"):
            LABELS[tid] = c["name"]
    b = cfg.get("bedroom") or {}
    if CAST and b:
        CAST.cfg.update(name=LABELS["bedroom"], device=b.get("device") or None)
        CAST.name = LABELS["bedroom"]
    _roku.clear()  # details cached for the old addresses


@app.get("/api/discover")
def api_discover():
    """Look for TVs: Rokus (network search), Chromecasts (already found), the Pi's own screen. Takes a few seconds. Includes a first guess
    of how they pair up and why; whatever was saved before comes with it so the wizard can show the current choice."""
    admin_only()
    saved = tvconfig.load(DATA)
    ips = discover.roku_scan()
    ips += [ip for ip in (((saved.get(t) or {}).get("roku")) for t in tvconfig.IDS) if ip and ip not in ips]
    rokus = discover.describe(ips, roku.info)
    casts = [{"name": d.friendly_name, "model": d.model_name, "host": str(d.host)}
             for d in (list(CAST.devices.values()) if CAST else []) if d.cast_type == "cast"]
    display = discover.display_connected()
    cec = tv_cec() if display else None
    return jsonify(rokus=rokus, chromecasts=casts, display=display, cec_name=(cec or {}).get("name"),
                   guess=discover.guess(rokus, casts, display, (cec or {}).get("name"), CEC_PHYS),
                   saved={t: saved.get(t) or {} for t in tvconfig.IDS})


@app.post("/api/targets/config")
def api_targets_config():
    admin_only()
    try:
        cfg = tvconfig.clean(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    apply_tv_config(tvconfig.save(DATA, cfg))
    return jsonify(ok=True)


@app.post("/api/targets/test")
def api_targets_test():
    """Prove a Roku can be controlled: press Info (it shows its information banner and changes nothing)."""
    admin_only()
    ip = tvconfig.lan_address((request.get_json(silent=True) or {}).get("roku"))
    if not ip:
        return jsonify(ok=False, error="That isn't a TV address on your home network."), 400
    try:
        roku.key(ip, "Info")
    except roku.RokuError as e:
        return jsonify(ok=False, error=str(e)), 502
    return jsonify(ok=True)


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


@app.post("/api/tv/wake")
def api_tv_wake():
    """Turn the TV on and put it on this target's input (the same thing starting a stream does). Returns at once."""
    tid = request.args.get("target") or "living"
    if tid not in ROKU_IPS and tid != "living":
        return jsonify(ok=False, error="Unknown TV."), 400

    def go():
        if ROKU_IPS.get(tid) and INPUTS.get(tid):
            switch_input(tid)
        elif tid == "living":
            switch_tv_to_pi()  # no Roku address: wake and select over HDMI-CEC
    threading.Thread(target=go, daemon=True).start()
    return jsonify(ok=True)


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
    if k == "PowerOff":
        threading.Thread(target=on_tv_off, args=(request.args.get("target") or "living",), daemon=True).start()
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
    admin_only()
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
    if route(url)[0] == "Kick" and kick.enabled():
        r = kick.check(chat.channel_of(url))
        if r is not None:
            return r  # the official API; on a failure fall through to streamlink
    j = run_streamlink_json(url)
    if j is None:
        return None
    if j.get("streams"):
        meta = j.get("metadata") or {}
        return {"live": True, "game": meta.get("category"), "title": meta.get("title"), "uptime_s": live_seconds(j)}
    if "No playable streams" in (j.get("error") or ""):
        return {"live": False}
    return None


def fav_links(f):
    """Every link of a favourite: its own, then the same streamer's other sites (alts)."""
    return [{"url": f.get("url"), "source": f.get("source")}] + list(f.get("alts") or [])


def streamer_key(url):
    """The channel name for a Kick or Twitch channel link (lower case), so the same name on both sites matches."""
    return chat.channel_of(url or "") if is_channel(url or "") else None


def watched_urls():
    urls = [l["url"] or "" for u in [accounts.OWNER] + accounts.names() for f in load_favs(u) for l in fav_links(f)]  # everyone's: one pass serves all
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
        links = []
        for l in fav_links(f):
            e = snap.get(l["url"], {})
            links.append({**l, "live": e.get("live"), "game": e.get("game"), "last_live": e.get("last_live"),
                          "checked": e.get("checked"), "live_for": livewatch.live_for(l["url"])})
        top = next((l for l in links if l["live"]), links[0])  # the site they are live on, else the first
        states = [l["live"] for l in links]
        seen = [l["last_live"] for l in links if l["last_live"]]
        out.append({**f, "links": links, "play_url": top["url"],
                    "live": True if any(s is True for s in states) else False if all(s is False for s in states) else None,
                    "game": top["game"], "last_live": max(seen) if seen else None, "checked": top["checked"], "live_for": top["live_for"]})
    return jsonify(out)


@app.post("/api/favourites")
def favs_add():
    data = request.get_json(silent=True) or {}
    url = normalise(data.get("url") or state["url"])
    if not url:
        return jsonify(ok=False), 400
    favs = load_favs()
    if any(url == l["url"] for f in favs for l in fav_links(f)):
        return jsonify(favs)  # already saved (as the main link or as one of a streamer's sites)
    src, key = route(url)[0], streamer_key(url)
    if key and any(streamer_key(l["url"]) == key and l["source"] == src for f in favs for l in fav_links(f)):
        return jsonify(favs)  # the same channel with different capitalisation
    if key:  # the same name on another site is the same favourite: add it as one more chip, not a second entry
        for f in favs:
            if any(streamer_key(l["url"]) == key and l["source"] != src for l in fav_links(f)):
                f.setdefault("alts", []).append({"url": url, "source": src})
                save_favs(favs)
                return jsonify(favs)
    name = data.get("name") or fav_name(url)
    favs.append({"name": name[:40], "url": url, "source": src})
    save_favs(favs)
    return jsonify(favs)


@app.post("/api/favourites/order")
def favs_order():
    """Save the favourites in the order the phone shows them. Anything not listed keeps its place at the end."""
    urls = (request.get_json(silent=True) or {}).get("urls")
    if not isinstance(urls, list):
        return jsonify(ok=False), 400
    favs = load_favs()
    rank = {u: i for i, u in enumerate(urls)}
    favs.sort(key=lambda f: rank.get(f.get("url"), len(rank)))  # stable: unlisted ones stay in their old order
    save_favs(favs)
    return jsonify(favs)


@app.get("/api/kick")
def kick_state():
    return jsonify(can_link=kick.can_link(), name=kick.linked(g.user), can_chat=kick.can_chat(g.user))


@app.get("/auth/kick/start")
def kick_start():
    if not kick.can_link():
        return Response("Kick login is not set up (KICK_CLIENT_ID, KICK_CLIENT_SECRET and KICK_REDIRECT in .env).", 404)
    return redirect(kick.link_url(g.user), 303)


@app.get("/auth/kick/done")
def kick_done():
    """Where the https listener sends the browser back to, with Kick's one-time code."""
    try:
        if request.args.get("error"):
            raise ValueError(request.args["error"])
        kick.link_finish(request.args.get("code"), request.args.get("state"), g.user)
        return redirect("/?kick=linked#/settings", 303)
    except Exception as e:  # a bad state, a refused code or a Kick outage: all end the same way for the user
        print("Kick link failed:", kick.describe(e), flush=True)
        return redirect("/?kick=failed#/settings", 303)


@app.post("/api/kick/unlink")
def kick_unlink():
    kick.unlink(g.user)
    return kick_state()


@app.post("/api/kick/chat")
def kick_chat():
    data = request.get_json(silent=True) or {}
    slug = str(data.get("channel", "")).strip("/").lower()
    slug = slug if re.fullmatch(r"[\w-]{2,25}", slug) else None
    try:
        if not slug:
            raise ValueError("No channel.")
        kick.send_chat(g.user, slug, data.get("text"))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True)


def admin_only():
    if not auth_required():
        abort(Response(json.dumps({"ok": False, "error": "Finish the setup first."}), 403, mimetype="application/json"))
    if accounts.role(g.user) != "admin":
        abort(Response(json.dumps({"ok": False, "error": "Only an admin can do that."}), 403, mimetype="application/json"))


def may_change(name, own_password=False):
    """Admins manage ordinary accounts; only the owner may reset, demote or remove another admin (no takeover between admins)."""
    if accounts.is_owner(g.user) or not accounts.exists(name) or accounts.role(name) != "admin":
        return True
    return own_password and name == g.user


def only_admin(name):
    """True when `name` is the last admin: removing or demoting them would lock everyone out."""
    admins = [u["name"] for u in accounts.listing() if u["role"] == "admin"]
    return admins == [name]


def refuse():
    return jsonify(ok=False, error="Only the owner can change another admin."), 403


@app.get("/api/accounts")
def accounts_list():
    admin_only()
    people = accounts.listing()
    return jsonify(users=[{**u, "favs": len(load_favs(u["name"])), "last": activity.last(u["name"])} for u in people])


@app.post("/api/accounts")
def accounts_add():
    admin_only()
    data = request.get_json(silent=True) or {}
    target = str(data.get("name") or "").strip().lower()
    if not may_change(target, own_password=data.get("role") in (None, "admin")):
        return refuse()
    try:
        name = accounts.add(data.get("name"), data.get("password"), data.get("role"))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    drop_user_sessions(name)  # a new password signs that account out everywhere
    return accounts_list()


@app.patch("/api/accounts")
def accounts_role():
    admin_only()
    data = request.get_json(silent=True) or {}
    if not may_change(str(data.get("name", ""))):
        return refuse()
    if data.get("role") != "admin" and only_admin(str(data.get("name", ""))):
        return jsonify(ok=False, error="That's the only admin. Make someone else an admin first."), 400
    try:
        accounts.set_role(str(data.get("name", "")), data.get("role"))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return accounts_list()


@app.delete("/api/accounts")
def accounts_del():
    admin_only()
    name = str((request.get_json(silent=True) or {}).get("name", ""))
    if name == g.user:
        return jsonify(ok=False, error="You can't remove the account you're signed in with."), 400
    if not may_change(name):
        return refuse()
    if only_admin(name):
        return jsonify(ok=False, error="That's the only admin. Make someone else an admin first."), 400
    if accounts.remove(name):
        activity.forget(name)
        drop_user_sessions(name)  # re-creating the name later must not revive old logins
        for p in (fav_file(name), kick._path(name)):  # its favourites and Kick link go with it
            try:
                os.remove(p)
            except OSError:
                pass
    return accounts_list()


@app.post("/api/accounts/signout")
def accounts_signout():
    admin_only()
    name = str((request.get_json(silent=True) or {}).get("name", ""))
    if not accounts.exists(name):
        return jsonify(ok=False, error="No such account."), 400
    if not may_change(name):
        return refuse()
    drop_user_sessions(name)
    return jsonify(ok=True)


@app.post("/api/account/password")
def account_password():
    data = request.get_json(silent=True) or {}
    if g.user == accounts.OWNER:
        return jsonify(ok=False, error="Create an account first."), 400
    try:
        accounts.change_password(g.user, data.get("current"), data.get("new"))
    except ValueError as e:
        time.sleep(0.8)  # slows guessing
        return jsonify(ok=False, error=str(e)), 400
    drop_user_sessions(g.user)  # signed out everywhere, this device included: log in again with the new one
    return jsonify(ok=True)


@app.get("/api/access")
def access_state():
    admin_only()
    return jsonify(requests=access.requests_pending(), invites=access.invites_pending(), guests=guest_sessions(), used=access.invites_used())


def slug_name(text):
    return re.sub(r"[^a-z0-9_-]", "", re.sub(r"\s+", "-", (text or "").lower()))[:20]


@app.post("/api/requests/<rid>/approve")
def request_approve(rid):
    admin_only()
    data = request.get_json(silent=True) or {}
    req = next((r for r in access.requests_pending() if r["id"] == rid), None)
    if not req:
        return jsonify(ok=False, error="That request is gone."), 404
    name = slug_name(data.get("name") or req["name"])
    if accounts.exists(name):
        return jsonify(ok=False, error="That name is taken. Pick another."), 400
    password = data.get("password") or secrets.token_urlsafe(9)
    role = data.get("role") if data.get("role") in accounts.ROLES else "user"
    if role == "admin" and not accounts.is_owner(g.user):
        return refuse()
    try:
        accounts.add(name, password, role)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    access.pop_request(rid)
    return jsonify(ok=True, name=name, password=None if data.get("password") else password)  # a generated one is shown once


@app.delete("/api/requests/<rid>")
def request_deny(rid):
    admin_only()
    access.pop_request(rid)
    return access_state()


@app.post("/api/invites")
def invite_create():
    admin_only()
    data = request.get_json(silent=True) or {}
    try:
        token = access.create_invite(int(data.get("hours") or 24), data.get("label"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Pick how long access lasts."), 400
    return jsonify(ok=True, url=request.host_url + "join/" + token)


@app.delete("/api/invites/<iid>")
def invite_revoke(iid):
    admin_only()
    access.revoke_invite(iid)
    return access_state()


@app.delete("/api/guests/<gid>")
def guest_revoke(gid):
    admin_only()
    drop_guest(gid)
    return access_state()


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0b0e14"><title>__TITLE__</title>
<style>:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0e14;color:#f6f1ee;font:17px -apple-system,system-ui,sans-serif}
main{width:min(340px,86vw);display:grid;gap:14px}h1{margin:0 0 6px;font-size:26px;text-align:center}p{margin:0;color:#a9b3c3;text-align:center;line-height:1.4}
input,button{font:inherit;min-height:52px;border-radius:14px;border:0;padding:0 16px;box-sizing:border-box;width:100%}
input{background:#1a2030;color:#f6f1ee}button{background:#62b0ff;color:#0b0e14;font-weight:700}.e{color:#ff6b6b;min-height:1.2em}a{color:#8c97a8;text-align:center;padding:10px}</style></head>
<body><main><img src="/logo.svg" width="84" height="84" alt="" style="justify-self:center;border-radius:20px">__BODY__</main></body></html>"""


def page(title, body, status=200):
    return Response(PAGE.replace("__TITLE__", title).replace("__BODY__", body), status, mimetype="text/html", headers={"Cache-Control": "no-store"})


def request_form(err=""):
    return page("Request access", '<h1>Request access</h1><form method="post" action="/request" style="display:grid;gap:14px">'
                '<input name="name" placeholder="Your name" maxlength="30" autocomplete="name" required>'
                '<input name="note" placeholder="Anything to add? (optional)" maxlength="140">'
                f'<p class="e" role="alert">{html.escape(err)}</p><button>Send request</button><a href="/login">Back to log in</a></form>',
                400 if err else 200)


@app.route("/request", methods=["GET", "POST"])
def request_access():
    if request.method == "GET":
        return request_form()
    try:
        access.add_request(request.form.get("name"), request.form.get("note"), request.remote_addr)
    except ValueError as e:
        return request_form(str(e))
    return page("Request sent", "<h1>Request sent</h1><p>Thanks. An admin will look at it. Ask them to tell you when you have an account.</p>"
                '<a href="/login">Back to log in</a>')


@app.route("/join/<token>", methods=["GET", "POST"])
def join(token):
    inv = access.peek_invite(token)
    if not inv:
        return page("Link expired", "<h1>Link expired</h1><p>This link was already used or has expired. Ask for a new one.</p>", 410)
    if request.method == "GET":  # opening the link only shows this page: a chat app's link preview must not use the link up
        hrs = inv["hours"]
        span = f"{hrs} hour{'s' if hrs != 1 else ''}" if hrs < 48 else f"{hrs // 24} days"
        return page("Control the TV", f'<h1>Control the TV</h1><p>You can play things and control the TVs for {span} after you continue. '
                    'This link works once.</p><form method="post" style="display:grid"><button>Continue</button></form>')
    inv = access.redeem_invite(token)
    if not inv:
        return page("Link expired", "<h1>Link expired</h1><p>This link was already used or has expired. Ask for a new one.</p>", 410)
    seconds = inv["hours"] * 3600
    r = redirect("/", 303)
    r.set_cookie("tv_auth", new_session(accounts.GUEST, seconds, {"label": inv["label"], "since": time.time()}), max_age=seconds, httponly=True, samesite="Lax")
    return r


def actor():
    """The key activity is kept under: an account name, "owner", or one guest session."""
    if g.user == accounts.GUEST:
        return "guest:" + _h(request.cookies.get("tv_auth", ""))[:8]
    return g.user


def action_label():
    """One short line for what a request did, or None for requests that aren't worth remembering (polling, page loads)."""
    p, m = request.path, request.method
    where = f" ({request.args['target']})" if request.args.get("target") else ""
    data = (request.get_json(silent=True) or {}) if m in ("POST", "DELETE") else {}
    if p == "/play" or (m == "POST" and p == "/api/play"):
        url = request.args.get("u") or data.get("url") or data.get("text") or ""
        return "Played " + (fav_name(normalise(url) or url) if url else "something") + where
    if m == "POST" and p == "/api/cmd":
        return {"stop": "Stopped playback", "pause": "Paused or resumed", "mute": "Muted or unmuted",
                "restart": "Restarted the stream"}.get(data.get("action"), "Used the player controls") + where
    if p == "/api/favourites" and m in ("POST", "DELETE"):
        return "Saved a favourite" if m == "POST" else "Removed a favourite"
    if m == "POST" and p == "/api/roku/key":
        return "Used the TV remote" + where
    if m == "POST" and p == "/api/kick/chat":
        return "Sent a Kick chat message"
    return None


@app.after_request
def note_action(resp):
    try:
        if resp.status_code < 400 and hasattr(g, "user"):
            label = action_label()
            if label:
                activity.note(actor(), label)
    except Exception:  # remembering what someone did must never break what they did
        pass
    return resp


SETUP_CODE = secrets.token_hex(3)  # printed in the container log while the install is fresh; the wizard asks for it


def host_ok():
    """Is the page being reached by an IP address or a local name? Refuses a public name that points at this machine (DNS rebinding)."""
    host = request.host
    h = (host[1:host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0] if host.count(":") == 1 else host).lower()
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return h == "localhost" or "." not in h or h.endswith((".local", ".lan", ".home.arpa", ".localdomain"))


def setup_code_error(data):
    """None when the host, the throttle and the setup code (printed in the container's log) all pass; otherwise the response to send."""
    ip = request.remote_addr
    if not host_ok():
        return jsonify(ok=False, error="Open the remote by its IP address (or a .local name) to set it up."), 403
    if _throttled(ip, "setup") or len(_recent("setup")) >= 10:  # per address, and 10 wrong guesses from anywhere stop everyone for a while
        return too_many()
    if not hmac.compare_digest(str(data.get("code") or "").strip().lower().encode(), SETUP_CODE.encode()):
        _note_fail(ip, "setup")
        _fails.setdefault("setup", []).append(time.time())
        return jsonify(ok=False, error="That setup code isn't right. It is printed in the container's log: docker logs tvremote"), 403
    return None


def adopt_open_data(name):
    """The favourites and Kick link made before accounts existed belong to the owner: give them to the founder (once)."""
    for old, new in ((FAV_FILE, fav_file(name)), (kick._path(accounts.OWNER), kick._path(name))):
        if os.path.exists(old) and not os.path.exists(new):
            os.replace(old, new)


SETUP_FILE = "setup.json"
_setup_lock = threading.Lock()


def setup_done():
    try:
        with open(os.path.join(DATA, SETUP_FILE)) as f:
            return bool(json.load(f).get("done"))
    except (OSError, ValueError):
        return False


def setup_mark():
    with open(os.path.join(DATA, SETUP_FILE), "w") as f:
        json.dump({"done": True, "at": time.time()}, f)


@app.get("/api/setup")
def setup_state():
    """Does the setup wizard need to run, and where it stands. Only admins (or anyone, on a fresh install with no login yet) see it."""
    fresh = not auth_required()
    if not fresh and accounts.role(g.user) != "admin":
        return jsonify(needed=False)
    has_admin = any(u["role"] == "admin" for u in accounts.listing())
    needs_owner = has_admin and not fresh and not accounts.founder_name()  # an install from before owners: an admin has to claim it
    return jsonify(needed=not setup_done() or needs_owner, done=setup_done(), fresh=fresh, has_admin=has_admin, needs_owner=needs_owner,
                   user=g.user if not fresh else None, needs_code=fresh or needs_owner)


@app.post("/api/setup/admin")
def setup_admin():
    """Step 1: the first admin account. On a fresh install this also logs the new admin in (login is required from now on)."""
    data = request.get_json(silent=True) or {}
    with _setup_lock:
        fresh = not auth_required()
        if fresh:
            err = setup_code_error(data)
            if err:
                return err
        else:
            admin_only()
            if not accounts.is_owner(g.user):
                return refuse()  # a fresh install, or the owner: never another admin
        wanted = str(data.get("name") or "").strip().lower()
        if accounts.exists(wanted):
            return jsonify(ok=False, error="That name is taken."), 400  # setup creates accounts, it never overwrites one
        try:
            name = accounts.add(wanted, data.get("password"), "admin", founder=fresh)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
    r = jsonify(ok=True, name=name)
    if fresh:
        r.set_cookie("tv_auth", new_session(name), max_age=SESSION_DAYS * 86400, httponly=True, samesite="Lax")
        activity.note(name, "Set up the admin account")
    return r


@app.post("/api/setup/claim")
def setup_claim():
    """An install from before owners existed has no owner. An admin who can read the Pi's log (the setup code) claims it explicitly;
    the favourites and Kick link made before accounts existed then become theirs."""
    admin_only()
    if accounts.founder_name():
        return jsonify(ok=False, error="This install already has an owner."), 400
    err = setup_code_error(request.get_json(silent=True) or {})
    if err:
        return err
    if not accounts.claim_founder(g.user):
        return jsonify(ok=False, error="Only an admin can be the owner."), 400
    adopt_open_data(g.user)
    return jsonify(ok=True, name=g.user)


@app.post("/api/setup/finish")
def setup_finish():
    admin_only()
    if not any(u["role"] == "admin" for u in accounts.listing()):
        return jsonify(ok=False, error="Create the admin account first."), 400
    setup_mark()
    return jsonify(ok=True)


@app.delete("/api/favourites")
def favs_del():
    url = (request.get_json(silent=True) or {}).get("url")
    favs = [f for f in load_favs() if f.get("url") != url]  # the main link: the whole favourite goes
    for f in favs:  # one of a streamer's other sites: only that chip goes
        if f.get("alts"):
            f["alts"] = [a for a in f["alts"] if a.get("url") != url]
            if not f["alts"]:
                del f["alts"]
    save_favs(favs)
    return jsonify(favs)


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    apply_tv_config(tvconfig.load(DATA))
    if CastTarget:
        CAST = CastTarget(DATA, resolve, display_title, YTDL_HEIGHTS, SL_DEFAULT)
    cards.start()
    livewatch.start()
    threading.Thread(target=sleep_loop, daemon=True).start()
    settings.update(load_settings())
    HOME = shell.Shell(hint=f"Open http://{shell.lan_ip()}:{PORT} on your phone",
                       favs=lambda: [(f.get("name") or "")[:28] for f in load_favs()][:5])
    if settings["shell"]:
        threading.Thread(target=HOME.start, daemon=True).start()
    if kick.can_link():
        try:
            threading.Thread(target=tlscallback.make_server(kick.REDIRECT, PORT, DATA).serve_forever, daemon=True).start()
        except Exception as e:  # no openssl or the port is taken: the rest of the app still runs
            print("Kick login listener not started:", e, flush=True)
    if not auth_required():
        print(f"FIRST RUN: open the remote and enter this setup code to create the admin account: {SETUP_CODE}", flush=True)
    elif not accounts.founder_name():
        print(f"OWNER NEEDED: log in as an admin and enter this setup code in the setup wizard to become the owner: {SETUP_CODE}", flush=True)
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=8)
